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


def test_private_strategy_backtest_showcase_and_stable_owner(app):
    users = _team(app)
    a, aid = users["a"]
    b, bid = users["b"]
    strategy = a.post("/api/research/strategies", json={"name": "私有成果", "template_id": "ma_cross"}).json()[
        "strategy"
    ]
    slug = strategy["slug"]
    result = a.post(
        "/api/research/backtests", json={"slug": slug, "source": "synthetic", "bars_limit": 400, "warmup_bars": 60}
    )
    assert result.status_code == 200, result.text
    bt = result.json()
    assert bt["id"]
    assert a.put(f"/api/collab/shares/strategy/{slug}", json={"visibility": "private"}).status_code == 200

    def sections(client, uid):
        r = client.get(f"/api/community/users/{uid}/showcase")
        assert r.status_code == 200, r.text
        return {s["key"]: s for s in r.json()["sections"]}

    own = sections(a, aid)
    assert own["backtests"]["items"][0]["id"] == bt["id"]
    other = sections(b, aid)
    assert other["backtests"]["count"] == 0 and other["strategies"]["count"] == 0
    assert bt["id"] not in str(other)
    # Same display name must not transfer ownership; a rename must not lose it.
    assert b.put("/api/community/profile", json={"name": "研究员甲"}).status_code == 200
    assert a.put(f"/api/collab/shares/strategy/{slug}", json={"visibility": "workspace"}).status_code == 200
    assert sections(a, bid)["strategies"]["count"] == 0
    assert a.put("/api/community/profile", json={"name": "研究员改名"}).status_code == 200
    assert sections(b, aid)["strategies"]["items"][0]["id"] == slug


def test_custom_avatar_ownership_validation_and_preservation(app):
    import base64

    users = _team(app)
    a, aid = users["a"]
    b, _ = users["b"]
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0ZkAAAAASUVORK5CYII="
    )
    up = a.post("/api/uploads?filename=avatar.png", content=image, headers={"Content-Type": "image/png"})
    assert up.status_code == 200, up.text
    url = up.json()["file"]["url"]
    assert b.put("/api/community/profile", json={"name": "乙", "avatar_url": url}).status_code == 400
    assert (
        a.put("/api/community/profile", json={"name": "甲", "avatar_url": "https://example.com/avatar.png"}).status_code
        == 400
    )
    assert a.put("/api/community/profile", json={"name": "甲", "avatar_url": url}).status_code == 200
    assert a.get("/api/auth/me").json()["user"]["avatar_url"] == url
    assert b.get(f"/api/community/users/{aid}").json()["user"]["avatar_url"] == url
    assert b.get(url).status_code == 200
    a.put("/api/community/profile", json={"name": "改名", "bio": "保留头像"})
    assert a.get("/api/community/profile").json()["profile"]["avatar_url"] == url
    a.put("/api/community/profile", json={"name": "改名", "avatar_url": ""})
    assert a.get("/api/community/profile").json()["profile"]["avatar_url"] == ""


def test_mutual_dm_revoked_by_block_and_policy(app):
    users = _team(app)
    a, aid = users["a"]
    b, bid = users["b"]
    a.post(f"/api/community/users/{bid}/follow")
    dm = b.post(f"/api/community/users/{aid}/follow").json()["dm_group_id"]
    assert a.post(f"/api/collab/groups/{dm}/messages", json={"body": "互关私聊"}).status_code == 200
    b.post(f"/api/community/users/{aid}/block")
    assert a.post(f"/api/collab/groups/{dm}/messages", json={"body": "被拉黑后"}).status_code == 403
    b.delete(f"/api/community/users/{aid}/block")
    b.put("/api/community/profile", json={"name": "乙", "dm_policy": "none"})
    assert a.post(f"/api/collab/groups/{dm}/messages", json={"body": "关闭私信后"}).status_code == 403


