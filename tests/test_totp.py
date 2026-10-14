"""T11 · 双因素登录（TOTP）。

验收标准（docs/VibeCoding任务包.md T11）：
- 用户可在"设置 → 账号"启用 TOTP；部署管理员可要求工作区管理员必须启用；
- TOTP 种子用 `SecretBox` 加密；恢复码只显示一次；
- 错误验证码 5 次后锁定 15 分钟；启用流程与登录流程的接口测试。

另加本仓库的硬约束：密钥与恢复码不得明文落库 / 回显、审计 details 不得含密钥、
新端点按工作区分隔。

本文件同时验证 RFC 6238 / RFC 4226 的已知测试向量——自己实现的算法必须能对上标准，
否则"能通过自己的测试"毫无意义。
"""

from __future__ import annotations

import base64
import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import totp as T
from quant_agent.platform.errors import AppError, ErrorCode

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Passw0rd!"


@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


# ---------------------------------------------------------------------------
# 一、纯算法：RFC 已知测试向量
# ---------------------------------------------------------------------------
# RFC 4226 附录 D 与 RFC 6238 附录 B 都用 ASCII 字符串 "12345678901234567890" 当密钥。
RFC_SECRET_ASCII = b"12345678901234567890"
RFC_SECRET_B32 = base64.b32encode(RFC_SECRET_ASCII).decode().rstrip("=")


def test_hotp_rfc4226_vectors():
    """RFC 4226 附录 D：计数器 0..9 的 6 位 HOTP 值。"""
    expected = [
        "755224",
        "287082",
        "359152",
        "969429",
        "338314",
        "254676",
        "287922",
        "162583",
        "399871",
        "520489",
    ]
    got = [T.hotp(RFC_SECRET_ASCII, c) for c in range(10)]
    assert got == expected, f"HOTP 与 RFC 4226 测试向量不一致：{got}"


def test_totp_rfc6238_vectors():
    """RFC 6238 附录 B：8 位、SHA-1 的 6 个时间点。"""
    vectors = [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ]
    for ts, expected in vectors:
        assert T.totp(RFC_SECRET_B32, at=ts, digits=8) == expected, f"T={ts} 与 RFC 6238 不一致"


def test_totp_defaults_are_six_digits_and_thirty_seconds():
    """默认参数必须是与主流验证器兼容的 6 位 / 30 秒 / SHA1。"""
    assert T.DIGITS == 6
    assert T.PERIOD_SECONDS == 30
    assert len(T.totp(RFC_SECRET_B32, at=59)) == 6
    # 同一个 30 秒步长内验证码必须一致，跨步长必须变化
    assert T.totp(RFC_SECRET_B32, at=60) == T.totp(RFC_SECRET_B32, at=89)
    assert T.totp(RFC_SECRET_B32, at=60) != T.totp(RFC_SECRET_B32, at=90)


def test_secret_is_base32_and_reasonably_long():
    secrets = {T.generate_secret() for _ in range(20)}
    assert len(secrets) == 20, "密钥必须每次不同"
    for s in secrets:
        assert set(s) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"), "必须是 base32 字符集"
        assert len(s) >= 26, "128 位以上的密钥长度（base32 约 26 字符起步）"


def test_normalize_secret_tolerates_verifier_formatting():
    """验证器显示密钥时常带空格 / 小写 / 缺填充，规范化后必须等价。"""
    raw = T.generate_secret()
    spaced = " ".join(raw[i : i + 4] for i in range(0, len(raw), 4)).lower()
    assert T.normalize_secret(spaced) == T.normalize_secret(raw)
    with pytest.raises(ValueError):
        T.normalize_secret("!!!not base32!!!")


