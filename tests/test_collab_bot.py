"""QuantLab Bot：群组与策略广场的提及回复、权限和计费归属。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult
from quant_agent.platform import context
from quant_agent.platform.collab_bot import BOT_NAME


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
    for key, email, name in (("a", "a@example.com", "研究员甲"), ("b", "b@example.com", "研究员乙")):
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        c = TestClient(app)
        me = login(c, email=email, password="Member01!", name=name, invite_code=code)
        users[key] = (c, me["user"]["id"])
    return users


def _fake_router(seen: dict):
    def chat(messages, **kw):
        seen["user_id"] = context.current().user_id
        seen["role"] = context.current().role
        seen["system"] = messages[0].content
        seen["user"] = messages[-1].content
        return ChatResult(
            content="# 先说结论\n样本外先看，别只盯收益。 :qbot-think: :qbot-ok:",
            model="chat-test",
            provider="fake",
            usage={"input_tokens": 12, "output_tokens": 8},
        )

    get_services().router.chat = chat  # type: ignore[method-assign]


def test_group_bot_config_and_mention_billing(app):
    users = _team(app)
    a, a_id = users["a"]
    b, _ = users["b"]

    g = a.post("/api/collab/groups", json={"name": "Bot 群", "visibility": "open"}).json()["group"]
    a.post(f"/api/collab/groups/{g['id']}/members", json={"user_ids": [users["b"][1]]})

    r = a.put(
        f"/api/collab/groups/{g['id']}/bot",
        json={
            "enabled": True,
            "model_target": "",
            "system_prompt": "回答要短。",
            "personality": "direct",
            "qmoji_enabled": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["bot"]["created_by"] == a_id
    detail = a.get(f"/api/collab/groups/{g['id']}").json()["group"]
    bots = [m for m in detail["members"] if m.get("kind") == "bot"]
    assert bots and bots[0]["name"] == BOT_NAME

    # 普通成员不能改配置
    assert (
        b.put(
            f"/api/collab/groups/{g['id']}/bot",
            json={
                "enabled": False,
                "model_target": "",
                "system_prompt": "",
                "personality": "friendly",
                "qmoji_enabled": False,
            },
        ).status_code
        == 403
    )

    seen: dict = {}
    _fake_router(seen)
    sent = b.post(
        f"/api/collab/groups/{g['id']}/messages", json={"body": "@QuantLab Bot @QuantLab Bot 这组参数能上吗？"}
    )
    assert sent.status_code == 200, sent.text
    assert sent.json()["bot_message"] is None
    messages = b.get(f"/api/collab/groups/{g['id']}/messages").json()["messages"]
    bot_msg = next(m for m in messages if m["author_kind"] == "agent")
    assert len([m for m in messages if m["author_kind"] == "agent"]) == 1
    assert bot_msg["author_kind"] == "agent" and bot_msg["author_name"] == BOT_NAME
    assert "#" not in bot_msg["body"] and bot_msg["body"].count(":qbot-") == 1
    # 计费固定记在首次添加 Bot 的管理员（A）名下
    assert seen["user_id"] == a_id
    assert "回答要短" in seen["system"] and "性格：直接" in seen["system"]
    assert ":qbot-hi:" in seen["system"]


def test_square_bot_mention_bills_triggering_user(app):
    users = _team(app)
    a, a_id = users["a"]
    post = a.post("/api/community/posts", json={"type": "article", "title": "样本外讨论", "body": "测试正文"}).json()[
        "post"
    ]
    seen: dict = {}
    _fake_router(seen)
    r = a.post(
        f"/api/community/posts/{post['id']}/comments",
        json={"body": "@QuantLab Bot 这个假设怎么验证？"},
    )
    assert r.status_code == 200, r.text
    comment = r.json()["comment"]
    bot = r.json()["bot_comment"]
    assert bot["author"] == BOT_NAME and bot["is_ai"] is True
    assert bot["parent_id"] == comment["id"]
    assert seen["user_id"] == a_id
    assert seen["role"] == "member"


def test_group_bot_is_scheduled_after_user_message(app, monkeypatch):
    from fastapi import BackgroundTasks

    from quant_agent.api import routes_collab

    users = _team(app)
    a, _ = users["a"]
    g = a.post("/api/collab/groups", json={"name": "异步回复"}).json()["group"]
    queued = []
    monkeypatch.setattr(
        BackgroundTasks, "add_task", lambda self, fn, *args, **kwargs: queued.append((fn, args, kwargs))
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Bot must not run before the message response")

    monkeypatch.setattr(routes_collab, "_reply_as_group_bot", forbidden)
    sent = a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "@QuantLab Bot @QuantLab Bot 请回答"})
    assert sent.status_code == 200, sent.text
    assert sent.json()["message"]["body"] and sent.json()["bot_message"] is None
    assert len(queued) == 1
    second = a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "无需等待，继续讨论"})
    assert second.status_code == 200
    assert len(queued) == 1
