"""统一审批收件箱：AI 行动 + Lab 审批卡片的聚合与转发。"""

from __future__ import annotations

import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents.platform_tools import ACTIONS
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import context


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


def _user(client) -> tuple[str, str]:
    me = client.get("/api/auth/me").json()
    return me["workspace"]["id"], me["user"]["id"]


def test_approvals_empty_has_shape(client):
    r = client.get("/api/approvals")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []
    assert body["counts"] == {"ai": 0, "lab": 0}
    assert body["total"] == 0


def test_ai_action_appears_and_can_be_rejected(client):
    ws, user = _user(client)
    action = {
        "id": "act-test-1",
        "tool": "local_write_text",
        "args": {"path": "x.md", "text": "hi"},
        "summary": "写一个测试文件",
        "status": "pending",
        "created_by": user,
        "created_at": int(time.time() * 1000),
    }
    with context.bind(ws):
        get_services().store.upsert(ACTIONS, action)
    body = client.get("/api/approvals?source=ai").json()
    assert body["counts"]["ai"] == 1
    item = body["items"][0]
    assert item["id"] == "ai:act-test-1"
    assert item["source"] == "ai"
    r = client.post("/api/approvals/ai:act-test-1", json={"approve": False})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "ai"
    with context.bind(ws):
        assert get_services().store.get(ACTIONS, "act-test-1")["status"] == "canceled"


def test_lab_approval_card_appears_and_bad_id_rejected(client):
    ws, _user_id = _user(client)
    p = client.post(
        "/api/lab/projects",
        json={"kind": "research", "title": "审批卡测试", "idea": "测试统一审批"},
    ).json()["project"]
    from quant_agent.api.routes_lab import _lab

    with context.bind(ws):
        lab = _lab()
        lab._ask_approval(
            p["id"],
            [{"tool": "local_write_text", "label": "写报告", "args": {"path": "a.md"}, "category": "save"}],
        )
    body = client.get("/api/approvals?source=lab").json()
    assert body["counts"]["lab"] == 1
    item = body["items"][0]
    assert item["source"] == "lab" and item["project_id"] == p["id"]
    assert "写报告" in item["summary"]
    assert client.post("/api/approvals/lab:not-a-real-id", json={"approve": False}).status_code == 400
    assert client.post("/api/approvals/lab:missing:1", json={"approve": False}).status_code == 404