# ---------------------------------------------------------------------------
# 二、纯算法：±1 步漂移
# ---------------------------------------------------------------------------
def test_drift_accepts_plus_minus_one_step_only():
    now = 1_700_000_000.0
    step = int(now // 30)
    assert T.verify(RFC_SECRET_B32, T.totp(RFC_SECRET_B32, at=(step + 0) * 30), at=now) == step
    assert T.verify(RFC_SECRET_B32, T.totp(RFC_SECRET_B32, at=(step - 1) * 30), at=now) == step - 1
    assert T.verify(RFC_SECRET_B32, T.totp(RFC_SECRET_B32, at=(step + 1) * 30), at=now) == step + 1
    # 第 2 步以外一律拒绝：漂移窗口不能无限放大
    assert T.verify(RFC_SECRET_B32, T.totp(RFC_SECRET_B32, at=(step - 2) * 30), at=now) is None
    assert T.verify(RFC_SECRET_B32, T.totp(RFC_SECRET_B32, at=(step + 2) * 30), at=now) is None


def test_verify_rejects_malformed_codes():
    now = 1_700_000_000.0
    for bad in ("", "12345", "1234567", "abcdef", "12 34 56 78", None):
        assert T.verify(RFC_SECRET_B32, bad, at=now) is None


def test_provisioning_uri_carries_required_parameters():
    secret = T.generate_secret()
    uri = T.provisioning_uri(secret, account="alice@example.com", issuer="QuantLab")
    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "issuer=QuantLab" in uri
    assert "digits=6" in uri
    assert "period=30" in uri
    assert "algorithm=SHA1" in uri


# ---------------------------------------------------------------------------
# 三、恢复码：规范化与哈希
# ---------------------------------------------------------------------------
def test_recovery_codes_are_hashed_not_stored_plain():
    codes = T.generate_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    hashes = {T.hash_recovery_code(c) for c in codes}
    assert len(hashes) == 10
    for c, h in zip(codes, hashes, strict=False):
        assert h != c, "绝不能把明文当哈希存"
        assert len(h) == 64, "SHA-256 十六进制"
    # 大小写、空格、用户抄写时加的分隔符都要能对上
    sample = codes[0]
    assert T.hash_recovery_code(sample.lower()) == T.hash_recovery_code(sample)
    assert T.hash_recovery_code(f" {sample[:4]}-{sample[4:]} ") == T.hash_recovery_code(sample)


# ---------------------------------------------------------------------------
# 四、接口：启用流程与"密钥只出现一次"
# ---------------------------------------------------------------------------
def _setup(client: TestClient) -> dict:
    r = client.post("/api/auth/totp/setup")
    assert r.status_code == 200, r.text
    return r.json()


def _enable(client: TestClient, secret: str) -> dict:
    r = client.post("/api/auth/totp/enable", json={"code": T.totp(secret)})
    assert r.status_code == 200, r.text
    return r.json()


def _wrong_code(secret: str) -> str:
    """挑一个在当前漂移窗口内一定不成立的 6 位码，避免 1/10^6 的巧合让测试偶发失败。"""
    for i in range(50):
        candidate = f"{i:06d}"
        if T.verify(secret, candidate) is None:
            return candidate
    raise AssertionError("无法构造错误验证码")


def _code(secret: str, *, offset_steps: int = 0) -> str:
    """取相对当前时间步偏移若干步的验证码。

    偏移必须留在 ±1 步以内，否则会被漂移窗口直接拒掉——那测的就不是本用例想测的东西了。
    用"下一个时间步"的码可以避开"当前步即将结束"的边界抖动。
    """
    return T.totp(secret, at=(T.step_at() + offset_steps) * T.PERIOD_SECONDS)


def _as_admin(client: TestClient, *, totp_code: str | None = None) -> None:
    """切换到管理员会话，并把目标工作区设回他自己那个。

    注意：客户端上残留的 `X-Workspace-Id` 会让中间件按"非成员"直接 403，
    所以换账号前必须先清掉这个头。
    """
    client.headers.pop("X-Workspace-Id", None)
    body = {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    if totp_code:
        body["totp_code"] = totp_code
    r = client.post("/api/auth/login", json=body)
    assert r.status_code == 200, r.text
    ws = client.get("/api/workspaces").json()["workspaces"][0]["id"]
    client.headers.update({"X-Workspace-Id": ws})


def test_setup_returns_secret_once_then_status_hides_it(client):
    """启用时返回 otpauth 链接与密钥（用户要扫码）；之后任何查询都不得回显。"""
    body = _setup(client)
    secret = body["secret"]
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert secret in body["otpauth_uri"]
    assert body["digits"] == 6 and body["period"] == 30

    # 还没确认：状态必须是"未启用"
    status = client.get("/api/auth/totp")
    assert status.status_code == 200
    assert status.json()["totp"]["enabled"] is False
    assert secret not in status.text, "状态接口不得回显密钥"

    # 确认后同样不得回显密钥
    _enable(client, secret)
    after = client.get("/api/auth/totp")
    assert after.json()["totp"]["enabled"] is True
    assert after.json()["totp"]["recovery_codes_left"] == 10
    assert secret not in after.text
    # 再调一次 setup 必须被拒（否则等于无限次兑换密钥）
    assert client.post("/api/auth/totp/setup").status_code == 409


def test_confirm_requires_a_real_code(client):
    secret = _setup(client)["secret"]
    r = client.post("/api/auth/totp/enable", json={"code": _wrong_code(secret)})
    assert r.status_code == 401
    assert r.json()["code"] == "UNAUTHENTICATED"
    # 没确认成功就不能算启用
    assert client.get("/api/auth/totp").json()["totp"]["enabled"] is False
    # 启用前也不能关闭
    r = client.post("/api/auth/totp/disable", json={"password": ADMIN_PASSWORD, "code": "000000"})
    assert r.json()["code"] == "INVALID_PARAMETER"


def test_recovery_codes_returned_once_and_never_again(client):
    secret = _setup(client)["secret"]
    codes = _enable(client, secret)["recovery_codes"]
    assert len(codes) == 10
    # 状态接口只给"还剩几条"，不给明文
    body = client.get("/api/auth/totp").text
    for c in codes:
        assert c not in body, "恢复码明文不得再次回显"
    assert client.get("/api/workspace/audit").text.find(codes[0]) == -1


# ---------------------------------------------------------------------------
# 五、接口：登录流程（二次验证、错误码、重放、锁定）
# ---------------------------------------------------------------------------
def _logout(client: TestClient) -> None:
    client.post("/api/auth/logout")
    client.cookies.clear()


def _open_fresh_session() -> TestClient:
    """换一个干净的客户端：登录用例要验证"没有会话时会发生什么"。"""
    return TestClient(create_app())


def test_password_alone_does_not_issue_a_session(client):
    """核心验收：已启用 2FA 的用户，密码正确后必须再校验 TOTP 才发放会话。"""
    secret = _setup(client)["secret"]
    _enable(client, secret)
    _logout(client)

    with _open_fresh_session() as fresh:
        r = fresh.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 401, "密码正确也不能直接发放会话"
        body = r.json()
        assert body["code"] == "UNAUTHENTICATED"
        assert body["details"]["totp_required"] is True
        # 没有会话：受保护接口仍然进不去
        assert fresh.get("/api/auth/me").status_code == 401
        # 响应里不能出现密钥
        assert secret not in r.text

        # 第二步：带上验证码重发登录 → 会话
        r2 = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": T.totp(secret)},
        )
        assert r2.status_code == 200, r2.text
        me = fresh.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["email"] == ADMIN_EMAIL


def test_login_accepts_code_in_one_request(client):
    """前端也可以选择一次请求带上验证码（不需要先失败一次）。"""
    secret = _setup(client)["secret"]
    _enable(client, secret)
    _logout(client)

    with _open_fresh_session() as fresh:
        r = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": T.totp(secret)},
        )
        assert r.status_code == 200, r.text
        assert fresh.get("/api/auth/me").status_code == 200


