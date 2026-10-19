"""回复默认 @、互关自动建私聊、个人主页展示开关。

这三件事都属于「社交默认行为」：用户不需要额外操作，回复就带上 @对方的引用，
互相关注就有一条现成的二人对话，主页上展示哪些研究成果由本人决定。
"""

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


def _team(app):
    admin = TestClient(app)
    login(admin)
    users = {"admin": (admin, admin.get("/api/auth/me").json()["user"]["id"])}
    for key, email, name in (
        ("a", "mention-a@example.com", "研究员甲"),
        ("b", "mention-b@example.com", "研究员乙"),
    ):
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        c = TestClient(app)
        me = login(c, email=email, password="Member01!", name=name, invite_code=code)
        users[key] = (c, me["user"]["id"])
    return users


def test_group_reply_defaults_to_mention(app):
    """群回复默认带 @对方；私聊和回复 Bot 时不加。"""
    users = _team(app)
    a, _a_id = users["a"]
    b, b_id = users["b"]

    g = a.post("/api/collab/groups", json={"name": "趋势小组", "visibility": "private"}).json()["group"]
    a.post(f"/api/collab/groups/{g['id']}/members", json={"user_ids": [b_id]})
    first = b.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "这条回测的换手是不是偏高？"}).json()[
        "message"
    ]

    reply = a.post(
        f"/api/collab/groups/{g['id']}/messages", json={"body": "偏高，但成本已经算进去了", "reply_to": first["id"]}
    )
    assert reply.status_code == 200, reply.text
    body = reply.json()["message"]["body"]
    assert body.startswith("@研究员乙 "), body

    # 已经带 @ 的正文不会被重复加前缀
    again = b.post(
        f"/api/collab/groups/{g['id']}/messages",
        json={"body": "@研究员甲 收到", "reply_to": reply.json()["message"]["id"]},
    ).json()["message"]
    assert again["body"] == "@研究员甲 收到"

    # 私聊是两人对话，不加 @ 前缀
    dm = b.post("/api/collab/dm", json={"user_id": _a_id}).json()["group"]
    dm_first = a.post(f"/api/collab/groups/{dm['id']}/messages", json={"body": "在吗"}).json()["message"]
    dm_reply = b.post(
        f"/api/collab/groups/{dm['id']}/messages", json={"body": "在", "reply_to": dm_first["id"]}
    ).json()["message"]
    assert dm_reply["body"] == "在"


def test_mutual_follow_creates_dm_and_mutual_list(app):
    users = _team(app)
    a, a_id = users["a"]
    b, b_id = users["b"]

    first = a.post(f"/api/community/users/{b_id}/follow").json()
    assert first["following"] is True and first["mutual"] is False and first["dm_group_id"] == ""
    assert a.get("/api/community/mutuals").json()["users"] == []

    back = b.post(f"/api/community/users/{a_id}/follow").json()
    assert back["mutual"] is True
    assert back["dm_group_id"], back

    # 互关后双方各有一份现成的二人对话
    groups = {g["id"]: g for g in a.get("/api/collab/groups").json()["groups"]}
    assert back["dm_group_id"] in groups and groups[back["dm_group_id"]]["kind"] == "dm"

    mine = a.get("/api/community/mutuals").json()["users"]
    assert [u["id"] for u in mine] == [b_id]
    assert [u["id"] for u in b.get("/api/community/mutuals").json()["users"]] == [a_id]
    assert a.get(f"/api/community/users/{b_id}").json()["is_mutual"] is True


def test_profile_showcase_can_be_edited_and_hidden(app):
    users = _team(app)
    a, a_id = users["a"]
    b, _b_id = users["b"]

    base = a.get(f"/api/community/users/{a_id}").json()
    assert base["user"]["showcase"]["limit"] == 5
    assert [i["key"] for i in base["user"]["showcase"]["items"]] == [
        "factors",
        "strategies",
        "backtests",
        "models",
        "paper",
    ]

    saved = a.put(
        "/api/community/profile",
        json={
            "name": "研究员甲",
            "bio": "只展示研究资产",
            "dm_policy": "reply_only",
            "avatar_color": "#2E5C9A",
            "public_profile": True,
            "showcase": {"items": [{"key": "paper", "show": False}, {"key": "factors", "show": True}], "limit": 3},
        },
    )
    assert saved.status_code == 200, saved.text

    view = a.get(f"/api/community/users/{a_id}/showcase").json()
    assert view["is_me"] is True
    assert [s["key"] for s in view["sections"]] == ["factors", "strategies", "backtests", "models"]
    assert view["showcase"]["limit"] == 3
    assert all(s["count"] == 0 and s["items"] == [] for s in view["sections"])
    assert "paper" in {i["key"] for i in view["showcase"]["items"]}

    other = b.get(f"/api/community/users/{a_id}/showcase").json()
    assert other["is_me"] is False

    # 不带 showcase 的保存不会把已有配置清空
    a.put(
        "/api/community/profile",
        json={"name": "研究员甲", "bio": "改了简介", "dm_policy": "open", "avatar_color": "#2E5C9A"},
    )
    kept = a.get(f"/api/community/users/{a_id}").json()["user"]["showcase"]
    assert kept["limit"] == 3 and kept["items"][0]["key"] == "paper" and kept["items"][0]["show"] is False
