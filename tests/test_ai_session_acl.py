"""AI 会话历史的会话级 ACL：默认只看自己，管理员可显式看工作区。"""

from __future__ import annotations

import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import context
from quant_agent.store import Collections


def _clear_keys(monkeypatch) -> None:
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _clear_keys(monkeypatch)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_ai_history_is_scoped_to_owner_and_admin_can_request_workspace(client):
    me = client.get("/api/auth/me").json()
    ws, user = me["workspace"]["id"], me["user"]["id"]
    with context.bind(ws):
        svc = get_services()
        svc.store.upsert(
            Collections.AI_SESSIONS,
            {"id": "mine", "action": "chat", "summary": "我的", "user_id": user, "created_at": time.time()},
        )
        svc.store.upsert(
            Collections.AI_SESSIONS,
            {"id": "other", "action": "chat", "summary": "别人的", "user_id": "other-user", "created_at": time.time()},
        )
        svc.store.upsert(
            Collections.AI_SESSIONS,
            {"id": "legacy", "action": "chat", "summary": "旧记录", "created_at": time.time()},
        )

    mine = client.get("/api/ai/history?scope=mine").json()
    ids = {row.get("id") for row in mine["items"]}
    assert "mine" in ids
    assert "other" not in ids
    assert mine["scope"] == "mine"

    everyone = client.get("/api/ai/history?scope=workspace")
    assert everyone.status_code == 200
    all_ids = {row.get("id") for row in everyone.json()["items"]}
    assert {"mine", "other"} <= all_ids
