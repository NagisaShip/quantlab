"""RFC 6238 TOTP（双因素登录）与一次性恢复码的纯算法实现。

为什么自己实现而不用 pyotp：
- 仓库对第三方依赖是锁定的（requirements.lock）。多一个依赖就多一份供应链与审计成本，
  而 TOTP 本身只是 HMAC-SHA1 + 动态截断，标准库 `hmac`/`hashlib`/`base64`/`struct` 足够；
- 自己写可以把"允许 ±1 步漂移""拒绝重放""恒定时间比较"这些安全语义显式表达出来，
  而不是藏在第三方库的默认值里。

安全约定（与任务包 T11 一致）：
- 共享密钥属于凭证：本模块只负责算法，落库加密由 `AuthService` 调 `SecretBox` 完成；
- 恢复码只以哈希形式存储，明文仅在生成时返回一次；
- 所有比较都走 `hmac.compare_digest`，避免用比较耗时泄露信息。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time

# RFC 6238 默认参数：SHA-1、6 位、30 秒步长。与 Google Authenticator / 1Password 等主流
# 验证器一致，用户扫码即可用，不需要额外说明。
DIGITS = 6
PERIOD_SECONDS = 30
SECRET_BYTES = 20  # SHA-1 的分组长度就是 20 字节，与 RFC 4226 的推荐值一致
DRIFT_STEPS = 1  # 允许前后各 1 个时间步（±30 秒），容忍客户端与服务端的时钟偏差
RECOVERY_CODE_COUNT = 10
RECOVERY_CODE_BYTES = 10  # 10 字节 → base32 16 字符，足够抗暴力枚举


# ---------------------------------------------------------------------------
# 密钥
# ---------------------------------------------------------------------------
def generate_secret() -> str:
    """生成新的共享密钥，用 base32（无填充）表示——otpauth:// 与验证器都要求这种形式。"""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def normalize_secret(secret: str) -> bytes:
    """把用户/验证器给的 base32 密钥规范化成字节。

    验证器会把密钥显示成 `abcd efgh …` 这种带空格、可能小写的形式，用户手抄时还会漏掉填充，
    所以这里统一去掉空白与 `=`、转大写、补齐填充后再解码。
    """
    cleaned = "".join(secret.split()).replace("=", "").upper()
    if not cleaned:
        raise ValueError("密钥为空")
    padding = "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(cleaned + padding)
    except Exception as exc:  # binascii.Error 等
        raise ValueError("密钥不是合法的 base32 字符串") from exc


# ---------------------------------------------------------------------------
# 核心算法
# ---------------------------------------------------------------------------
def hotp(secret: bytes, counter: int, *, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP：HMAC-SHA1(密钥, 计数器) → 动态截断 → 取末 `digits` 位。

    动态截断（dynamic truncation）是 RFC 4226 §5.3 的做法：用摘要最后一个字节的低 4 位
    当偏移量，从该位置取 4 字节大端整数，再对 10^digits 取模。
    """
    if counter < 0:
        raise ValueError("计数器不能为负")
    digest = hmac.new(secret, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**digits)).zfill(digits)


def totp(secret: str, *, at: float | None = None, period: int = PERIOD_SECONDS, digits: int = DIGITS) -> str:
    """某一时刻的 TOTP 码。`at` 是 Unix 秒（默认取当前时间），便于测试注入固定时间。"""
    counter = int((time.time() if at is None else at) // period)
    return hotp(normalize_secret(secret), counter, digits=digits)


def step_at(at: float | None = None, *, period: int = PERIOD_SECONDS) -> int:
    """某一时刻所属的时间步。重放防护记录的就是这个值。"""
    return int((time.time() if at is None else at) // period)


def verify(
    secret: str,
    code: str,
    *,
    at: float | None = None,
    period: int = PERIOD_SECONDS,
    digits: int = DIGITS,
    drift_steps: int = DRIFT_STEPS,
) -> int | None:
    """校验验证码，成功返回匹配的时间步，失败返回 None。

    返回时间步而不是布尔值，是为了让调用方能把"这一步已经用过了"记下来做重放防护——
    只返回 True/False 的话，调用方还得再算一次窗口，容易两边算法漂移。

    允许 ±drift_steps 个时间步的漂移：手机时钟与服务器差几十秒是常态，
    不容忍漂移会让用户频繁遇到"验证码错误"，从而被推向关闭 2FA。
    """
    candidate = (code or "").strip().replace(" ", "")
    if not candidate.isdigit() or len(candidate) != digits:
        return None
    try:
        key = normalize_secret(secret)
    except ValueError:
        return None
    current = step_at(at, period=period)
    for offset in range(-drift_steps, drift_steps + 1):
        step = current + offset
        if step < 0:
            continue
        # 恒定时间比较：避免用耗时差异逐位猜出验证码
        if hmac.compare_digest(hotp(key, step, digits=digits), candidate):
            return step
    return None


# ---------------------------------------------------------------------------
# otpauth:// 链接
# ---------------------------------------------------------------------------
def provisioning_uri(secret: str, *, account: str, issuer: str = "QuantLab") -> str:
    """生成供验证器 App 扫码的 otpauth:// 链接。

    只在启用时返回一次：链接里含明文密钥，等同于凭证本身。
    """
    from urllib.parse import quote, urlencode

    label = quote(f"{issuer}:{account}", safe="")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


# ---------------------------------------------------------------------------
# 恢复码
# ---------------------------------------------------------------------------
def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """生成一次性恢复码（明文，只在生成时返回一次）。"""
    return [
        base64.b32encode(secrets.token_bytes(RECOVERY_CODE_BYTES)).decode("ascii").rstrip("=") for _ in range(count)
    ]


def normalize_recovery_code(code: str) -> str:
    """恢复码规范化：去空白、转大写、去掉用户抄写时可能加的分隔符（`-`）。"""
    return "".join((code or "").split()).replace("-", "").upper()


def hash_recovery_code(code: str) -> str:
    """恢复码哈希。

    恢复码是 80 位随机值，不存在被字典攻击的弱口令空间，因此用 SHA-256 即可；
    加盐反而会让"按哈希查表"需要全表扫描。密码哈希（scrypt）用在别处。
    """
    return hashlib.sha256(normalize_recovery_code(code).encode("utf-8")).hexdigest()
