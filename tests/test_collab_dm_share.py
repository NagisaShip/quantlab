"""私聊、分享到群组 / 私聊 / 广场、在线状态。"""

import pytest
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture()
def app(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def test_dm_share_and_presence(app):
    a = TestClient(app, headers={"X-Requested-With": "pytest"})
    assert a.post("/api/auth/register", json={"username": "alice", "password": "secret123abc"}).status_code == 200
    a.headers["X-Workspace-Id"] = a.get("/api/auth/me").json()["workspace"]["id"]
    inv = a.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    b = TestClient(app, headers={"X-Requested-With": "pytest"})
    assert (
        b.post(
            "/api/auth/register", json={"username": "bob", "password": "secret123abc", "invite_code": inv}
        ).status_code
        == 200
    )
    bob_id = b.get("/api/auth/me").json()["user"]["id"]
    ws = b.get("/api/auth/me").json()["workspace"]["id"]
    for c in (a, b):
        c.headers["X-Workspace-Id"] = ws

    # 成员搜索（按用户名）
    found = a.get("/api/collab/members", params={"q": "bo"}).json()["members"]
    assert [m["username"] for m in found] == ["bob"]

    # 私聊：同一对人只有一个会话；对方收到 dm 通知
    g1 = a.post("/api/collab/dm", json={"user_id": bob_id}).json()["group"]
    g2 = a.post("/api/collab/dm", json={"user_id": bob_id}).json()["group"]
    assert g1["id"] == g2["id"] and g1["kind"] == "dm" and g1["peer"]["name"] == "bob"
    a.post(f"/api/collab/groups/{g1['id']}/messages", json={"body": "你好"})
    summary = b.get("/api/inbox/summary").json()
    assert summary["unread"].get("dm") == 1 and summary["group_unread"] == 1

    # 分享站内页面到私聊与广场
    r = a.post(
        "/api/collab/share-to",
        json={
            "target": "dm",
            "target_id": bob_id,
            "ref": {"type": "link", "id": "factors/abc", "title": "动量因子"},
            "note": "看看",
        },
    )
    assert r.status_code == 200, r.text
    msgs = b.get(f"/api/collab/groups/{g1['id']}/messages").json()["messages"]
    assert msgs[-1]["refs"][0]["type"] == "link"
    bad = a.post(
        "/api/collab/share-to",
        json={
            "target": "dm",
            "target_id": bob_id,
            "ref": {"type": "link", "id": "https://evil.example/x", "title": "x"},
        },
    )
    assert bad.status_code == 404
    sq = a.post(
        "/api/collab/share-to",
        json={"target": "square", "ref": {"type": "link", "id": "factors/abc", "title": "动量因子"}},
    )
    assert sq.status_code == 200 and sq.json()["link"].startswith("post/")

    # 在线状态：bob 看到 alice 正在编辑
    a.post("/api/collab/presence", json={"object_type": "strategy", "object_id": "s1", "editing": True})
    others = b.post("/api/collab/presence", json={"object_type": "strategy", "object_id": "s1"}).json()["others"]
    assert others == [{"user_id": a.get("/api/auth/me").json()["user"]["id"], "name": "alice", "editing": True}]
