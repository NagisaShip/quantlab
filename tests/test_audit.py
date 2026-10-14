"""T10 · 审计日志导出与保留策略。

验收标准（docs/VibeCoding任务包.md）：
1. 导出文件中以 `=` 开头的字段被中和；
2. 非管理员访问返回 `PERMISSION_DENIED`。
另加：按时间 / 操作者 / 动作筛选，超过保留期（默认 365 天）的记录归档。
"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform.audit import AuditLog, scrub
from quant_agent.platform.db import Database, now_ms


@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _db(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    db.migrate()
    return db


def _seed(db, ws="ws1"):
    """直接写审计行，便于精确控制时间与内容。

    时间刻意错开保留期：两条落在 400 天前（已过期），一条在 1 天前（未过期），
    这样归档用例能验证"只归档过期的"。
    """
    day = 86_400_000
    old = now_ms() - 400 * day
    rows = [
        ("strategy.save", "strategy", "s-1", old, "u1"),
        ("run.start", "run", "r-1", old + 1000, "u2"),
        ('=HYPERLINK("http://evil")', "strategy", "s-2", now_ms() - day, "u1"),
    ]
    for action, otype, oid, ts, uid in rows:
        db.execute(
            "INSERT INTO audit_log (ts, workspace_id, user_id, action, object_type, object_id, trace_id, ip, details) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, ws, uid, action, otype, oid, "t-1", "127.0.0.1", "{}"),
        )
    return rows


# ---------------------------------------------------------------- 筛选
def test_filter_by_action_prefix(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    log = AuditLog(db)
    items = log.list("ws1", action="run")
    assert [i["action"] for i in items] == ["run.start"]


def test_filter_by_time_range_is_half_open(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    log = AuditLog(db)
    day = 86_400_000
    cut = now_ms() - 10 * day
    recent = log.list("ws1", since=cut)
    assert [i["action"] for i in recent] == ['=HYPERLINK("http://evil")']
    # until 是开区间：把 until 设成最早那条的时间，它自己不应被包含
    oldest = log.list("ws1", limit=1000)[-1]
    assert all(i["id"] != oldest["id"] for i in log.list("ws1", until=oldest["ts"]))


def test_filter_by_user_and_workspace(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    _seed(db, ws="ws2")
    log = AuditLog(db)
    assert {i["user_id"] for i in log.list("ws1", user_id="u1")} == {"u1"}
    assert len(log.list("ws1", limit=1000)) == 3
    assert len(log.list("ws2", limit=1000)) == 3
    assert log.list("ws-none", limit=1000) == []


# ---------------------------------------------------------------- 导出中和
def test_export_neutralizes_formula_cells(tmp_path):
    """以 `=` 开头的 action 在导出 CSV 里必须被加单引号中和。"""
    db = _db(tmp_path)
    _seed(db)
    body = AuditLog(db).export_csv("ws1")
    assert "'=HYPERLINK" in body, "以 = 开头的字段必须被中和"
    # 中和后不应再出现裸的 = 开头单元格
    assert ",=HYPERLINK" not in body


def test_export_has_header_and_human_time(tmp_path):
    db = _db(tmp_path)
    _seed(db)
    body = AuditLog(db).export_csv("ws1")
    header = body.lstrip("\ufeff").splitlines()[0]
    for col in ("id", "time", "action", "user_name", "details"):
        assert col in header
    assert "T" not in body.splitlines()[1].split(",")[2], "time 应是可读的 UTC 字符串"


def test_export_details_are_scrubbed(tmp_path):
    """导出不得二次泄露凭证：details 里的密钥键在写入时就已掩码。"""
    db = _db(tmp_path)
    db.execute(
        "INSERT INTO audit_log (ts, workspace_id, user_id, action, object_type, object_id, trace_id, ip, details) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (now_ms(), "ws1", "u1", "account.create", "account", "a-1", "t", "ip", scrub({"api_key": "sk-live-SECRET"})),
    )
    body = AuditLog(db).export_csv("ws1")
    assert "sk-live-SECRET" not in body
    assert "***" in body


# ---------------------------------------------------------------- 归档
def test_archive_moves_expired_rows_and_keeps_them(tmp_path):
    """超过保留期的记录进归档表，仍可查回——归档不是删除。"""
    db = _db(tmp_path)
    _seed(db)
    log = AuditLog(db)
    before = len(log.list("ws1", limit=1000))
    res = log.archive_expired("ws1", retention_days=365)
    assert res["archived"] == 2  # 400 天前那两条过期，1 天前那条留下
    after = log.list("ws1", limit=1000)
    assert len(after) == before - 2
    assert all(i["action"] != "strategy.save" for i in after)
    archived = log.list_archived("ws1")
    assert sorted(a["action"] for a in archived) == ["run.start", "strategy.save"]


def test_archive_respects_workspace(tmp_path):
    db = _db(tmp_path)
    _seed(db, ws="ws1")
    _seed(db, ws="ws2")
    log = AuditLog(db)
    log.archive_expired("ws1", retention_days=365)
    assert len(log.list("ws2", limit=1000)) == 3, "另一个工作区不应被归档"
    assert log.list_archived("ws2") == []


def test_archive_rejects_non_positive_retention(tmp_path):
    db = _db(tmp_path)
    log = AuditLog(db)
    for bad in (0, -1):
        try:
            log.archive_expired("ws1", retention_days=bad)
        except ValueError:
            continue
        raise AssertionError("保留天数必须为正")


# ---------------------------------------------------------------- 接口与权限
def test_audit_endpoints_require_admin(client):
    """非管理员（reviewer/member）访问导出与归档必须被拒绝。"""
    login(client)
    me = client.get("/api/auth/me").json()
    ws = me["workspace"]["id"]
    # 邀请一个 reviewer 并以其身份登录
    inv = client.post("/api/workspace/invites", json={"role": "reviewer", "email": "rev@example.com"}).json()
    code = inv.get("code") or inv.get("invite", {}).get("code")
    assert code, inv
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/register",
        json={"email": "rev@example.com", "password": "Passw0rd!", "name": "审阅者", "invite_code": code},
    )
    assert r.status_code == 200, r.text
    client.headers.update({"X-Workspace-Id": ws, "X-Requested-With": "pytest"})
    for path in ("/api/workspace/audit/export", "/api/workspace/audit/archived"):
        resp = client.get(path)
        assert resp.status_code == 403, (path, resp.status_code)
        assert resp.json()["code"] == "PERMISSION_DENIED"
    resp = client.post("/api/workspace/audit/archive", json={"retention_days": 365})
    assert resp.status_code == 403
    assert resp.json()["code"] == "PERMISSION_DENIED"


def test_admin_can_export_and_archive(client):
    login(client)
    client.post("/api/workspace/invites", json={"role": "member", "email": "m@example.com"})
    r = client.get("/api/workspace/audit/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "id,ts,time,action" in r.text.lstrip("\ufeff")
    a = client.post("/api/workspace/audit/archive", json={"retention_days": 365})
    assert a.status_code == 200, a.text
    assert "archived" in a.json()
    assert client.get("/api/workspace/audit/archived").status_code == 200
