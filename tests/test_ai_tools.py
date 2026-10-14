"""「问 AI」调用平台工具：目录、按场景加载、只读工具直接执行、有后果的操作要人确认。"""

from __future__ import annotations

import json

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_demo_trading import FLIP

from quant_agent.agents import platform_tools as PT
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult, ToolCall


class ToolRouter:
    """按剧本返回：第 1 轮调 list_strategies，第 2 轮调 promote_strategy（需确认），第 3 轮给结论。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(
        self,
        messages,
        *,
        role="default",
        model=None,
        temperature=0.2,
        tools=None,
        max_tokens=None,
        effort=None,
        on_delta=None,
    ):
        self.calls.append({"messages": list(messages), "tools": [t.name for t in tools or []]})
        step = self.script.pop(0)
        if isinstance(step, list):
            return ChatResult(
                content="",
                model="fake-1",
                provider="fake",
                tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(step)],
            )
        return ChatResult(content=step, model="fake-1", provider="fake")

    def available_models(self):
        return []

    def current_target(self, role="default"):
        return "fake/fake-1"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    services = AppServices.build(store_root=str(tmp_path_factory.mktemp("ai-tools")))
    set_services(services)
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_catalog_and_prompt_are_central():
    cat = PT.catalog()
    names = {t["name"] for t in cat["tools"]}
    assert {
        "list_strategies",
        "run_backtest",
        "start_session",
        "stop_session",
        "fidelity_report",
        "start_stress_drill",
        "promote_strategy",
        "list_events",
    } <= names
    for t in cat["tools"]:
        assert t["description"] and t["when"] and t["risk"] in ("read", "run", "confirm") and t["scene"] in PT.SCENES
    # 有后果的操作全部是 confirm；目录里没有任何实盘下单工具
    risky = {t["name"] for t in cat["tools"] if t["risk"] == "confirm"}
    assert {"start_session", "stop_session", "promote_strategy", "apply_calibration"} <= risky
    # 微实盘只开放「看状态」和「一键停止」，没有任何启动实盘或下单的工具
    assert {t["name"] for t in cat["tools"] if "live" in t["name"]} == {"microlive_status", "microlive_kill"}
    assert not any("实盘下单" in t["title"] or "启动实盘" in t["title"] for t in cat["tools"])
    assert "microlive_kill" in risky
    # 按场景加载：只列本轮的工具
    block = PT.prompt_block(["validation"])
    assert "promote_strategy" in block and "list_strategies" not in block and "需要你确认" in block
    assert PT.scenes_for("validation", None) == ["validation", "trading"]
    assert PT.scenes_for("validation", ["data", "bogus"]) == ["data"]
    assert PT.prompt_block([]) == ""


def test_ask_calls_tools_and_risky_actions_wait_for_confirmation(client):
    slug = client.post(
        "/api/studio/strategies", json={"name": "工具测试", "code": FLIP, "securities": ["BTC/USDT"], "timeframe": "1h"}
    ).json()["strategy"]["slug"]
    router = ToolRouter(
        [[("list_strategies", {})], [("promote_strategy", {"slug": slug})], "我查到了这个策略，晋级需要你确认。"]
    )
    get_services().router = router
    r = client.post(
        "/api/ai/ask", json={"question": "把它晋级", "page": "validation", "scenes": ["strategy", "validation"]}
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["content"].startswith("我查到了")
    calls = out["tool_calls"]
    assert [c["tool"] for c in calls] == ["list_strategies", "promote_strategy"]
    assert calls[0]["status"] == "done" and calls[1]["status"] == "pending" and calls[1]["action_id"]
    # 工具定义按场景传给模型；系统提示里有统一生成的工具说明
    assert "promote_strategy" in router.calls[0]["tools"] and "start_session" not in router.calls[0]["tools"]
    assert "【平台工具】" in router.calls[0]["messages"][0].content
    # 模型拿到的工具结果：列表是真实数据；需要确认的动作没有执行
    tool_msgs = [m for m in router.calls[2]["messages"] if m.role == "tool"]
    assert slug in tool_msgs[0].content
    assert json.loads(tool_msgs[1].content)["status"] == "needs_confirmation"
    assert client.get(f"/api/validation/graduation/{slug}").json()["stage"] == "backtest"

    aid = calls[1]["action_id"]
    assert client.get(f"/api/ai/actions/{aid}").json()["action"]["status"] == "pending"
    # 确认：真正执行（这里检查没通过，所以执行结果是错误，阶段不变）
    act = client.post(f"/api/ai/actions/{aid}", json={"approve": True}).json()["action"]
    assert act["status"] == "error" and "没通过" in json.dumps(act["result"], ensure_ascii=False)
    # 同一个动作不能再确认一次
    assert client.post(f"/api/ai/actions/{aid}", json={"approve": True}).status_code == 400


def test_cancel_and_stream_tool_events(client):
    router = ToolRouter([[("stop_session", {"session_id": "nope"})], "需要你确认停止。"])
    get_services().router = router
    r = client.post("/api/ai/ask/stream", json={"question": "停掉", "page": "paper"})
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {") and "type" in line]
    tool = next(e for e in events if e["type"] == "tool")
    assert tool["tool"] == "stop_session" and tool["status"] == "pending"
    final = events[-1]
    assert final["type"] == "final" and final["tool_calls"][0]["action_id"] == tool["action_id"]
    act = client.post(f"/api/ai/actions/{tool['action_id']}", json={"approve": False}).json()["action"]
    assert act["status"] == "canceled"
    # 没带页面也没勾选场景：不加载任何工具（普通问答不受影响）
    router = ToolRouter(["普通回答"])
    get_services().router = router
    client.post("/api/ai/ask", json={"question": "hi"})
    assert router.calls[0]["tools"] == []