def test_wrong_code_is_rejected_with_unauthenticated(client):
    secret = _setup(client)["secret"]
    _enable(client, secret)
    _logout(client)

    with _open_fresh_session() as fresh:
        r = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": _wrong_code(secret)},
        )
        assert r.status_code == 401
        assert r.json()["code"] == "UNAUTHENTICATED"
        assert "验证码" in r.json()["message"]
        # 错误信息里不能出现密钥
        assert secret not in r.text


def test_totp_code_cannot_be_replayed(client):
    """防重放：同一个验证码只能用一次，否则 ±1 步漂移窗口内可被反复使用。

    这里用"下一个时间步"的码：它在 ±1 漂移窗口内还能存活 60 秒以上，
    不会因为测试跑到一半跨了 30 秒边界而变成"窗口外"（那种失败测的就不是重放了）。
    """
    secret = _setup(client)["secret"]
    _enable(client, secret)
    _logout(client)

    code = _code(secret, offset_steps=1)
    with _open_fresh_session() as fresh:
        first = fresh.post(
            "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": code}
        )
        assert first.status_code == 200, first.text

    with _open_fresh_session() as replay:
        second = replay.post(
            "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": code}
        )
        assert second.status_code == 401, "同一个验证码第二次必须被拒"
        assert second.json()["details"]["reason"] == "replayed"
        assert replay.get("/api/auth/me").status_code == 401, "重放不得发放会话"

    # 比已用时间步更早的码同样不得放行（严格单调，而不是只比"是否等于上一步"）
    with _open_fresh_session() as older:
        back = older.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": _code(secret)},
        )
        assert back.status_code == 401
        assert older.get("/api/auth/me").status_code == 401


