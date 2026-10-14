"""v2.0 平台基座：账号、工作区隔离、错误契约、幂等键、CSRF、角色、审计、凭证加密。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def _client(app):
    return TestClient(app)


def _assert_error(r, status: int, code: str):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"code", "message", "trace_id", "details"}, body
    assert body["code"] == code
    assert body["trace_id"] and r.headers.get("x-trace-id") == body["trace_id"]
    return body


# ---------------- 账号 ----------------
def test_bootstrap_then_controlled_registration(app):
    with _client(app) as c:
        assert c.get("/api/auth/status").json() == {"bootstrapped": False, "open_registration": False}
        me = login(c)
        assert me["user"]["is_admin"] and me["workspace"]["role"] == "admin"
        assert c.get("/api/auth/status").json()["bootstrapped"] is True
    with _client(app) as c2:
        c2.headers.update({"X-Requested-With": "t"})
        r = c2.post("/api/auth/register", json={"email": "x@y.com", "password": "Abcdef12!"})
        _assert_error(r, 403, "PERMISSION_DENIED")


def test_unauthenticated_and_bad_password(app):
    with _client(app) as c:
        login(c)
    with _client(app) as anon:
        _assert_error(anon.get("/api/research/backtests"), 401, "UNAUTHENTICATED")
        r = anon.post("/api/auth/login", json={"email": "admin@example.com", "password": "wrong-pass1"})
        _assert_error(r, 401, "UNAUTHENTICATED")


def test_weak_password_rejected(app):
    with _client(app) as c:
        c.headers.update({"X-Requested-With": "t"})
        r = c.post("/api/auth/register", json={"email": "a@b.com", "password": "12345678"})
        _assert_error(r, 400, "INVALID_PARAMETER")


def test_logout_invalidates_session(app):
    with _client(app) as c:
        login(c)
        assert c.get("/api/auth/me").status_code == 200
        c.post("/api/auth/logout")
        _assert_error(c.get("/api/auth/me"), 401, "UNAUTHENTICATED")


# ---------------- 工作区隔离（P0 必测） ----------------
def test_cross_workspace_resources_are_invisible(app, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with _client(app) as alice, _client(app) as bob:
        a = login(alice, email="alice@example.com", password="Alice123!", name="Alice")
        b = login(bob, email="bob@example.com", password="Bobby123!", name="Bob")
        assert a["workspace"]["id"] != b["workspace"]["id"]

        s = alice.post("/api/research/strategies", json={"name": "Alice 的策略", "template_id": "ma_cross"}).json()[
            "strategy"
        ]
        bt = alice.post(
            "/api/research/backtests",
            json={"slug": s["slug"], "source": "synthetic", "bars_limit": 300, "warmup_bars": 50},
        ).json()["id"]
        alice.post("/api/community/posts", json={"title": "Alice 的帖子"})

        # Bob 在自己的工作区里看不到 Alice 的任何东西
        assert all(x["slug"] != s["slug"] for x in bob.get("/api/studio/strategies").json()["strategies"])
        assert bob.get(f"/api/studio/strategies/{s['slug']}").status_code == 404
        assert bob.get(f"/api/research/backtests/{bt}").status_code == 404
        assert all("Alice" not in p["title"] for p in bob.get("/api/community/posts").json()["items"])

        # 伪造目标工作区：客户端的 workspace_id 不是授权证据
        bob.headers.update({"X-Workspace-Id": a["workspace"]["id"]})
        body = _assert_error(bob.get("/api/studio/strategies"), 403, "PERMISSION_DENIED")
        assert "无权" in body["message"]
        # 下载链接用 ?ws= 也一样要校验
        bob.headers.pop("X-Workspace-Id")
        _assert_error(
            bob.get(f"/api/studio/strategies/{s['slug']}/export?ws={a['workspace']['id']}"), 403, "PERMISSION_DENIED"
        )


def test_reviewer_is_read_only(app):
    with _client(app) as admin:
        login(admin)
        code = admin.post("/api/workspace/invites", json={"role": "reviewer"}).json()["invite"]["code"]
        admin.post("/api/research/strategies", json={"name": "共享策略", "template_id": "ma_cross"})
    with _client(app) as rev:
        me = login(rev, email="rev@example.com", password="Review123!", name="审阅者", invite_code=code)
        assert me["workspace"]["role"] == "reviewer"
        assert rev.get("/api/studio/strategies").json()["strategies"], "审阅者可以查看"
        r = rev.post("/api/research/strategies", json={"name": "x", "template_id": "ma_cross"})
        _assert_error(r, 403, "PERMISSION_DENIED")
        _assert_error(rev.get("/api/workspace/audit"), 403, "PERMISSION_DENIED")


def test_member_management_and_last_admin_guard(app):
    with _client(app) as admin:
        me = login(admin)
        code = admin.post("/api/workspace/invites", json={"role": "member", "email": "m@example.com"}).json()["invite"][
            "code"
        ]
        with _client(app) as wrong:
            wrong.headers.update({"X-Requested-With": "t"})
            r = wrong.post(
                "/api/auth/register", json={"email": "other@example.com", "password": "Other123!", "invite_code": code}
            )
            _assert_error(r, 403, "PERMISSION_DENIED")
        with _client(app) as m:
            login(m, email="m@example.com", password="Member123!", name="M", invite_code=code)
        members = admin.get("/api/workspace/members").json()["members"]
        assert {x["role"] for x in members} == {"admin", "member"}
        _assert_error(admin.put(f"/api/workspace/members/{me['user']['id']}", json={"role": "member"}), 409, "CONFLICT")
        mid = next(x["user_id"] for x in members if x["role"] == "member")
        assert admin.put(f"/api/workspace/members/{mid}", json={"role": "reviewer"}).status_code == 200


# ---------------- CSRF ----------------
def test_cookie_writes_require_custom_header(app):
    with _client(app) as c:
        login(c)
        c.headers.pop("X-Requested-With")
        r = c.post("/api/research/strategies", json={"name": "x", "template_id": "ma_cross"})
        _assert_error(r, 403, "PERMISSION_DENIED")


def test_bearer_token_auth(app):
    with _client(app) as c:
        login(c)
        token = c.post("/api/auth/login", json={"email": "admin@example.com", "password": "Passw0rd!"}).json()["token"]
    with _client(app) as api:
        r = api.post(
            "/api/research/strategies",
            json={"name": "脚本创建", "template_id": "ma_cross"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text


# ---------------- 错误契约 ----------------
def test_error_contract_shapes(app):
    with _client(app) as c:
        login(c)
        _assert_error(c.get("/api/studio/strategies/nope"), 404, "NOT_FOUND")
        body = _assert_error(c.post("/api/research/strategies", json={"name": ""}), 422, "INVALID_PARAMETER")
        assert body["details"]["fields"][0]["field"] == "name"
        _assert_error(c.get("/api/api-that-does-not-exist"), 404, "NOT_FOUND")
        body = _assert_error(
            c.post(
                "/api/research/backtests", json={"code": "import os\ndef initialize(ctx): pass", "source": "synthetic"}
            ),
            400,
            "INVALID_PARAMETER",
        )
        assert "os" in body["message"]


# ---------------- 幂等 ----------------
def test_idempotency_replay_and_conflict(app):
    with _client(app) as c:
        login(c)
        h = {"Idempotency-Key": "create-1"}
        r1 = c.post("/api/research/strategies", json={"name": "幂等", "template_id": "ma_cross"}, headers=h)
        r2 = c.post("/api/research/strategies", json={"name": "幂等", "template_id": "ma_cross"}, headers=h)
        assert r1.status_code == r2.status_code == 200
        assert r1.json()["strategy"]["slug"] == r2.json()["strategy"]["slug"]
        assert r2.headers.get("idempotent-replay") == "true"
        assert sum(1 for s in c.get("/api/studio/strategies").json()["strategies"] if s["name"] == "幂等") == 1
        r3 = c.post("/api/research/strategies", json={"name": "另一个", "template_id": "ma_cross"}, headers=h)
        _assert_error(r3, 409, "IDEMPOTENCY_CONFLICT")
        # 体内字段同样生效
        b = {"name": "体内键", "template_id": "ma_cross", "idempotency_key": "k-body"}
        assert (
            c.post("/api/research/strategies", json=b).json()["strategy"]["slug"]
            == c.post("/api/research/strategies", json=b).json()["strategy"]["slug"]
        )


# ---------------- 审计 ----------------
def test_audit_log_records_actions_without_secrets(app):
    with _client(app) as c:
        login(c)
        c.post(
            "/api/config/providers",
            json={
                "name": "myllm",
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-secret-value-123",
                "models": ["m"],
            },
        )
        c.post("/api/workspace/invites", json={"role": "member"})
        items = c.get("/api/workspace/audit").json()["items"]
        actions = {i["action"] for i in items}
        assert {"user.register", "member.invite"} <= actions
        assert "sk-secret-value-123" not in str(items)


def test_llm_secrets_are_encrypted_at_rest(app, tmp_path):
    with _client(app) as c:
        login(c)
        c.post(
            "/api/config/providers",
            json={
                "name": "myllm",
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-plain-should-not-appear",
                "models": ["m"],
            },
        )
    files = list(tmp_path.rglob("*.json"))
    assert files
    assert not any("sk-plain-should-not-appear" in f.read_text(encoding="utf-8", errors="ignore") for f in files)


def test_only_deployment_admin_changes_llm_config(app):
    with _client(app) as admin:
        login(admin)
        code = admin.post("/api/workspace/invites", json={"role": "admin"}).json()["invite"]["code"]
    with _client(app) as ws_admin:
        login(ws_admin, email="wa@example.com", password="WsAdmin1!", name="WA", invite_code=code)
        r = ws_admin.post(
            "/api/config/providers",
            json={"name": "x", "type": "openai_compatible", "base_url": "https://a.example/v1", "models": ["m"]},
        )
        _assert_error(r, 403, "PERMISSION_DENIED")
