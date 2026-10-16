"""用户、会话、工作区、成员与邀请。

注册策略（一期为受控注册）：
- 系统里还没有任何用户时，第一个注册者成为部署管理员，并获得自己的工作区；
- 之后只能凭邀请码注册（邀请码绑定工作区与角色），除非部署时显式设置 QAGENT_OPEN_REGISTRATION=1。

角色：
- admin    工作区管理员：成员、配额、设置、审计
- member   普通成员：创建策略、数据集、运行与报告
- reviewer 审阅者：只读
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .db import Database, IntegrityError, jdump, jload, now_ms
from .errors import AppError, ErrorCode

SESSION_TTL_MS = 14 * 24 * 3600 * 1000
INVITE_TTL_MS = 7 * 24 * 3600 * 1000
ROLES = ("admin", "member", "reviewer")

# T11 · 双因素登录：验证码错误锁定策略（5 次 / 15 分钟）
TOTP_MAX_ATTEMPTS = 5
TOTP_LOCK_MS = 15 * 60 * 1000

DEFAULT_QUOTAS = {
    "max_concurrent_runs": 4,  # 同时处于排队 / 运行状态的运行数
    "max_runs_per_day": 200,  # 每天创建的运行数
    "max_bars_per_run": 1_000_000,  # 单次回测的 K 线数上限
    "max_paper_runs": 3,  # 同时运行的实时模拟数
    "allow_python_strategies": True,  # Python 策略（在隔离子进程中运行；管理员可关闭，只留规则 DSL）
}
DEFAULT_SETTINGS = {"timezone": "Asia/Shanghai", "quotas": DEFAULT_QUOTAS}


# ---------------------------------------------------------------------------
# 口令
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    n, r, p = 2**14, 8, 1
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


#: 只用用户名注册的账号，email 列写 <id>@unbound.invalid（保留域名，永远收不到信）
UNBOUND_DOMAIN = "@unbound.invalid"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-一-鿿]{2,32}$")


def normalize_username(raw: str) -> str:
    name = (raw or "").strip()
    if not _USERNAME_RE.match(name) or name.startswith((".", "-")):
        raise AppError(ErrorCode.INVALID_PARAMETER, "用户名 2–32 个字符，只能用字母、数字、中文、下划线、点和短横线，且不能含 @")
    return name


def public_email(email: str | None) -> str:
    """对外显示的邮箱：占位地址显示为空（未绑定）。"""
    return "" if not email or email.endswith(UNBOUND_DOMAIN) else email


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise AppError(ErrorCode.INVALID_PARAMETER, "密码至少 8 位")
    if password.isdigit() or password.isalpha():
        raise AppError(ErrorCode.INVALID_PARAMETER, "密码需要同时包含字母和数字（或符号）")


# ---------------------------------------------------------------------------
# 登录限速（进程内；多实例部署时由反向代理再做一层）
# ---------------------------------------------------------------------------
class LoginLimiter:
    WINDOW_MS = 15 * 60 * 1000
    MAX_FAILURES = 10

    def __init__(self) -> None:
        self._fails: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = now_ms()
        with self._lock:
            recent = [t for t in self._fails.get(key, []) if now - t < self.WINDOW_MS]
            self._fails[key] = recent
            if len(recent) >= self.MAX_FAILURES:
                raise AppError(ErrorCode.RATE_LIMITED, "登录失败次数过多，请 15 分钟后再试")

    def fail(self, key: str) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(now_ms())

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


limiter = LoginLimiter()


@dataclass
class SessionUser:
    user_id: str
    email: str
    name: str
    is_admin: bool
    session_id: str


class AuthService:
    def __init__(self, db: Database, secrets: Any = None) -> None:
        self.db = db
        # TOTP 共享密钥要加密落库。默认惰性构造一个 SecretBox（数据目录同 AppServices），
        # 这样只构造 AuthService(db) 的老代码（含测试）不用改；显式传入则复用同一个实例。
        self._secrets = secrets
        # 部署管理员在运维控制台里改的平台设置（开放注册等）；没有注册时只看环境变量
        self.settings_reader: Any = None

    @property
    def secrets(self) -> Any:
        if self._secrets is None:
            from pathlib import Path

            from .secretbox import SecretBox

            home = Path(os.environ.get("QAGENT_HOME", "") or (Path.home() / ".quant_agent"))
            self._secrets = SecretBox(home)
        return self._secrets

    # ---------------- 用户 ----------------
    def user_count(self) -> int:
        return int(self.db.scalar("SELECT COUNT(*) AS n FROM users") or 0)

    def open_registration(self) -> bool:
        if self.settings_reader is not None:
            try:
                override = (self.settings_reader() or {}).get("open_registration")
            except Exception:
                override = None
            if override is not None:
                return bool(override)
        return os.environ.get("QAGENT_OPEN_REGISTRATION", "") == "1"

    def register(
        self, *, email: str = "", name: str = "", password: str, invite_code: str | None = None, username: str = ""
    ) -> dict[str, Any]:
        """注册。用户名与邮箱至少给一个：只给用户名时邮箱记占位地址，之后在账号设置里用验证码绑定；
        只给邮箱时用户名取邮箱 @ 前面的部分（重名自动加数字）。注册时填的邮箱不算已验证。"""
        email = email.strip().lower()
        username = normalize_username(username) if username.strip() else ""
        if not email and not username:
            raise AppError(ErrorCode.INVALID_PARAMETER, "请填写用户名或邮箱")
        if email and ("@" not in email or len(email) > 200 or email.endswith(UNBOUND_DOMAIN)):
            raise AppError(ErrorCode.INVALID_PARAMETER, "邮箱格式不正确")
        validate_password(password)
        if username and self.db.fetchone("SELECT id FROM users WHERE lower(username)=?", (username.lower(),)):
            raise AppError(ErrorCode.CONFLICT, "用户名已被使用")
        if not username:
            username = self._free_username(email.split("@")[0])
        name = name.strip()[:40] or username
        now = now_ms()
        with self.db.tx() as t:
            first = int(t.scalar("SELECT COUNT(*) AS n FROM users") or 0) == 0
            invite = None
            if not first:
                if invite_code:
                    invite = t.fetchone(
                        "SELECT * FROM invites WHERE code_hash=? AND used_at IS NULL AND revoked_at IS NULL",
                        (_token_hash(invite_code.strip()),),
                    )
                    if invite is None or invite["expires_at"] < now:
                        raise AppError(ErrorCode.PERMISSION_DENIED, "邀请码无效或已过期")
                    if invite["email"] and invite["email"].lower() != email:
                        raise AppError(ErrorCode.PERMISSION_DENIED, "邀请码与邮箱不匹配")
                elif not self.open_registration():
                    raise AppError(ErrorCode.PERMISSION_DENIED, "本部署为受控注册：请向工作区管理员索取邀请码")
            user_id = uuid.uuid4().hex
            try:
                t.execute(
                    "INSERT INTO users (id, email, username, name, password_hash, is_admin, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (user_id, email or f"{user_id}{UNBOUND_DOMAIN}", username, name, hash_password(password), first, now),
                )
            except IntegrityError as exc:
                raise AppError(ErrorCode.CONFLICT, "该邮箱或用户名已注册") from exc
            if invite is not None:
                t.execute(
                    "INSERT INTO memberships (workspace_id, user_id, role, created_at) VALUES (?,?,?,?)",
                    (invite["workspace_id"], user_id, invite["role"], now),
                )
                t.execute("UPDATE invites SET used_by=?, used_at=? WHERE id=?", (user_id, now, invite["id"]))
                workspace_id = invite["workspace_id"]
            else:
                workspace_id = self._create_workspace(t, owner_id=user_id, name=f"{name} 的工作区")
        return {"user_id": user_id, "workspace_id": workspace_id, "is_admin": first}

    def _create_workspace(self, t, *, owner_id: str, name: str, is_demo: bool = False) -> str:
        ws_id = uuid.uuid4().hex[:16]
        t.execute(
            "INSERT INTO workspaces (id, name, owner_id, is_demo, settings, created_at) VALUES (?,?,?,?,?,?)",
            (ws_id, name[:60], owner_id, is_demo, jdump(DEFAULT_SETTINGS), now_ms()),
        )
        t.execute(
            "INSERT INTO memberships (workspace_id, user_id, role, created_at) VALUES (?,?,?,?)",
            (ws_id, owner_id, "admin", now_ms()),
        )
        return ws_id

    def create_workspace(self, *, owner_id: str, name: str) -> str:
        with self.db.tx() as t:
            return self._create_workspace(t, owner_id=owner_id, name=name)

    # ---------------- 会话 ----------------
    def login(
        self,
        *,
        email: str,
        password: str,
        ip: str = "",
        user_agent: str = "",
        totp_code: str | None = None,
    ) -> tuple[str, dict]:
        """密码登录。

        已启用 TOTP 的用户，密码正确后**不会**直接拿到会话：必须在本请求里带上正确的
        `totp_code`（验证码或恢复码），否则抛 `UNAUTHENTICATED` 且 `details.totp_required=True`，
        前端据此提示用户输入验证码并重发登录请求。
        """
        # email 参数也接受用户名：含 @ 按邮箱查，否则按用户名查（不区分大小写）
        email = email.strip().lower()
        key = f"{email}|{ip}"
        limiter.check(key)
        if "@" in email:
            row = self.db.fetchone("SELECT * FROM users WHERE email=?", (email,))
        else:
            row = self.db.fetchone("SELECT * FROM users WHERE lower(username)=?", (email,))
        if row is None or not verify_password(password, row["password_hash"]):
            limiter.fail(key)
            raise AppError(ErrorCode.UNAUTHENTICATED, "用户名 / 邮箱或密码错误")
        if not row["is_active"]:
            # 密码正确才说明原因：不给撞库者额外信息
            reason = row.get("banned_reason") or ""
            raise AppError(
                ErrorCode.PERMISSION_DENIED,
                "账号已被管理员封禁" + (f"：{reason}" if reason else "") + "。如有疑问请联系部署管理员",
                details={"banned": True},
            )
        if self.totp_enabled(row["id"]):
            # 密码已正确：这里不再计入密码失败次数，改由 TOTP 自己的锁定策略约束
            limiter.clear(key)
            self._require_second_factor(row, totp_code=totp_code)
        limiter.clear(key)
        token = secrets.token_urlsafe(32)
        now = now_ms()
        with self.db.tx() as t:
            t.execute(
                "INSERT INTO sessions (id, token_hash, user_id, created_at, expires_at, ip, user_agent) VALUES (?,?,?,"
                "?,?,?,?)",
                (uuid.uuid4().hex, _token_hash(token), row["id"], now, now + SESSION_TTL_MS, ip[:64], user_agent[:200]),
            )
            t.execute("UPDATE users SET last_login_at=? WHERE id=?", (now, row["id"]))
        return token, row

    # ---------------- 双因素登录（TOTP，T11） ----------------
    #
    # 共享密钥与恢复码都属于凭证：
    # - 密钥用 SecretBox 加密后再落库，恢复码只存 SHA-256 哈希；
    # - 明文只在"启用"响应里返回一次（用户要扫码 / 抄写恢复码），之后任何查询都不回显；
    # - 审计 details 里只写计数与时间，不写任何密钥片段。
    def totp_row(self, user_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM user_totp WHERE user_id=?", (user_id,))

    def totp_enabled(self, user_id: str) -> bool:
        """是否已启用 2FA：只有 enabled_at 非空才算启用（创建后未确认的行不算）。"""
        row = self.totp_row(user_id)
        return bool(row and row.get("enabled_at"))

    def _default_workspace(self, user_id: str) -> str:
        """用户的主工作区。

        user_totp.workspace_id 记录"在哪个工作区启用的 2FA"——用于工作区级的管理员视图与
        跨工作区隔离检查；登录本身是全局的（一个用户一个密钥），不随工作区切换而变。
        """
        rows = self.memberships(user_id)
        if not rows:
            raise AppError(ErrorCode.PERMISSION_DENIED, "当前用户不属于任何工作区")
        return rows[0]["id"]

    def totp_status(self, user_id: str) -> dict[str, Any]:
        """2FA 状态。**只回显是否启用、剩余恢复码数、锁定截止时间**，绝不回显密钥。"""
        row = self.totp_row(user_id)
        # 部署级开关：部署管理员可以要求"工作区管理员必须启用 2FA"。
        # 这里只回报 required / setup_required 供前端强制跳设置页，不做硬拦截——
        # 硬拦等于把管理员挡在登录之外，他反而永远无法完成启用。
        required = os.environ.get("QAGENT_REQUIRE_TOTP_FOR_ADMINS", "") == "1"
        is_ws_admin = bool(
            self.db.scalar(
                "SELECT COUNT(*) AS n FROM memberships WHERE user_id=? AND role='admin' AND status='active'",
                (user_id,),
            )
            or 0
        )
        enabled = bool(row and row.get("enabled_at"))
        base: dict[str, Any] = {
            "enabled": enabled,
            "recovery_codes_left": 0,
            "enabled_at": None,
            "locked_until": None,
        }
        if row is not None:
            left = self.db.scalar(
                "SELECT COUNT(*) AS n FROM totp_recovery_codes WHERE user_id=? AND used_at IS NULL", (user_id,)
            )
            base["recovery_codes_left"] = int(left or 0)
            base["enabled_at"] = row.get("enabled_at")
            base["locked_until"] = row.get("locked_until")
        base["required_for_admins"] = required
        base["setup_required"] = bool(required and is_ws_admin and not enabled)
        return base

    def totp_setup(self, user_id: str, *, issuer: str = "QuantLab", workspace_id: str | None = None) -> dict[str, Any]:
        """开始启用 2FA：生成密钥并返回 otpauth:// 链接。

        此时**还没有启用**——必须再调一次 `totp_enable` 用一次真实验证码确认，
        否则用户扫错码或验证器没配好就会被锁在门外。
        明文密钥只在这一个响应里出现。
        """
        from . import totp as T

        user = self.db.fetchone("SELECT email FROM users WHERE id=?", (user_id,))
        if user is None:
            raise AppError(ErrorCode.NOT_FOUND, "用户不存在")
        if self.totp_enabled(user_id):
            raise AppError(ErrorCode.CONFLICT, "双因素认证已启用；如需更换验证器请先禁用")
        # 绑定发生在哪个工作区就记在哪里：工作区之间互相看不到对方的 2FA 绑定
        target_ws = workspace_id or self._default_workspace(user_id)
        if self.role_in(user_id, target_ws) is None:
            raise AppError(ErrorCode.PERMISSION_DENIED, "无权在该工作区绑定双因素认证")
        secret = T.generate_secret()
        now = now_ms()
        with self.db.tx() as t:
            t.execute("DELETE FROM user_totp WHERE user_id=?", (user_id,))
            t.execute(
                "INSERT INTO user_totp (user_id, workspace_id, secret_enc, enabled_at, created_at, last_used_step, "
                "failed_attempts, locked_until) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, target_ws, self.secrets.encrypt(secret), None, now, None, 0, None),
            )
        return {
            "secret": secret,
            "otpauth_uri": T.provisioning_uri(secret, account=user["email"], issuer=issuer),
            "digits": T.DIGITS,
            "period": T.PERIOD_SECONDS,
            "algorithm": "SHA1",
            "note": "请立即用验证器扫码或手工录入密钥；密钥只显示这一次，平台之后不会回显。",
        }

    def _secret_of(self, row: dict[str, Any]) -> str:
        """解出共享密钥。解密失败说明主密钥换了——按凭证撤销处理，不泄露密文内容。"""
        from .secretbox import SecretError

        try:
            return self.secrets.decrypt(row["secret_enc"])
        except SecretError as exc:
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "双因素密钥无法解密，请重新绑定验证器") from exc

    def totp_enable(self, user_id: str, code: str) -> dict[str, Any]:
        """用一次真实验证码确认绑定，并生成一次性恢复码。

        恢复码明文只在这个响应里返回一次，之后平台只保留哈希。

        这里**不**记录 `last_used_step`：确认绑定不是一次登录，如果把它算成"用过的码"，
        用户启用完立刻退出再登录（还在同一个 30 秒窗口内）就会被自己的防重放拦下。
        防重放只针对发放会话的登录。
        """
        from . import totp as T

        row = self.totp_row(user_id)
        if row is None:
            raise AppError(ErrorCode.INVALID_PARAMETER, "请先开始启用双因素认证（获取密钥）")
        if row.get("enabled_at"):
            raise AppError(ErrorCode.CONFLICT, "双因素认证已启用")
        if T.verify(self._secret_of(row), code) is None:
            raise AppError(ErrorCode.UNAUTHENTICATED, "验证码不正确，请检查验证器时间后重试")
        codes = T.generate_recovery_codes()
        now = now_ms()
        with self.db.tx() as t:
            t.execute(
                "UPDATE user_totp SET enabled_at=?, failed_attempts=0, locked_until=NULL WHERE user_id=?",
                (now, user_id),
            )
            t.execute("DELETE FROM totp_recovery_codes WHERE user_id=?", (user_id,))
            for c in codes:
                t.execute(
                    "INSERT INTO totp_recovery_codes (id, user_id, workspace_id, code_hash, created_at, used_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, row["workspace_id"], T.hash_recovery_code(c), now, None),
                )
        return {
            "enabled": True,
            "recovery_codes": codes,
            "note": "恢复码只显示这一次，请离线保存；每个恢复码只能用一次。",
        }

    def totp_disable(self, user_id: str, *, password: str, code: str) -> dict[str, Any]:
        """关闭 2FA。必须同时提供密码与当前验证码（或恢复码）。

        只凭会话就能关 2FA 的话，会话被劫持就等于 2FA 白开——所以这里再验一次凭证。
        """
        from . import totp as T

        row = self.totp_row(user_id)
        if row is None or not row.get("enabled_at"):
            raise AppError(ErrorCode.INVALID_PARAMETER, "双因素认证未启用")
        user = self.db.fetchone("SELECT password_hash FROM users WHERE id=?", (user_id,))
        if user is None or not verify_password(password, user["password_hash"]):
            raise AppError(ErrorCode.PERMISSION_DENIED, "密码不正确")
        if T.verify(self._secret_of(row), code) is None and not self._consume_recovery_code(user_id, code):
            raise AppError(ErrorCode.UNAUTHENTICATED, "验证码或恢复码不正确")
        with self.db.tx() as t:
            t.execute("DELETE FROM user_totp WHERE user_id=?", (user_id,))
            t.execute("DELETE FROM totp_recovery_codes WHERE user_id=?", (user_id,))
        return {"enabled": False, "note": "双因素认证已关闭，恢复码全部失效。"}

    def totp_recovery_codes(self, user_id: str) -> list[str]:
        """重新生成恢复码（旧的全部作废）。同样只在这一次返回明文。"""
        from . import totp as T

        row = self.totp_row(user_id)
        if row is None or not row.get("enabled_at"):
            raise AppError(ErrorCode.INVALID_PARAMETER, "双因素认证未启用")
        codes = T.generate_recovery_codes()
        now = now_ms()
        with self.db.tx() as t:
            t.execute("DELETE FROM totp_recovery_codes WHERE user_id=?", (user_id,))
            for c in codes:
                t.execute(
                    "INSERT INTO totp_recovery_codes (id, user_id, workspace_id, code_hash, created_at, used_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, user_id, row["workspace_id"], T.hash_recovery_code(c), now, None),
                )
        return codes

    def _consume_recovery_code(self, user_id: str, code: str) -> bool:
        """核销一个恢复码。用过即失效（used_at 非空），并且只允许核销未使用的那一条。"""
        from . import totp as T

        normalized = T.normalize_recovery_code(code)
        if not normalized:
            return False
        digest = T.hash_recovery_code(normalized)
        now = now_ms()
        with self.db.tx() as t:
            row = t.fetchone(
                "SELECT id FROM totp_recovery_codes WHERE user_id=? AND code_hash=? AND used_at IS NULL",
                (user_id, digest),
            )
            if row is None:
                return False
            # 条件里再带一次 used_at IS NULL：并发下两个请求抢同一个码时只有一个能改到行
            n = t.execute("UPDATE totp_recovery_codes SET used_at=? WHERE id=? AND used_at IS NULL", (now, row["id"]))
            return bool(n)

    def verify_second_factor(self, user_id: str, code: str, *, at: float | None = None) -> bool:
        """校验 TOTP 验证码或恢复码，带防重放与失败锁定。

        - 防重放：同一个时间步只能成功用一次，且时间步必须严格递增（`step <= last_used_step`
          一律拒绝）。只判"是否等于上一步"会留下漏洞：攻击者截获第 N 步的码后，
          只要用户先用掉第 N+1 步，重放第 N 步就又通过了。
        - 锁定：连续 5 次错误锁 15 分钟，防止 6 位码被在线暴力枚举（10^6 空间）。

        `at` 只是给测试注入固定时钟用的（时间相关逻辑不注入时钟就没法确定性地测）；
        生产路径不传，走真实时间。
        """
        from . import totp as T

        row = self.totp_row(user_id)
        if row is None or not row.get("enabled_at"):
            return False
        now = now_ms()
        locked_until = row.get("locked_until")
        if locked_until and locked_until > now:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                "验证码错误次数过多，账号已临时锁定，请稍后再试",
                details={"locked_until": locked_until},
            )

        step = T.verify(self._secret_of(row), code, at=at)
        if step is not None:
            # 重放检查与写入放在同一个事务里，避免两个并发请求都通过检查。
            # 注意：判定结果要等到事务提交后再抛错——在事务里 raise 会连带 ROLLBACK，
            # 把刚写下的 last_used_step 一起丢掉，防重放就形同虚设。
            replayed = False
            with self.db.tx() as t:
                fresh = t.fetchone("SELECT last_used_step FROM user_totp WHERE user_id=?", (user_id,))
                if fresh and fresh.get("last_used_step") is not None and step <= fresh["last_used_step"]:
                    replayed = True
                else:
                    t.execute(
                        "UPDATE user_totp SET last_used_step=?, failed_attempts=0, locked_until=NULL WHERE user_id=?",
                        (step, user_id),
                    )
            if replayed:
                # 重复提交同一个码不计入失败次数：用户双击提交不该把自己锁掉
                raise AppError(
                    ErrorCode.UNAUTHENTICATED,
                    "该验证码已经使用过，请等待验证器生成下一个验证码",
                    details={"reason": "replayed"},
                )
            return True

        if self._consume_recovery_code(user_id, code):
            # 恢复码不参与步长防重放：它本身就是一次性的，用过即失效
            self.db.execute("UPDATE user_totp SET failed_attempts=0, locked_until=NULL WHERE user_id=?", (user_id,))
            return True

        self._fail_attempt(user_id)
        return False

    def _fail_attempt(self, user_id: str) -> None:
        """记一次失败；达到阈值就锁定。

        写库与抛错必须分开：AppError 在 `with self.db.tx()` 里抛出会触发 ROLLBACK，
        锁定时间就写不进去了。所以先在事务里落库，出了事务再决定抛哪种错。
        """
        locked_until: int | None = None
        with self.db.tx() as t:
            row = t.fetchone("SELECT failed_attempts FROM user_totp WHERE user_id=?", (user_id,))
            attempts = int((row or {}).get("failed_attempts") or 0) + 1
            if attempts >= TOTP_MAX_ATTEMPTS:
                locked_until = now_ms() + TOTP_LOCK_MS
                t.execute(
                    "UPDATE user_totp SET failed_attempts=?, locked_until=? WHERE user_id=?",
                    (attempts, locked_until, user_id),
                )
            else:
                t.execute("UPDATE user_totp SET failed_attempts=? WHERE user_id=?", (attempts, user_id))
        if locked_until is not None:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"验证码连续错误 {TOTP_MAX_ATTEMPTS} 次，账号已锁定 15 分钟",
                details={"locked_until": locked_until, "attempts": attempts},
            )
        raise AppError(
            ErrorCode.UNAUTHENTICATED,
            "验证码不正确",
            details={"attempts": attempts, "remaining": TOTP_MAX_ATTEMPTS - attempts},
        )

    def _require_second_factor(self, row: dict[str, Any], *, totp_code: str | None) -> None:
        """已启用 2FA 的用户：本次登录必须带上正确的验证码（或恢复码）。

        为什么不做"票据式两步登录"：第二步的端点不在 `PUBLIC_PATHS` 里，
        没有会话就会被中间件挡在 401，而中间件不在 T11 的改动范围内。
        因此采用"重发一次登录请求并附验证码"的形态：`/api/auth/login` 本就是公共端点，
        密码每次都会被重新校验（口令限速同样生效），安全性与票据式等价。
        """
        if not totp_code:
            raise AppError(
                ErrorCode.UNAUTHENTICATED,
                "该账号已启用双因素认证，请在登录时提交验证码",
                details={"totp_required": True},
            )
        if not self.verify_second_factor(row["id"], totp_code):
            # verify_second_factor 内部已抛错；这里只是兜底，避免静默通过
            raise AppError(ErrorCode.UNAUTHENTICATED, "验证码不正确")

    def resolve(self, token: str) -> SessionUser | None:
        if not token:
            return None
        row = self.db.fetchone(
            "SELECT s.id AS sid, s.expires_at, s.revoked_at, u.id, u.email, u.name, u.is_admin, u.is_active "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_token_hash(token),),
        )
        if row is None or row["revoked_at"] or row["expires_at"] < now_ms() or not row["is_active"]:
            return None
        return SessionUser(row["id"], row["email"], row["name"], bool(row["is_admin"]), row["sid"])

    def logout(self, token: str) -> None:
        self.db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=?", (now_ms(), _token_hash(token)))

    def change_password(self, user_id: str, old: str, new: str) -> None:
        row = self.db.fetchone("SELECT password_hash FROM users WHERE id=?", (user_id,))
        if row is None or not verify_password(old, row["password_hash"]):
            raise AppError(ErrorCode.PERMISSION_DENIED, "原密码不正确")
        validate_password(new)
        with self.db.tx() as t:
            t.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), user_id))
            t.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_ms(), user_id))

    # ---------------- 账号资料：用户名、绑定邮箱 ----------------
    EMAIL_CODE_TTL_MS = 10 * 60_000
    EMAIL_CODE_COOLDOWN_MS = 60_000
    EMAIL_CODE_HOURLY = 5
    EMAIL_CODE_ATTEMPTS = 5

    def _free_username(self, base: str) -> str:
        base = re.sub(r"[^\w.\-一-鿿]", "", base)[:24] or "user"
        if len(base) < 2:
            base = f"{base}_u"
        name, n = base, 1
        while self.db.fetchone("SELECT id FROM users WHERE lower(username)=?", (name.lower(),)):
            n += 1
            name = f"{base}{n}"
        return name

    def backfill_usernames(self) -> int:
        """给还没有用户名的老账号补一个（邮箱 @ 前面的部分，重名加数字）。幂等，启动时调用。"""
        rows = self.db.fetchall("SELECT id, email FROM users WHERE username IS NULL OR username='' ORDER BY created_at")
        for r in rows:
            self.db.execute("UPDATE users SET username=? WHERE id=?", (self._free_username(r["email"].split("@")[0]), r["id"]))
        return len(rows)

    def account(self, user_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT id, email, username, name, email_verified_at FROM users WHERE id=?", (user_id,))
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "账号不存在")
        email = public_email(row["email"])
        return {
            "id": row["id"],
            "username": row["username"] or "",
            "name": row["name"],
            "email": email,
            "email_verified": bool(email and row["email_verified_at"]),
        }

    def update_account(self, user_id: str, *, username: str | None = None, name: str | None = None) -> dict[str, Any]:
        if username is not None:
            username = normalize_username(username)
            other = self.db.fetchone("SELECT id FROM users WHERE lower(username)=? AND id<>?", (username.lower(), user_id))
            if other:
                raise AppError(ErrorCode.CONFLICT, "用户名已被使用")
            self.db.execute("UPDATE users SET username=? WHERE id=?", (username, user_id))
        if name is not None and name.strip():
            self.db.execute("UPDATE users SET name=? WHERE id=?", (name.strip()[:40], user_id))
        return self.account(user_id)

    def send_email_code(self, user_id: str, email: str) -> dict[str, Any]:
        """给要绑定的邮箱发 6 位验证码（10 分钟有效）。同一账号 60 秒内只能发一次、每小时最多 5 次。"""
        from . import mailer

        email = email.strip().lower()
        if "@" not in email or len(email) > 200 or email.endswith(UNBOUND_DOMAIN) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise AppError(ErrorCode.INVALID_PARAMETER, "邮箱格式不正确")
        taken = self.db.fetchone("SELECT id FROM users WHERE email=? AND id<>?", (email, user_id))
        if taken:
            raise AppError(ErrorCode.CONFLICT, "该邮箱已绑定其他账号")
        now = now_ms()
        recent = self.db.fetchall(
            "SELECT created_at FROM email_codes WHERE user_id=? AND created_at>? ORDER BY created_at DESC",
            (user_id, now - 3_600_000),
        )
        if recent and now - recent[0]["created_at"] < self.EMAIL_CODE_COOLDOWN_MS:
            wait = int((self.EMAIL_CODE_COOLDOWN_MS - (now - recent[0]["created_at"])) / 1000) + 1
            raise AppError(ErrorCode.RATE_LIMITED, f"验证码已发送，请 {wait} 秒后再试", details={"retry_after": wait})
        if len(recent) >= self.EMAIL_CODE_HOURLY:
            raise AppError(ErrorCode.RATE_LIMITED, "发送太频繁，请一小时后再试")
        code = f"{secrets.randbelow(1_000_000):06d}"
        row = self.db.fetchone("SELECT username, name FROM users WHERE id=?", (user_id,)) or {}
        who = row.get("name") or row.get("username") or ""
        mode = mailer.send(
            email,
            f"QuantLab 邮箱验证码：{code}",
            f"{who}，你好：\n\n你正在为 QuantLab 账号绑定邮箱 {email}。验证码：{code}\n"
            "10 分钟内有效。如果不是你本人操作，请忽略这封邮件。\n\n— QuantLab",
            f"<p>{who}，你好：</p><p>你正在为 QuantLab 账号绑定邮箱 <b>{email}</b>。</p>"
            f"<p style='font-size:24px;letter-spacing:6px'><b>{code}</b></p><p>10 分钟内有效。如果不是你本人操作，请忽略这封邮件。</p>",
        )
        # 发出去之后才记录：发信失败不占用冷却与次数
        self.db.execute(
            "INSERT INTO email_codes (id, user_id, email, purpose, code_hash, created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, user_id, email, "bind", _token_hash(f"{email}|{code}"), now, now + self.EMAIL_CODE_TTL_MS),
        )
        return {"sent": True, "email": email, "via": mode, "expires_in": self.EMAIL_CODE_TTL_MS // 1000,
                "cooldown": self.EMAIL_CODE_COOLDOWN_MS // 1000}

    def verify_email_code(self, user_id: str, email: str, code: str) -> dict[str, Any]:
        email = email.strip().lower()
        code = re.sub(r"\s", "", code or "")
        now = now_ms()
        row = self.db.fetchone(
            "SELECT * FROM email_codes WHERE user_id=? AND email=? AND used_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (user_id, email),
        )
        if row is None or row["expires_at"] < now:
            raise AppError(ErrorCode.INVALID_PARAMETER, "验证码已过期，请重新获取")
        if row["attempts"] >= self.EMAIL_CODE_ATTEMPTS:
            raise AppError(ErrorCode.RATE_LIMITED, "验证码错误次数过多，请重新获取")
        if not hmac.compare_digest(row["code_hash"], _token_hash(f"{email}|{code}")):
            self.db.execute("UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            left = self.EMAIL_CODE_ATTEMPTS - row["attempts"] - 1
            raise AppError(ErrorCode.INVALID_PARAMETER, f"验证码不正确（还可以试 {left} 次）")
        try:
            with self.db.tx() as t:
                t.execute("UPDATE email_codes SET used_at=? WHERE id=?", (now, row["id"]))
                t.execute("UPDATE users SET email=?, email_verified_at=? WHERE id=?", (email, now, user_id))
        except IntegrityError as exc:
            raise AppError(ErrorCode.CONFLICT, "该邮箱已绑定其他账号") from exc
        return self.account(user_id)

    # ---------------- 工作区与成员 ----------------
    def memberships(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT w.id, w.name, w.is_demo, w.status, m.role FROM memberships m JOIN workspaces w ON w.id = m.workspa"
            "ce_id "
            "WHERE m.user_id=? AND m.status='active' AND w.status='active' ORDER BY w.created_at",
            (user_id,),
        )
        return [{**r, "is_demo": bool(r["is_demo"])} for r in rows]

    def role_in(self, user_id: str, workspace_id: str) -> str | None:
        row = self.db.fetchone(
            "SELECT m.role FROM memberships m JOIN workspaces w ON w.id=m.workspace_id "
            "WHERE m.user_id=? AND m.workspace_id=? AND m.status='active' AND w.status='active'",
            (user_id, workspace_id),
        )
        return row["role"] if row else None

    def workspace(self, workspace_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM workspaces WHERE id=?", (workspace_id,))
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "工作区不存在")
        settings = jload(row["settings"], {}) or {}
        quotas = {**DEFAULT_QUOTAS, **(settings.get("quotas") or {})}
        return {**row, "is_demo": bool(row["is_demo"]), "settings": {**DEFAULT_SETTINGS, **settings, "quotas": quotas}}

    def update_settings(self, workspace_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        ws = self.workspace(workspace_id)
        settings = ws["settings"]
        if "timezone" in patch:
            settings["timezone"] = str(patch["timezone"])[:64]
        if "quotas" in patch:
            q = dict(settings["quotas"])
            for k, v in (patch["quotas"] or {}).items():
                if k not in DEFAULT_QUOTAS:
                    raise AppError(ErrorCode.INVALID_PARAMETER, f"未知的配额项 {k}")
                if isinstance(DEFAULT_QUOTAS[k], bool):
                    q[k] = bool(v)
                else:
                    n = int(v)
                    if n < 0:
                        raise AppError(ErrorCode.INVALID_PARAMETER, f"配额 {k} 不能为负数")
                    q[k] = n
            settings["quotas"] = q
        if "name" in patch:
            name = str(patch["name"]).strip()[:60]
            if name:
                self.db.execute("UPDATE workspaces SET name=? WHERE id=?", (name, workspace_id))
        self.db.execute("UPDATE workspaces SET settings=? WHERE id=?", (jdump(settings), workspace_id))
        return self.workspace(workspace_id)

    def members(self, workspace_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT u.id AS user_id, u.email, u.username, u.name, m.role, m.status, m.created_at, u.last_login_at "
            "FROM memberships m JOIN users u ON u.id = m.user_id WHERE m.workspace_id=? ORDER BY m.created_at",
            (workspace_id,),
        )
        return [{**r, "email": public_email(r["email"]), "username": r.get("username") or ""} for r in rows]

    def set_role(self, workspace_id: str, user_id: str, role: str) -> None:
        if role not in ROLES:
            raise AppError(ErrorCode.INVALID_PARAMETER, f"角色只能是 {', '.join(ROLES)}")
        with self.db.tx() as t:
            current = t.fetchone(
                "SELECT role FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)
            )
            if current is None:
                raise AppError(ErrorCode.NOT_FOUND, "成员不存在")
            if current["role"] == "admin" and role != "admin":
                admins = t.scalar(
                    "SELECT COUNT(*) AS n FROM memberships WHERE workspace_id=? AND role='admin' AND status='active'",
                    (workspace_id,),
                )
                if int(admins or 0) <= 1:
                    raise AppError(ErrorCode.CONFLICT, "工作区至少要保留一名管理员")
            t.execute("UPDATE memberships SET role=? WHERE workspace_id=? AND user_id=?", (role, workspace_id, user_id))

    def remove_member(self, workspace_id: str, user_id: str) -> None:
        with self.db.tx() as t:
            current = t.fetchone(
                "SELECT role FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)
            )
            if current is None:
                raise AppError(ErrorCode.NOT_FOUND, "成员不存在")
            if current["role"] == "admin":
                admins = t.scalar(
                    "SELECT COUNT(*) AS n FROM memberships WHERE workspace_id=? AND role='admin' AND status='active'",
                    (workspace_id,),
                )
                if int(admins or 0) <= 1:
                    raise AppError(ErrorCode.CONFLICT, "不能移除最后一名管理员")
            t.execute(
                "UPDATE memberships SET status='removed' WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)
            )

    def create_invite(
        self, *, workspace_id: str, role: str, created_by: str, email: str | None = None
    ) -> dict[str, Any]:
        if role not in ROLES:
            raise AppError(ErrorCode.INVALID_PARAMETER, f"角色只能是 {', '.join(ROLES)}")
        code = secrets.token_urlsafe(12)
        invite_id = uuid.uuid4().hex[:12]
        now = now_ms()
        self.db.execute(
            "INSERT INTO invites (id, code_hash, workspace_id, role, email, created_by, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                invite_id,
                _token_hash(code),
                workspace_id,
                role,
                (email or "").strip().lower() or None,
                created_by,
                now,
                now + INVITE_TTL_MS,
            ),
        )
        # 明文邀请码只在创建时返回一次
        return {"id": invite_id, "code": code, "role": role, "email": email, "expires_at": now + INVITE_TTL_MS}

    def invites(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT id, role, email, created_at, expires_at, used_at, revoked_at FROM invites WHERE workspace_id=? "
            "ORDER BY created_at DESC",
            (workspace_id,),
        )

    def revoke_invite(self, workspace_id: str, invite_id: str) -> None:
        n = self.db.execute(
            "UPDATE invites SET revoked_at=? WHERE id=? AND workspace_id=? AND used_at IS NULL",
            (now_ms(), invite_id, workspace_id),
        )
        if not n:
            raise AppError(ErrorCode.NOT_FOUND, "邀请不存在或已使用")

    def join_with_invite(self, user_id: str, code: str) -> str:
        now = now_ms()
        with self.db.tx() as t:
            inv = t.fetchone(
                "SELECT * FROM invites WHERE code_hash=? AND used_at IS NULL AND revoked_at IS NULL",
                (_token_hash(code.strip()),),
            )
            if inv is None or inv["expires_at"] < now:
                raise AppError(ErrorCode.PERMISSION_DENIED, "邀请码无效或已过期")
            existing = t.fetchone(
                "SELECT status FROM memberships WHERE workspace_id=? AND user_id=?", (inv["workspace_id"], user_id)
            )
            if existing:
                t.execute(
                    "UPDATE memberships SET status='active', role=? WHERE workspace_id=? AND user_id=?",
                    (inv["role"], inv["workspace_id"], user_id),
                )
            else:
                t.execute(
                    "INSERT INTO memberships (workspace_id, user_id, role, created_at) VALUES (?,?,?,?)",
                    (inv["workspace_id"], user_id, inv["role"], now),
                )
            t.execute("UPDATE invites SET used_by=?, used_at=? WHERE id=?", (user_id, now, inv["id"]))
            return inv["workspace_id"]


def ms_to_s(ms: int | None) -> float | None:
    return ms / 1000 if ms else None


def now_s() -> float:
    return time.time()
