"""策略室目标（/goal）与共享工具：解析、合理性检查、必达/尽量评估、提前结束、接口、联网搜索与站内搜索。"""

from __future__ import annotations

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents import lab_goal as G
from quant_agent.agents.strategy_lab import _hopeless
from quant_agent.agents.team import TeamRunner, ToolRegistry
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult, ToolCall
from quant_agent.llm.websearch import WebSearch, html_to_text

GOOD = """PARAMS = {"fast": 10, "slow": 30, "weight": 0.6}

def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    c = history(s, PARAMS["slow"], "close")
    if len(c) < PARAMS["slow"]:
        return
    order_target_percent(s, PARAMS["weight"] if sum(c[-PARAMS["fast"]:]) / PARAMS["fast"] > sum(c) / len(c) else 0)
"""


# ---------------- 解析 ----------------
def test_parse_goal_text_cn():
    out = G.parse_goal_text("/goal 夏普>1.2 回撤不超过15% 交易至少20笔 尽量 胜率>50% ETH 4h 2023-01-01~2024-06-30 6轮")
    spec = out["spec"]
    items = {(i["metric"], i["priority"]): i for i in spec["items"]}
    assert items[("sharpe", "must")]["target"] == 1.2 and items[("sharpe", "must")]["op"] == ">="
    assert items[("max_drawdown", "must")]["target"] == pytest.approx(0.15)
    assert items[("max_drawdown", "must")]["op"] == "<="
    assert items[("trades", "must")]["target"] == 20
    assert items[("win_rate", "try")]["target"] == pytest.approx(0.5)
    assert spec["symbols"] == ["ETH/USDT"] and spec["timeframe"] == "4h" and spec["max_rounds"] == 6
    assert spec["start"] < spec["end"]


def test_parse_goal_recent_range_and_pairs():
    spec = G.parse_goal_text("年化 30% 回撤 20% SOL/USDT 1d 近 2 年")["spec"]
    assert {i["metric"] for i in spec["items"]} == {"annualized_return", "max_drawdown"}
    assert spec["symbols"] == ["SOL/USDT"] and spec["timeframe"] == "1d" and spec["start"] and spec.get("end") is None


# ---------------- 评估 ----------------
def test_must_and_try_evaluation():
    items = [
        {"metric": "sharpe", "op": ">=", "target": 1.0, "priority": "must"},
        {"metric": "win_rate", "op": ">=", "target": 0.6, "priority": "try"},
    ]
    ev = G.evaluate_items({"sharpe": 1.3, "win_rate": 0.4}, items)
    assert ev["passed"], "尽量项不达标不影响通过"
    worse = G.evaluate_items({"sharpe": 0.8, "win_rate": 0.7}, items)
    assert not worse["passed"]
    assert ev["score"] > G.evaluate_items({"sharpe": 1.3, "win_rate": 0.4}, items[:1])["score"] - 0.01
    assert G.gap_report(worse)[0]["metric"] == "sharpe"


def test_legacy_roundtrip():
    items = G.items_from_legacy({"min_sharpe": 1.0, "max_drawdown": 0.2, "min_trades": None})
    assert G.legacy_from_items(items) == {
        "min_sharpe": 1.0,
        "max_drawdown": 0.2,
        "min_trades": None,
        "min_excess": None,
    }


# ---------------- 合理性检查 ----------------
def test_check_flags_unrealistic_goals_with_suggestions():
    spec = G.normalize_spec(
        {
            "items": [
                {"metric": "sharpe", "target": 6},
                {"metric": "max_drawdown", "target": 0.01},
                {"metric": "trades", "target": 500},
            ],
            "bars_limit": 600,
        }
    )
    check = G.check_spec(spec)
    assert check["verdict"] == "unreasonable"
    fields = {i["field"] for i in check["issues"]}
    assert {"items.0.target", "items.1.target", "items.2.target"} <= fields
    fixed = G.apply_suggestions(spec, check)
    assert fixed["items"][0]["target"] == G.METRICS["sharpe"]["soft_max"]
    assert G.check_spec(fixed)["verdict"] != "unreasonable"


