"""角色库打通：编辑内置角色（模型/努力程度/头像）、恢复默认、流水线模板、角色努力程度传给模型。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents.base import Agent
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.context.manager import ContextManager
from quant_agent.llm.base import ChatResult


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    set_services(AppServices.build(store_root=str(root)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_edit_and_reset_builtin_role(client):
    body = client.get("/api/agents/roles").json()
    assert {e["id"] for e in body["efforts"]} == {"low", "medium", "high", "max"}
    assert any(t["name"] == "market_summary" for t in body["tool_catalog"])
    base = next(r for r in body["roles"] if r["id"] == "risk_officer")
    assert base["customized"] is False

    r = client.put("/api/agents/roles/risk_officer", json={**base, "effort": "high", "avatar": "🛡", "model": "x/y"})
    assert r.status_code == 200
    role = r.json()["role"]
    assert role["effort"] == "high" and role["avatar"] == "🛡" and role["builtin"] and role["customized"]

    assert client.put("/api/agents/roles/risk_officer", json={**base, "effort": "ultra"}).status_code == 400

    r = client.post("/api/agents/roles/risk_officer/reset")
    role = r.json()["role"]
    assert role["effort"] == "" and role["model"] == "" and role["customized"] is False
    custom = client.post("/api/agents/roles", json={"name": "临时"}).json()["role"]
    assert client.post(f"/api/agents/roles/{custom['id']}/reset").status_code == 400


def test_pipeline_templates(client):
    r = client.post(
        "/api/missions/pipelines",
        json={
            "name": "我的流水线",
            "stages": [{"name": "看盘", "mode": "parallel", "role_ids": ["market_analyst", "nope"]}],
        },
    )
    assert r.status_code == 200
    pl = r.json()["pipeline"]
    assert pl["custom"] and pl["stages"][0]["role_ids"] == ["market_analyst"]
    presets = client.get("/api/missions/presets").json()
    assert any(p["id"] == pl["id"] and p["custom"] for p in presets["pipelines"])
    assert any(p["id"] == "full" and not p["custom"] for p in presets["pipelines"])
    assert "effort" in presets["roles"][0]

    r = client.post(
        "/api/missions",
        json={"directive": "测试模板", "mode": "pipeline", "preset": pl["id"], "include_market_data": False},
    )
    assert r.status_code == 200
    m = r.json()["mission"]
    assert [s["role_ids"] for s in m["stages"]] == [["market_analyst"]]
    assert "effort" in m["roster"][0]

    assert (
        client.post(
            "/api/missions/pipelines", json={"id": "full", "name": "x", "stages": [{"role_ids": ["market_analyst"]}]}
        ).status_code
        == 400
    )
    assert client.post("/api/missions/pipelines", json={"name": "空", "stages": [{"role_ids": []}]}).status_code == 400
    assert client.delete(f"/api/missions/pipelines/{pl['id']}").json()["ok"]


class _Router:
    def __init__(self, with_effort: bool):
        self.calls: list[dict] = []
        self.with_effort = with_effort

    def chat(self, messages, **kw):
        self.calls.append(kw)
        return ChatResult(content="好", provider="p", model="m")


class _OldRouter:
    def __init__(self):
        self.calls: list[dict] = []

    def chat(self, messages, *, role="default", model=None, temperature=0.2, tools=None):
        self.calls.append({"model": model})
        return ChatResult(content="好", provider="p", model="m")


def test_agent_passes_effort_only_when_supported():
    r = _Router(True)
    Agent(name="a", role="reasoning", router=r, context=ContextManager(system_prompt="s"), effort="high").run("t")
    assert r.calls[0]["effort"] == "high"
    old = _OldRouter()
    run = Agent(name="a", role="reasoning", router=old, context=ContextManager(system_prompt="s"), effort="high").run(
        "t"
    )
    assert run.status == "done" and run.final == "好"