def test_concurrent_mutual_follow_creates_one_dm(app):
    from concurrent.futures import ThreadPoolExecutor

    users = _team(app)
    a, aid = users["a"]
    b, bid = users["b"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        fa = pool.submit(a.post, f"/api/community/users/{bid}/follow")
        fb = pool.submit(b.post, f"/api/community/users/{aid}/follow")
        replies = [fa.result(), fb.result()]
    assert all(r.status_code == 200 for r in replies)
    assert a.get("/api/community/mutuals").json()["users"][0]["id"] == bid
    from quant_agent.api.deps import get_services

    service = get_services()
    ws = a.get("/api/auth/me").json()["workspace"]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        gids = list(pool.map(lambda _: service.collab.open_dm(ws, aid, bid), range(8)))
    assert len(set(gids)) == 1
    assert len([g for g in a.get("/api/collab/groups").json()["groups"] if g["kind"] == "dm"]) == 1
    # Removing a follow does not grant an unestablished DM unlimited messages.
    a.delete(f"/api/community/users/{bid}/follow")
    assert a.get("/api/community/mutuals").json()["users"] == []
    assert a.post(f"/api/collab/groups/{gids[0]}/messages", json={"body": "第一条"}).status_code == 200
    assert a.post(f"/api/collab/groups/{gids[0]}/messages", json={"body": "未回复的第二条"}).status_code == 403


def test_showcase_migration_preserves_existing_profile(tmp_path, monkeypatch):
    import shutil

    from quant_agent.platform import db as db_module

    old = tmp_path / "old-migrations"
    old.mkdir()
    original = db_module.MIGRATIONS_DIR
    for sql in original.glob("*.sql"):
        if sql.name < "0023":
            shutil.copyfile(sql, old / sql.name)
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", old)
    db = db_module.Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    db.migrate()
    db.execute(
        "INSERT INTO community_profiles (user_id, workspace_id, bio, dm_policy, avatar_color, public_profile, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        ("legacy-user", "workspace", "已有简介", "none", "#123456", 1, 1, 2),
    )
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", original)
    applied = db.migrate()
    assert "0023_profile_showcase" in applied
    row = db.fetchone("SELECT * FROM community_profiles WHERE user_id=?", ("legacy-user",))
    assert row["bio"] == "已有简介" and row["dm_policy"] == "none" and row["avatar_color"] == "#123456"
    assert row["public_profile"] == 1 and row["showcase"] == ""
    assert db.migrate() == []
    db.reset()


def test_showcase_selects_specific_backtest_and_filters_preview(app):
    users = _team(app)
    a, aid = users["a"]
    b, bid = users["b"]
    strategy = a.post("/api/research/strategies", json={"name": "指定展示", "template_id": "ma_cross"}).json()[
        "strategy"
    ]
    bt = a.post(
        "/api/research/backtests",
        json={"slug": strategy["slug"], "source": "synthetic", "bars_limit": 400, "warmup_bars": 60},
    ).json()
    rid = bt["id"]
    # Newer rows must not displace an explicitly selected older report.
    from quant_agent.api.deps import get_services
    from quant_agent.platform import context
    from quant_agent.store import Collections

    ws = a.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws, user_id=aid):
        get_services().store.upsert(
            Collections.BACKTESTS,
            {"id": "newer-record", "created_by": aid, "slug": strategy["slug"], "created_at": 9999999999999},
        )
        get_services().store.upsert(
            Collections.BACKTESTS, {"id": "other-users-record", "created_by": bid, "created_at": 9999999999999}
        )
    settings = {
        "items": [
            {"key": "backtests", "show": True, "selection": "selected", "ids": [rid, "other-users-record", "missing"]}
        ],
        "limit": 1,
    }
    saved = a.put("/api/community/profile", json={"name": "甲", "showcase": settings})
    assert saved.status_code == 200, saved.text
    view = b.get(f"/api/community/users/{aid}/showcase").json()
    section = next(s for s in view["sections"] if s["key"] == "backtests")
    assert section["count"] == 1 and section["items"][0]["id"] == rid
    preview = section["items"][0]["preview"]
    assert 2 <= len(preview["timestamps"]) <= 240
    assert len(preview["equity"]) == len(preview["timestamps"])
    assert preview["equity"][0] == 0
    assert "code" not in preview and "fills" not in preview
    assert b.get(f"/api/community/users/{aid}/showcase?catalog=true").status_code == 403
    catalog = a.get(f"/api/community/users/{aid}/showcase?catalog=true").json()
    choices = next(s for s in catalog["sections"] if s["key"] == "backtests")["items"]
    assert {r["id"] for r in choices} == {rid, "newer-record"}
    assert not any("preview" in r for r in choices)
    assert a.put(f"/api/collab/shares/strategy/{strategy['slug']}", json={"visibility": "private"}).status_code == 200
    hidden = b.get(f"/api/community/users/{aid}/showcase").json()
    assert rid not in str(hidden) and "other-users-record" not in str(hidden)
    assert rid not in str(b.get(f"/api/community/users/{aid}").json()["user"]["showcase"])
    # Explicitly selecting nothing differs from the legacy latest-items mode.
    settings["items"][0]["ids"] = []
    a.put("/api/community/profile", json={"name": "甲", "showcase": settings})
    own = a.get(f"/api/community/users/{aid}/showcase").json()
    assert next(s for s in own["sections"] if s["key"] == "backtests")["count"] == 0