def test_check_ok_and_history_gap():
    spec = G.normalize_spec({"items": G.TEMPLATES[0]["items"]})
    assert G.check_spec(spec)["verdict"] == "ok"
    far = G.check_spec(spec, history=[{"sharpe": 0.1, "max_drawdown": 0.1, "trades": 20}])
    assert far["verdict"] == "warn" and any("离目标很远" in i["problem"] for i in far["issues"])


def test_hopeless_early_stop():
    ev = G.evaluate_items({"sharpe": 0.2}, [{"metric": "sharpe", "op": ">=", "target": 2.0, "priority": "must"}])
    assert _hopeless(ev, [1.0, 1.0], 2)
    assert not _hopeless(ev, [1.0, 2.0], 2), "还在进步就不停"
    assert not _hopeless(ev, [1.0], 1)


# ---------------- 接口 ----------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _project(client, **kw):
    body = {"idea": "BTC 均线", "data_source": "synthetic", "bars_limit": 600, "max_rounds": 2, **kw}
    return client.post("/api/lab/projects", json=body).json()["project"]


def test_goal_endpoints(client):
    meta = client.get("/api/lab/meta").json()
    assert meta["goal_metrics"] and meta["goal_templates"] and meta["search"]["configured"] is False
    p = _project(client)
    parsed = client.post(
        "/api/lab/goal/parse", json={"text": "/goal 夏普>1.5 回撤<12% 4h", "project_id": p["id"]}
    ).json()
    assert parsed["spec"]["timeframe"] == "4h" and parsed["spec"]["symbols"] == []
    assert p["market_auto"] is True
    assert "items" in parsed["parsed_fields"]
    check = client.post(
        "/api/lab/goal/check", json={"spec": {"items": [{"metric": "sharpe", "target": 9}]}, "project_id": p["id"]}
    ).json()
    assert check["check"]["verdict"] == "unreasonable" and check["suggested"]["items"][0]["target"] < 9
    r = client.post(f"/api/lab/projects/{p['id']}/goal", json={"spec": parsed["spec"], "review": False})
    assert r.status_code == 200, r.text
    proj = r.json()["project"]
    assert proj["timeframe"] == "4h" and proj["goal"]["items"] and proj["goals"]["min_sharpe"] == 1.5
    card = [m for m in r.json()["messages"] if m["kind"] == "goal"][-1]
    assert card["data"]["stage"] == "set" and card["data"]["check"]["verdict"] in ("ok", "warn")
    # 旧接口改 goals 时，结构化目标同步
    client.patch(f"/api/lab/projects/{p['id']}", json={"goals": {"min_sharpe": 0.8}})
    proj = client.get(f"/api/lab/projects/{p['id']}").json()["project"]
    assert any(i["metric"] == "sharpe" and i["target"] == 0.8 for i in proj["goal"]["items"])


def test_goal_set_and_start_runs_autopilot(client):
    p = _project(client, securities=["BTC/USDT"], market_auto=False)
    spec = {"items": [{"metric": "sharpe", "target": 0.1}, {"metric": "trades", "target": 1}]}
    r = client.post(
        f"/api/lab/projects/{p['id']}/goal", json={"spec": spec, "review": False, "start": True, "wait": True}
    )
    assert r.status_code == 200, r.text
    kinds = [m["kind"] for m in r.json()["messages"]]
    assert "goal" in kinds and "backtest" in kinds and r.json()["project"]["status"] == "awaiting_user"


class ToolRouter:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        r = self.replies.pop(0)
        return r if isinstance(r, ChatResult) else ChatResult(content=r, model="fake-1", provider="fake")

    def available_models(self):
        return [{"provider": "fake", "model": "fake-1", "target": "fake/fake-1", "type": "openai_compatible"}]

    def _has_credentials(self, _):
        return True


