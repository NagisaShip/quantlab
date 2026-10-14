"""策略广场：发帖、附策略与回测快照、点赞收藏、评论回复、采纳、克隆、AI 审稿、排序与标签。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _strategy_and_backtest(client):
    s = client.post("/api/research/strategies", json={"name": "广场策略", "template_id": "ma_cross"}).json()["strategy"]
    bt = client.post(
        "/api/research/backtests", json={"slug": s["slug"], "source": "synthetic", "bars_limit": 500, "warmup_bars": 60}
    ).json()
    return s, bt["id"]


def test_seeded_posts_and_pinned_first(client):
    body = client.get("/api/community/posts").json()
    assert body["total"] >= 4
    assert body["items"][0]["pinned"]
    assert "body" not in body["items"][0]
    assert any(t["tag"] == "入门" for t in client.get("/api/community/tags").json()["hot"])


def test_strategy_post_freezes_code_and_backtest(client):
    s, bt_id = _strategy_and_backtest(client)
    r = client.post(
        "/api/community/posts",
        json={
            "type": "article",
            "title": "我的均线策略",
            "body": "思路……",
            "tags": ["趋势", "趋势", " 入门 "],
            "strategy_slug": s["slug"],
            "backtest_id": bt_id,
        },
    )
    assert r.status_code == 200, r.text
    post = r.json()["post"]
    assert post["type"] == "strategy"
    assert post["tags"] == ["趋势", "入门"]
    assert post["strategy"]["code_sha256"] and post["backtest"]["metrics"]["sharpe"] is not None
    assert len(post["backtest"]["equity"]) <= 162
    # 作者事后修改策略，帖子里的代码不变
    client.post(f"/api/research/strategies/{s['slug']}/apply-params", json={"params": {"fast": 5}})
    detail = client.get(f"/api/community/posts/{post['id']}").json()["post"]
    assert detail["strategy"]["code"] == post["strategy"]["code"]
    assert detail["views"] == 1


def test_strategy_post_requires_attachment(client):
    r = client.post("/api/community/posts", json={"type": "strategy", "title": "空的分享"})
    assert r.status_code == 400


def test_like_favorite_toggle_and_filters(client):
    pid = client.post("/api/community/posts", json={"title": "一篇文章", "tags": ["心得"]}).json()["post"]["id"]
    assert client.post(f"/api/community/posts/{pid}/like").json() == {"on": True, "count": 1}
    assert client.post(f"/api/community/posts/{pid}/like").json() == {"on": False, "count": 0}
    client.post(f"/api/community/posts/{pid}/favorite")
    fav = client.get("/api/community/posts?favorited=true").json()["items"]
    assert [p["id"] for p in fav] == [pid]
    assert client.get("/api/community/posts?tag=心得").json()["total"] == 1
    assert client.get("/api/community/posts?q=一篇文章").json()["total"] == 1
    mine = client.get("/api/community/posts?mine=true").json()["items"]
    assert [p["id"] for p in mine] == [pid]


def test_comments_replies_and_accept(client):
    q = {"type": "question", "title": "均线周期怎么选？"}
    pid = client.post("/api/community/posts", json=q).json()["post"]["id"]
    c1 = client.post(f"/api/community/posts/{pid}/comments", json={"body": "看样本外"}).json()["comment"]
    c2 = client.post(f"/api/community/posts/{pid}/comments", json={"body": "同意", "parent_id": c1["id"]}).json()
    assert c2["comment"]["parent_id"] == c1["id"]
    assert client.post(f"/api/community/posts/{pid}/accept/{c1['id']}").json()["accepted_comment"] == c1["id"]
    detail = client.get(f"/api/community/posts/{pid}").json()
    assert detail["post"]["comment_count"] == 2 and len(detail["comments"]) == 2
    assert client.post(f"/api/community/comments/{c1['id']}/like").json()["count"] == 1
    assert client.delete(f"/api/community/comments/{c2['comment']['id']}").json()["ok"]
    bad = client.post(f"/api/community/posts/{pid}/comments", json={"body": "x", "parent_id": "nope"})
    assert bad.status_code == 404


def test_clone_and_ai_review(client):
    s, bt_id = _strategy_and_backtest(client)
    pid = client.post("/api/community/posts", json={"title": "可克隆", "backtest_id": bt_id, "ai_review": True}).json()[
        "post"
    ]["id"]
    detail = client.get(f"/api/community/posts/{pid}").json()
    ai = [c for c in detail["comments"] if c["is_ai"]]
    assert ai and "可信度" in ai[0]["body"] and ai[0]["meta"]["mode"] == "fallback"
    cloned = client.post(f"/api/community/posts/{pid}/clone").json()["strategy"]
    assert cloned["source"] == "community" and cloned["slug"] != s["slug"]
    assert client.get(f"/api/community/posts/{pid}").json()["post"]["clones"] == 1


def test_profile_and_permissions(client):
    pid = client.post("/api/community/posts", json={"title": "张三的帖子"}).json()["post"]["id"]
    # 改名不改变身份：作者身份来自服务端会话，不是显示名
    assert client.put("/api/community/profile", json={"name": "李四"}).json()["profile"]["name"] == "李四"
    assert client.put(f"/api/community/posts/{pid}", json={"title": "改标题"}).status_code == 200
    assert client.put("/api/community/profile", json={"name": "AI 审稿人"}).status_code == 400

    # 同工作区的普通成员不能编辑 / 删除别人的帖子，也不能设精华
    code = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    from fastapi.testclient import TestClient

    with TestClient(client.app) as other:
        login(other, email="member@example.com", password="Member123!", name="成员", invite_code=code)
        assert other.put(f"/api/community/posts/{pid}", json={"title": "篡改"}).status_code == 403
        assert other.delete(f"/api/community/posts/{pid}").status_code == 403
        assert other.post(f"/api/community/posts/{pid}/feature").status_code == 403
        assert other.post(f"/api/community/posts/{pid}/like").json()["count"] == 1
    stats = client.get("/api/community/stats").json()
    assert stats["counts"]["posts"] >= 5
    assert all(r["author"] not in ("平台小助手", "AI 审稿人") for r in stats["ranking"])


def test_sorting_modes(client):
    for sort in ("recommend", "latest", "reply", "hot"):
        assert client.get(f"/api/community/posts?sort={sort}").status_code == 200
    assert client.get("/api/community/posts?sort=bogus").status_code == 422
