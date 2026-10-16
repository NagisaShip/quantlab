"""指挥台：单周期 / 流水线作业、关卡、插话与追问、规则降级发言、模型发言。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_presets(client):
    body = client.get("/api/missions/presets").json()
    assert {p["id"] for p in body["pipelines"]} >= {"full", "fast", "redteam"}
    assert body["teams"] and body["roles"]


def test_single_cycle_fallback_speeches(client):
    r = client.post(
        "/api/missions",
        json={"directive": "评估 BTC 趋势策略", "mode": "single", "team_id": "adversarial_team", "wait": True},
    )
    assert r.status_code == 200, r.text
    view = r.json()
    m, feed = view["mission"], view["feed"]
    assert m["status"] == "done" and m["conclusion"]
    speeches = [u for u in feed if u["kind"] in ("speech", "challenge", "synthesis")]
    assert all(u["status"] == "done" and u["mode"] == "fallback" for u in speeches)
    assert any(u["kind"] == "challenge" and u["data"]["reply_to"] for u in speeches)
    assert speeches[-1]["kind"] == "synthesis" and "规则降级" in speeches[-1]["content"]
    assert client.get("/api/missions").json()["missions"][0]["id"] == m["id"]


def test_pipeline_gate_say_and_ask(client):
    r = client.post(
        "/api/missions",
        json={"directive": "为 ETH 设计策略", "mode": "pipeline", "preset": "full", "symbol": "ETH/USDT", "wait": True},
    ).json()
    m = r["mission"]
    assert m["status"] == "awaiting_approval" and m["cursor"] == 2
    assert [s["status"] for s in m["stages"]] == ["done", "done", "pending"]
    assert r["feed"][-1]["kind"] == "gate"
    mid = m["id"]
    # 插话（不点名）只记录
    v = client.post(f"/api/missions/{mid}/say", json={"text": "注意控制回撤"}).json()
    assert v["feed"][-1]["kind"] == "commander"
    # 点名追问：被点名角色回应，状态回到等待批准
    v = client.post(
        f"/api/missions/{mid}/say", json={"text": "止损设多少？", "to_role": "risk_officer", "wait": True}
    ).json()
    assert v["feed"][-1]["role_id"] == "risk_officer" and v["mission"]["status"] == "awaiting_approval"
    # 批准 → 跑完红队阶段并汇总
    v = client.post(f"/api/missions/{mid}/approve", json={"wait": True}).json()
    assert v["mission"]["status"] == "done" and v["mission"]["conclusion"]
    # 增量拉取
    last = v["feed"][-1]["seq"]
    assert client.get(f"/api/missions/{mid}?after={last}").json()["feed"] == []


def test_llm_speeches_see_commander_notes(client):
    class Router:
        def __init__(self):
            self.tasks = []

        def chat(self, messages, **kw):
            self.tasks.append(messages[-1].content)
            return ChatResult(content=f"观点{len(self.tasks)}", model="m1", provider="fake")

    router = Router()
    get_services().router = router
    get_services().teams.router = router
    r = client.post(
        "/api/missions",
        json={"directive": "测试", "mode": "pipeline", "preset": "fast", "include_market_data": False, "wait": True},
    ).json()
    speeches = [u for u in r["feed"] if u["kind"] in ("speech", "synthesis")]
    assert all(u["mode"] == "llm" and u["model"] == "fake/m1" for u in speeches)
    assert "【讨论记录】" in router.tasks[-1] and "观点" in router.tasks[-1]


def test_mission_with_lab_project_materials(client):
    p = client.post("/api/lab/projects", json={"idea": "均线", "template_id": "ma_cross"}).json()["project"]
    r = client.post(
        "/api/missions",
        json={"directive": "评审策略室项目", "lab_project_id": p["id"], "include_market_data": False, "wait": True},
    ).json()
    # 代码、版本演进与数据窗口放在【背景】里的研究简报（和单 Agent 同一份状态），平台数据里只留结构化要点
    ctx = r["mission"]["context"]
    assert "def initialize" in ctx and "【版本演进" in ctx and "v1（当前）" in ctx
    assert "最近 1500 根" in ctx
    lab = r["mission"]["materials"]["lab"]
    assert lab["version"] == 1 and lab["versions"] == 1 and lab["bars_limit"] == 1500