def test_model_tools_set_goal_and_search(client):
    p = _project(client)
    client.post(f"/api/lab/projects/{p['id']}/code", json={"code": GOOD})
    router = ToolRouter(
        [
            ChatResult(
                content="",
                model="fake-1",
                provider="fake",
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="set_goal",
                        arguments={"items": [{"metric": "sharpe", "target": 1.4}], "timeframe": "4h"},
                    ),
                    ToolCall(id="2", name="search_platform", arguments={"query": "均线"}),
                    ToolCall(id="3", name="web_search", arguments={"query": "BTC ETF 资金流"}),
                    ToolCall(id="4", name="check_goals", arguments={}),
                ],
            ),
            "好的，目标已经记下。",
        ]
    )
    get_services().router = router
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "目标夏普 1.4，改 4 小时", "wait": True})
    assert r.status_code == 200, r.text
    names = {t.name for t in router.calls[0]["tools"]}
    assert {"set_goal", "check_goals", "walk_forward", "search_platform", "web_search", "fetch_page"} <= names
    tool_out = {m.name: m.content for m in router.calls[1]["messages"] if m.role == "tool"}
    assert "verdict" in tool_out["set_goal"]
    assert "BTC 均线" in tool_out["search_platform"] or "lab" in tool_out["search_platform"]
    assert "未配置" in tool_out["web_search"]
    proj = r.json()["project"]
    assert proj["timeframe"] == "4h" and proj["goal"]["items"][0]["target"] == 1.4
    tool_cards = [m["data"]["name"] for m in r.json()["messages"] if m["kind"] == "tool"]
    assert "search_platform" in tool_cards and "web_search" in tool_cards


def test_walk_forward(client):
    p = _project(client, bars_limit=1500)
    client.post(f"/api/lab/projects/{p['id']}/code", json={"code": GOOD})
    r = client.post(f"/api/lab/projects/{p['id']}/walkforward", json={"segments": 3, "wait": True})
    assert r.status_code == 200, r.text
    card = [m for m in r.json()["messages"] if m["kind"] == "walkforward"][-1]
    assert len(card["data"]["segments"]) == 3 and card["data"]["verdict"]
    assert all(s["ok"] for s in card["data"]["segments"]), card["data"]


def test_search_key_config(client):
    assert client.get("/api/config/search").json()["configured"] is False
    r = client.put("/api/config/search", json={"provider": "tavily", "api_key": "tvly-1234567890abcd"})
    assert r.status_code == 200 and r.json()["configured"] and "1234567890" not in r.json()["key_masked"]
    assert client.put("/api/config/search", json={"provider": "tavily", "api_key": ""}).json()["configured"] is False


# ---------------- 联网搜索（mock 网络）----------------
def _ws(handler, secrets=None, resolver=None):
    return WebSearch(
        lambda: secrets or {},
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=resolver or (lambda host: ["93.184.216.34"]),
    )


def test_web_search_tavily_and_unconfigured():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"results": [{"title": "A", "url": "https://a.com", "content": "x <b>y</b>"}]})

    assert "未配置" in _ws(handler).search("btc")["error"]
    out = _ws(handler, {"search:tavily": "tvly-key"}).search("btc", max_results=3)
    assert out["ok"] and out["results"][0]["url"] == "https://a.com" and seen["auth"] == "Bearer tvly-key"


def test_fetch_page_blocks_private_and_extracts_text():
    def handler(req):
        if req.url.path == "/r":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/secret"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><title>标题</title><script>x()</script></head><body><p>正文一</p><p>正文二</p></body></html>",
        )

    def resolver(host):
        return ["10.0.0.1"] if host.startswith("10.") else ["93.184.216.34"]

    ws = _ws(handler, resolver=resolver)
    ok = ws.fetch("https://example.com/page")
    assert ok["ok"] and ok["title"] == "标题" and "正文一" in ok["text"] and "x()" not in ok["text"]
    assert not ws.fetch("http://127.0.0.1/admin")["ok"]
    assert not ws.fetch("file:///etc/passwd")["ok"]
    assert "内网" in ws.fetch("https://example.com/r")["error"], "跳转到内网也要拦下"
    assert html_to_text("<div>a</div><div>b</div>") == "a\nb"


def test_registry_catalog_and_schema_tools():
    reg = ToolRegistry()
    reg.register("legacy", lambda q: {"q": q})
    reg.register(
        "search_platform",
        lambda query="", kind="all": {"query": query},
        label="站内搜索",
        description="搜索",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    assert {c["name"] for c in reg.catalog()} == {"legacy", "search_platform"}
    runner = TeamRunner.__new__(TeamRunner)
    runner.tools = reg
    t = runner._make_tool("search_platform")
    assert t.parameters["properties"]["query"] and t.handler(query="x") == {"query": "x"}
    assert runner._make_tool("legacy").handler(query="y") == {"q": "y"}