def test_second_factor_step_is_strictly_monotonic(client):
    """确定性验证时间步单调性（注入固定时钟，不依赖真实时间）。

    只判"是否等于上一步"会留下漏洞：攻击者截获第 N 步的码后，只要用户先用掉第 N+1 步，
    重放第 N 步就又通过了。这里确认第 N 步在第 N+1 步之后也被拒。
    """
    secret = _setup(client)["secret"]
    _enable(client, secret)
    auth = get_services().auth
    user_id = client.get("/api/auth/me").json()["user"]["id"]

    base_step = 20_000_000  # 固定基准，与真实时间无关
    n1 = base_step + 1
    clock = base_step * 30 + 1  # 时钟停在 base_step

    # 第 N+1 步（在 ±1 窗口内）可以成功
    assert auth.verify_second_factor(user_id, T.totp(secret, at=n1 * 30), at=clock) is True

    # 同一步重放 → 拒绝，且带上 replayed 标记
    with pytest.raises(AppError) as exc:
        auth.verify_second_factor(user_id, T.totp(secret, at=n1 * 30), at=clock)
    assert exc.value.code == ErrorCode.UNAUTHENTICATED
    assert exc.value.details["reason"] == "replayed"

    # 更早的第 N 步 → 也必须拒绝（严格单调）
    with pytest.raises(AppError) as exc2:
        auth.verify_second_factor(user_id, T.totp(secret, at=base_step * 30), at=clock)
    assert exc2.value.code == ErrorCode.UNAUTHENTICATED

    # 更新的第 N+2 步 → 可以（防重放不能把用户永久锁死）
    assert auth.verify_second_factor(user_id, T.totp(secret, at=(n1 + 1) * 30), at=(n1 + 1) * 30) is True


def test_five_wrong_codes_lock_the_account_for_fifteen_minutes(client):
    """验收：错误验证码 5 次后锁定 15 分钟。"""
    secret = _setup(client)["secret"]
    _enable(client, secret)
    _logout(client)

    wrong = _wrong_code(secret)
    with _open_fresh_session() as fresh:
        for i in range(4):
            r = fresh.post(
                "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": wrong}
            )
            assert r.status_code == 401, f"第 {i + 1} 次错误应为 401，实际 {r.status_code}"
            assert r.json()["details"]["remaining"] == 4 - i

        fifth = fresh.post(
            "/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": wrong}
        )
        assert fifth.status_code == 429, "第 5 次错误应触发锁定"
        assert fifth.json()["code"] == "RATE_LIMITED"
        assert "锁定" in fifth.json()["message"]
        locked_until = fifth.json()["details"]["locked_until"]
        # 锁 15 分钟（允许 1 分钟的实现误差）
        assert abs((locked_until - int(time.time() * 1000)) - 15 * 60 * 1000) < 60_000

        # 锁定期间即使给正确的验证码也必须拒绝
        during = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": T.totp(secret, at=time.time() + 60)},
        )
        assert during.status_code == 429
        assert fresh.get("/api/auth/me").status_code == 401

    # 锁定只针对验证码：密码本身还是对的，所以不该影响其他账号
    with _open_fresh_session() as other:
        assert other.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401


# ---------------------------------------------------------------------------
# 六、接口：恢复码一次性
# ---------------------------------------------------------------------------
def test_recovery_code_replaces_totp_and_is_single_use(client):
    secret = _setup(client)["secret"]
    codes = _enable(client, secret)["recovery_codes"]
    _logout(client)

    with _open_fresh_session() as fresh:
        r = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[0]},
        )
        assert r.status_code == 200, r.text
        assert fresh.get("/api/auth/me").status_code == 200

    # 同一个恢复码再用一次必须失败
    with _open_fresh_session() as again:
        r2 = again.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[0]},
        )
        assert r2.status_code == 401
        assert again.get("/api/auth/me").status_code == 401

    # 未用过的另一条仍然可用，且状态里的剩余数减少
    with _open_fresh_session() as third:
        r3 = third.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[1]},
        )
        assert r3.status_code == 200, r3.text
        assert third.get("/api/auth/totp").json()["totp"]["recovery_codes_left"] == 8


