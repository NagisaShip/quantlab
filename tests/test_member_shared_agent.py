from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult
from quant_agent.platform import context


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def test_member_can_chat_with_agent_using_workspace_shared_model(app, monkeypatch):
    """成员默认可用管理员登记的工作区共享模型，Agent 对话实际走管理员密钥。"""
    services = get_services()
    seen: list[dict[str, str]] = []

    class FakeSharedClient:
        def chat(self, messages, *, model: str, **_kwargs):
            return ChatResult(
                content="成员通过工作区共享模型完成了这次 Agent 对话。",
                model=model,
                provider="shared",
                usage={"input_tokens": 8, "output_tokens": 4},
            )

    def fake_client(provider_name: str, pc=None):
        seen.append(
            {
                "provider": provider_name,
                "scope": getattr(pc, "scope", ""),
                "owner_id": getattr(pc, "owner_id", ""),
                "api_key": getattr(pc, "api_key", ""),
            }
        )
        return FakeSharedClient()

    monkeypatch.setattr(services.router, "_client", fake_client)

    shared = {
        "name": "shared",
        "type": "openai_compatible",
        "base_url": "https://a.example/v1",
        "api_key": "sk-admin-shared",
        "models": ["m"],
        "currency": "USD",
        "fx_rate": "7",
        "model_settings": {"m": {"price_in": "2", "price_out": "8"}},
    }
    with TestClient(app) as admin:
        login(admin)
        assert admin.post("/api/config/providers", json=shared).status_code == 200
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        ws = admin.get("/api/auth/me").json()["workspace"]["id"]

    with TestClient(app) as member:
        login(member, email="agent-member@example.com", password="Member01!", name="Agent Member", invite_code=code)
        member_id = member.get("/api/auth/me").json()["user"]["id"]
        cfg = member.get("/api/config").json()
        provider = next(p for p in cfg["providers"] if p["name"] == "shared")
        assert provider["scope"] == "workspace" and provider["editable"] is False
        assert "sk-admin-shared" not in str(cfg)

        project = member.post(
            "/api/lab/projects",
            json={"kind": "strategy", "title": "成员共享模型对话", "idea": "测试 Agent 对话", "model": "shared/m"},
        ).json()["project"]
        view = member.post(
            f"/api/lab/projects/{project['id']}/messages",
            json={"text": "你好，请确认你能收到这条消息。", "model": "shared/m", "wait": True},
        ).json()
        assistant = [m for m in view["messages"] if m.get("role") == "assistant" and m.get("kind") == "text"]
        assert assistant and "工作区共享模型" in assistant[-1]["content"]
        assert assistant[-1]["model"] == "shared/m"

    assert seen and seen[-1] == {
        "provider": "shared",
        "scope": "workspace",
        "owner_id": "",
        "api_key": "sk-admin-shared",
    }
    with context.bind(ws, user_id=member_id, role="member"):
        row = services.db.fetchone(
            "SELECT user_id, scope, provider, model, input_tokens, output_tokens FROM llm_usage "
            "WHERE workspace_id=? ORDER BY created_at DESC LIMIT 1",
            (ws,),
        )
        assert row == {
            "user_id": member_id,
            "scope": "workspace",
            "provider": "shared",
            "model": "m",
            "input_tokens": 8,
            "output_tokens": 4,
        }
        stats = services.model_hub.stats(ws, days=1, user_id=member_id)["total"]
        assert stats["calls"] == 1 and stats["tokens"] == 12 and stats["cost"] == "0.0003"


def test_member_can_switch_agent_conversation_to_personal_provider(app, monkeypatch):
    """成员可添加个人服务商，并在同一 Agent 对话里切换个人 / 工作区共享模型。"""
    services = get_services()
    seen: list[dict[str, str]] = []

    class FakeClient:
        def chat(self, messages, *, model: str, **_kwargs):
            return ChatResult(
                content=f"已通过 {seen[-1]['provider']}/{model} 回答。",
                model=model,
                provider=seen[-1]["provider"],
                usage={"input_tokens": 4, "output_tokens": 2},
            )

    def fake_client(provider_name: str, pc=None):
        seen.append(
            {
                "provider": provider_name,
                "scope": getattr(pc, "scope", ""),
                "api_key": getattr(pc, "api_key", ""),
            }
        )
        return FakeClient()

    monkeypatch.setattr(services.router, "_client", fake_client)

    shared = {
        "name": "shared",
        "type": "openai_compatible",
        "base_url": "https://shared.example/v1",
        "api_key": "sk-admin-shared",
        "models": ["m"],
    }
    personal = {
        "name": "mine",
        "type": "openai_compatible",
        "base_url": "https://personal.example/v1",
        "api_key": "sk-member-personal",
        "models": ["pm"],
    }
    with TestClient(app) as admin:
        login(admin)
        assert admin.post("/api/config/providers", json=shared).status_code == 200
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        ws = admin.get("/api/auth/me").json()["workspace"]["id"]

    with TestClient(app) as member:
        login(member, email="switch-member@example.com", password="Member01!", name="Switch", invite_code=code)
        member_id = member.get("/api/auth/me").json()["user"]["id"]
        cfg = member.get("/api/config").json()
        assert cfg["me"]["allow_personal_models"] is True

        saved = member.post("/api/config/providers", json=personal).json()["provider"]
        assert saved["scope"] == "personal"
        mine = next(p for p in member.get("/api/config").json()["providers"] if p["name"] == "mine")
        assert mine["scope"] == "personal" and mine["editable"] is True
        models = member.get("/api/config/models").json()["models"]
        assert {(m["provider"], m["model"], m["scope"]) for m in models} >= {
            ("shared", "m", "workspace"),
            ("mine", "pm", "personal"),
        }

        project = member.post(
            "/api/lab/projects",
            json={"kind": "strategy", "title": "切换服务商", "idea": "测试切换", "model": "mine/pm"},
        ).json()["project"]
        first = member.post(
            f"/api/lab/projects/{project['id']}/messages",
            json={"text": "先用我的提供商。", "model": "mine/pm", "wait": True},
        ).json()
        second = member.post(
            f"/api/lab/projects/{project['id']}/messages",
            json={"text": "再切回工作区共享提供商。", "model": "shared/m", "wait": True},
        ).json()

    assert [x["provider"] for x in seen] == ["mine", "shared"]
    assert [x["api_key"] for x in seen] == ["sk-member-personal", "sk-admin-shared"]
    assert [x["scope"] for x in seen] == ["personal", "workspace"]
    for view, expected in ((first, "mine/pm"), (second, "shared/m")):
        assistant = [m for m in view["messages"] if m.get("role") == "assistant" and m.get("kind") == "text"]
        assert assistant[-1]["model"] == expected

    with context.bind(ws, user_id=member_id, role="member"):
        rows = services.db.fetchall(
            "SELECT scope, provider, model FROM llm_usage WHERE workspace_id=? ORDER BY created_at ASC",
            (ws,),
        )
        assert [(r["scope"], r["provider"], r["model"]) for r in rows] == [
            ("personal", "mine", "pm"),
            ("workspace", "shared", "m"),
        ]
