"""策略广场社交：用户主页、关注、拉黑、私信首条限制和服务端强制校验。"""

from __future__ import annotations

import io

import pytest
from conftest import login
from fastapi.testclient import TestClient
from PIL import Image

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
    me = login(admin)
    users = {"admin": (admin, me["user"]["id"])}
    for key, email, name in (
        ("a", "social-a@example.com", "作者甲"),
        ("b", "social-b@example.com", "读者乙"),
        ("d", "social-d@example.com", "读者丁"),
    ):
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        c = TestClient(app)
        m = login(c, email=email, password="Member01!", name=name, invite_code=code)
        users[key] = (c, m["user"]["id"])
    return users


def test_profile_follow_dm_policy_and_block(app):
    users = _team(app)
    a, a_id = users["a"]
    b, _b_id = users["b"]
    d, _d_id = users["d"]
    _admin, admin_id = users["admin"]

    # 主页与发帖
    post = a.post(
        "/api/community/posts", json={"type": "article", "title": "社交功能测试帖", "body": "正文", "tags": ["测试"]}
    )
    assert post.status_code == 200, post.text
    pid = post.json()["post"]["id"]
    # 头像颜色与管理员保护
    assert (
        a.put(
            "/api/community/profile",
            json={"name": "作者甲", "bio": "研究", "dm_policy": "reply_only", "avatar_color": "#236B68"},
        ).status_code
        == 200
    )
    assert a.get("/api/community/profile").json()["profile"]["avatar_color"] == "#236B68"
    assert b.post(f"/api/community/users/{admin_id}/block").status_code == 400
    assert (
        b.post("/api/inbox/mutes", json={"target_type": "user", "target_id": admin_id, "label": "管理员"}).status_code
        == 403
    )
    profile = b.get(f"/api/community/users/{a_id}")
    assert profile.status_code == 200 and profile.json()["user"]["posts"] >= 1
    assert profile.json()["user"]["username"]
    assert profile.json()["can_dm"] is True

    # 关注与关注流
    follow = b.post(f"/api/community/users/{a_id}/follow")
    assert follow.status_code == 200 and follow.json()["following"] is True
    assert b.get("/api/community/users/" + a_id).json()["is_following"] is True
    feed = b.get("/api/community/posts", params={"following": True}).json()["items"]
    assert any(x["id"] == pid for x in feed)
    assert b.post(f"/api/community/posts/{pid}/favorite").json()["on"] is True
    favorites = b.get("/api/community/favorites").json()["items"]
    assert any(x["object_type"] == "post" and x["object_id"] == pid for x in favorites)

    # 默认私信策略：首条可发，对方回复前不能继续
    dm = b.post("/api/collab/dm", json={"user_id": a_id}).json()["group"]
    gid = dm["id"]
    assert b.post(f"/api/collab/groups/{gid}/messages", json={"body": "你好，想请教策略"}).status_code == 200
    second = b.post(f"/api/collab/groups/{gid}/messages", json={"body": "还在吗"})
    assert second.status_code == 403 and "只能发送一条" in second.json()["message"]
    assert a.post(f"/api/collab/groups/{gid}/messages", json={"body": "在，请说"}).status_code == 200
    assert b.post(f"/api/collab/groups/{gid}/messages", json={"body": "谢谢"}).status_code == 200

    # 关闭私信：新会话不能再发
    assert (
        a.put("/api/community/profile", json={"name": "作者甲", "bio": "研究", "dm_policy": "none"}).status_code == 200
    )
    dm2 = d.post("/api/collab/dm", json={"user_id": a_id}).json()["group"]
    denied = d.post(f"/api/collab/groups/{dm2['id']}/messages", json={"body": "想私聊"})
    assert denied.status_code == 403 and "关闭私信" in denied.json()["message"]

    # 拉黑：解除关注、隐藏帖子、阻止新关注和私信
    assert d.post(f"/api/community/users/{a_id}/block").status_code == 200
    assert b.get(f"/api/community/users/{a_id}").json()["is_following"] is True
    blocked_profile = d.get(f"/api/community/users/{a_id}").json()
    assert blocked_profile["is_blocked"] is True and blocked_profile["posts"] == []
    assert d.get("/api/community/posts").status_code == 200
    assert all(x["id"] != pid for x in d.get("/api/community/posts").json()["items"])
    assert d.post(f"/api/community/users/{a_id}/follow").status_code == 403
    assert d.post("/api/collab/dm", json={"user_id": a_id}).status_code == 403
    assert d.get(f"/api/community/posts/{pid}").status_code == 403

    # 取消拉黑后可恢复可见
    assert d.delete(f"/api/community/users/{a_id}/block").status_code == 200
    assert d.get(f"/api/community/users/{a_id}").json()["is_blocked"] is False

    # 图片上传时自动生成缩略图
    im = Image.effect_noise((1200, 800), 80).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    uploaded = b.post("/api/uploads?filename=cover.png", content=buf.getvalue(), headers={"Content-Type": "image/png"})
    assert uploaded.status_code == 200, uploaded.text
    info = uploaded.json()["file"]
    assert info["thumb_url"] and info["width"] == 1200 and info["height"] == 800
    thumb = b.get(info["thumb_url"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"].startswith("image/webp")
    assert len(thumb.content) > 0


def test_seed_guide_posts_open_with_system_author(app):
    """平台内置帖子作者是 system；详情页也必须能正常打开。"""
    with TestClient(app) as c:
        login(c)
        items = c.get("/api/community/posts").json()["items"]
        assert any(x["id"] == "seed-guide" for x in items)
        for post_id in ("seed-guide", "seed-conventions", "seed-overfit", "seed-momentum-rotation"):
            r = c.get(f"/api/community/posts/{post_id}")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["post"]["id"] == post_id
            assert body["post"]["author_role"] == "system"
            assert body["post"]["body"]