def test_regenerating_recovery_codes_invalidates_the_old_ones(client):
    secret = _setup(client)["secret"]
    old = _enable(client, secret)["recovery_codes"]
    r = client.post("/api/auth/totp/recovery-codes")
    assert r.status_code == 200
    new = r.json()["recovery_codes"]
    assert set(new).isdisjoint(old)
    assert client.get("/api/auth/totp").json()["totp"]["recovery_codes_left"] == 10

    _logout(client)
    with _open_fresh_session() as fresh:
        bad = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": old[0]},
        )
        assert bad.status_code == 401, "旧恢复码必须立刻失效"
        good = fresh.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": new[0]},
        )
        assert good.status_code == 200


# ---------------------------------------------------------------------------
# 七、接口：关闭
# ---------------------------------------------------------------------------
def test_disable_requires_password_and_code(client):
    secret = _setup(client)["secret"]
    _enable(client, secret)

    r = client.post("/api/auth/totp/disable", json={"password": "WrongPass1", "code": T.totp(secret)})
    assert r.status_code == 403
    assert r.json()["code"] == "PERMISSION_DENIED"

    r = client.post("/api/auth/totp/disable", json={"password": ADMIN_PASSWORD, "code": _wrong_code(secret)})
    assert r.status_code == 401

    r = client.post(
        "/api/auth/totp/disable",
        json={"password": ADMIN_PASSWORD, "code": _code(secret, offset_steps=1)},
    )
    assert r.status_code == 200, r.text
    after = client.get("/api/auth/totp").json()["totp"]
    assert after["enabled"] is False
    assert after["recovery_codes_left"] == 0
    assert after["enabled_at"] is None
    assert after["locked_until"] is None

    # 关闭后只用密码就能登录
    _logout(client)
    with _open_fresh_session() as fresh:
        r = fresh.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200
        assert fresh.get("/api/auth/me").status_code == 200


# ---------------------------------------------------------------------------
# 八、密钥与恢复码不得明文落库 / 进审计
# ---------------------------------------------------------------------------
def test_secret_and_recovery_codes_are_encrypted_at_rest(client):
    secret = _setup(client)["secret"]
    codes = _enable(client, secret)["recovery_codes"]

    db = get_services().db
    row = db.fetchone("SELECT * FROM user_totp")
    assert row is not None
    assert row["secret_enc"] != secret
    assert secret not in row["secret_enc"], "落库的必须是密文"
    # 密文必须能用 SecretBox 解回原密钥（说明是真加密，不是随手做的掩码）
    assert get_services().secrets.decrypt(row["secret_enc"]) == secret

    stored = db.fetchall("SELECT code_hash FROM totp_recovery_codes")
    assert len(stored) == 10
    hashes = {r["code_hash"] for r in stored}
    assert hashes.isdisjoint(codes), "恢复码只能存哈希"
    assert all(len(h) == 64 for h in hashes)


def test_audit_log_never_contains_totp_secrets(client):
    """审计 details 里绝不能出现密钥或恢复码明文。"""
    secret = _setup(client)["secret"]
    codes = _enable(client, secret)["recovery_codes"]
    client.post("/api/auth/totp/recovery-codes")

    body = client.get("/api/workspace/audit", params={"limit": 200}).text
    assert secret not in body
    for c in codes:
        assert c not in body
    # 但"发生过启用"这件事必须留痕
    assert "user.totp_enable" in body


# ---------------------------------------------------------------------------
# 九、跨工作区隔离
# ---------------------------------------------------------------------------
def _switch_account(client: TestClient, email: str, password: str = "Passw0rd!", *, register: bool = False) -> str:
    """换一个账号登录（必要时先注册），返回它自己的工作区 id 并设为当前目标工作区。

    换账号时必须先清掉 `X-Workspace-Id`：残留的旧工作区 id 不属于新用户，
    中间件会按"非成员"返回 403，而不是"没有目标工作区"。
    """
    client.headers.pop("X-Workspace-Id", None)
    client.cookies.clear()
    body = {"email": email, "password": password, "name": email.split("@")[0]}
    if register:
        r = client.post("/api/auth/register", json=body)
        assert r.status_code == 200, r.text
    else:
        r = client.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
    ws = client.get("/api/workspaces").json()["workspaces"][0]["id"]
    client.headers.update({"X-Workspace-Id": ws})
    return ws


