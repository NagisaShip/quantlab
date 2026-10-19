"""主管理员：首个成员不可降级 / 移出，部署级任命只能由主管理员执行。"""

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


def test_workspace_primary_admin_is_immutable(app):
    admin = TestClient(app)
    me = login(admin)
    primary_id = me["user"]["id"]
    assert me["user"]["is_primary_admin"] is True
    assert me["workspace"]["is_primary_admin"] is True

    code = admin.post("/api/workspace/invites", json={"role": "admin"}).json()["invite"]["code"]
    second = TestClient(app)
    second_me = login(
        second, email="second-admin@example.com", password="Admin123!", name="第二管理员", invite_code=code
    )
    assert second_me["workspace"]["role"] == "admin"
    assert second_me["workspace"]["is_primary_admin"] is False

    members = admin.get("/api/workspace/members").json()["members"]
    primary = next(m for m in members if m["user_id"] == primary_id)
    assert primary["is_primary_admin"] is True
    other = next(m for m in members if m["user_id"] == second_me["user"]["id"])
    assert other["is_primary_admin"] is False

    # 第二位管理员也不能降级或移出主管理员。
    assert second.put(f"/api/workspace/members/{primary_id}", json={"role": "member"}).status_code == 409
    assert second.delete(f"/api/workspace/members/{primary_id}").status_code == 409
    # 主管理员本人也不能把自己改成低权限 / 移出。
    assert admin.put(f"/api/workspace/members/{primary_id}", json={"role": "member"}).status_code == 409
    assert admin.delete(f"/api/workspace/members/{primary_id}").status_code == 409


def test_only_primary_deployment_admin_can_appoint_admins(app):
    admin = TestClient(app)
    me = login(admin)
    primary_id = me["user"]["id"]

    code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    other = TestClient(app)
    other_me = login(other, email="co-admin@example.com", password="Member123!", name="共同管理员", invite_code=code)
    other_id = other_me["user"]["id"]
    code2 = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    third = TestClient(app)
    third_me = login(
        third, email="third-admin@example.com", password="Member123!", name="第三管理员", invite_code=code2
    )
    third_id = third_me["user"]["id"]

    # 主管理员可以把对方授予部署管理员。
    r = admin.post(f"/api/admin/users/{other_id}/admin", json={"is_admin": True})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["is_primary_admin"] is False
    assert admin.post(f"/api/admin/users/{third_id}/admin", json={"is_admin": True}).status_code == 200

    # 但不能撤销主管理员，也不能由其他部署管理员再任命别人。
    assert admin.post(f"/api/admin/users/{primary_id}/admin", json={"is_admin": False}).status_code == 400
    assert other.post(f"/api/admin/users/{primary_id}/admin", json={"is_admin": False}).status_code == 403
    assert other.post(f"/api/admin/users/{third_id}/admin", json={"is_admin": False}).status_code == 403
    assert other.post(f"/api/admin/users/{primary_id}/ban", json={"reason": "x"}).status_code == 403
    assert other.post(f"/api/admin/users/{primary_id}/reset-password").status_code == 403


def test_co_admins_share_issue_quota_and_post_moderation(app):
    admin = TestClient(app)
    login(admin)
    admin_code = admin.post("/api/workspace/invites", json={"role": "admin"}).json()["invite"]["code"]
    member_code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    co = TestClient(app)
    login(co, email="co@example.com", password="Member123!", name="共同管理员", invite_code=admin_code)
    member = TestClient(app)
    login(member, email="reporter@example.com", password="Member123!", name="报错成员", invite_code=member_code)

    issue = member.post(
        "/api/issues",
        json={"title": "共同管理员处理 BUG", "body": "回测页报错", "category": "bug", "severity": "high"},
    ).json()["issue"]
    listing = co.get("/api/issues?scope=all").json()
    assert listing["is_staff"] is True
    assert any(i["id"] == issue["id"] for i in listing["items"])
    replied = co.post(f"/api/issues/{issue['id']}/replies", json={"body": "已看到，正在定位"})
    assert replied.status_code == 200
    assert replied.json()["issue"]["status"] == "replied"

    request = member.post(
        "/api/config/quota-requests",
        json={"metric": "tokens", "window": "day", "limit": "5000", "reason": "本周复测"},
    ).json()["request"]
    visible = co.get("/api/config/quota-requests").json()["requests"]
    assert any(r["id"] == request["id"] for r in visible)
    decided = co.post(f"/api/config/quota-requests/{request['id']}/decide", json={"approve": True, "note": "批准"})
    assert decided.status_code == 200
    assert decided.json()["request"]["status"] == "approved"

    post = member.post(
        "/api/community/posts", json={"type": "article", "title": "共同管理员可审核的帖子", "body": "正文"}
    ).json()["post"]
    assert co.post(f"/api/community/posts/{post['id']}/feature").status_code == 200
    assert co.delete(f"/api/community/posts/{post['id']}").status_code == 200