def test_totp_state_is_isolated_between_workspaces(client, monkeypatch):
    """另一个工作区的用户既看不到、也改不了别人的 2FA 绑定。"""
    ws1 = client.get("/api/auth/me").json()["workspace"]["id"]
    secret = _setup(client)["secret"]
    client.post("/api/auth/totp/enable", json={"code": T.totp(secret)})

    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    ws2 = _switch_account(client, "bob@example.com", register=True)
    assert ws2 != ws1

    # B 的视角：自己没有 2FA，也看不到 A 的密钥
    b = client.get("/api/auth/totp").json()["totp"]
    assert b["enabled"] is False
    assert b["recovery_codes_left"] == 0
    assert secret not in client.get("/api/auth/totp").text

    # B 不能借自己的会话去关掉 A 的 2FA（端点只作用于调用者自己）
    r = client.post("/api/auth/totp/disable", json={"password": "Passw0rd!", "code": T.totp(secret)})
    assert r.json()["code"] == "INVALID_PARAMETER"

    # B 拿着 ws1 的 id 也进不去
    client.headers.update({"X-Workspace-Id": ws1})
    denied = client.get("/api/workspace/audit")
    assert denied.status_code == 403
    assert denied.json()["code"] == "PERMISSION_DENIED"

    # 审计留痕落在 A 自己的工作区，不串到 ws2
    client.headers.update({"X-Workspace-Id": ws2})
    assert "user.totp_enable" not in client.get("/api/workspace/audit").text
    client.headers.update({"X-Workspace-Id": ws1})
    _as_admin(client, totp_code=_code(secret, offset_steps=1))
    assert "user.totp_enable" in client.get("/api/workspace/audit").text


def test_another_user_cannot_use_someone_elses_recovery_code(client, monkeypatch):
    """A 的恢复码只对 A 生效；B 拿它登录自己的账号毫无用处。"""
    secret = _setup(client)["secret"]
    codes = client.post("/api/auth/totp/enable", json={"code": T.totp(secret)}).json()["recovery_codes"]

    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    _switch_account(client, "carol@example.com", register=True)

    # Carol 没有开 2FA，多传的恢复码不参与判定，也不该被核销
    carol_login = client.post(
        "/api/auth/login",
        json={"email": "carol@example.com", "password": "Passw0rd!", "totp_code": codes[0]},
    )
    assert carol_login.status_code == 200

    with _open_fresh_session() as fresh2:
        r2 = fresh2.post(
            "/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "totp_code": codes[0]},
        )
        assert r2.status_code == 200, "A 的恢复码不该被别人的登录消耗掉"


# ---------------------------------------------------------------------------
# 十、部署级"必须启用"开关 + 迁移
# ---------------------------------------------------------------------------
def test_required_for_admins_is_reported(client, monkeypatch):
    """部署管理员要求工作区管理员必须启用 2FA 时，接口要明确回报"待完成"。"""
    monkeypatch.setenv("QAGENT_REQUIRE_TOTP_FOR_ADMINS", "1")
    status = client.get("/api/auth/totp").json()["totp"]
    assert status["enabled"] is False
    assert status["required_for_admins"] is True
    assert status["setup_required"] is True

    secret = _setup(client)["secret"]
    client.post("/api/auth/totp/enable", json={"code": T.totp(secret)})
    after = client.get("/api/auth/totp").json()["totp"]
    assert after["enabled"] is True
    assert after["setup_required"] is False


def test_migration_created_totp_tables(tmp_path):
    """迁移必须能在 SQLite 上跑通，并建出两张表（PG 由 CI 的 QAGENT_TEST_PG 覆盖）。"""
    from quant_agent.platform.db import Database

    db = Database(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    applied = db.migrate()
    assert "0006_totp" in applied
    names = {r["name"] for r in db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("user_totp", "totp_recovery_codes"):
        assert table in names, f"缺少表 {table}"
    # 新表必须带 workspace_id（跨工作区隔离的基础）
    for table in ("user_totp", "totp_recovery_codes"):
        cols = {r["name"] for r in db.fetchall(f"PRAGMA table_info({table})")}
        assert "workspace_id" in cols, f"{table} 缺少 workspace_id"
