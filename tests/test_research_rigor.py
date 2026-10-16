"""研究纪律与研究工具：数据核验、指标一致性、零交易诊断、共线性、联网搜索解析、行情同步、
单轮工具上限、调研报告、子 Agent、回测批量删除。"""

from __future__ import annotations

import time

import numpy as np
import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_lab_goal import GOOD, ToolRouter

from quant_agent.agents import rigor as R
from quant_agent.agents.strategy_lab import _diff_summary, _trim_tool_outputs, tool_call_limit
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.data.service import MarketDataService, synthetic_bars
from quant_agent.llm import websearch as W
from quant_agent.llm.base import ChatResult, Message, ToolCall


@pytest.fixture
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


# ---------------- 数据核验 / 指标口径 ----------------
def test_data_check_flags_short_and_stale_data():
    hour = 3_600_000
    now = int(time.time() * 1000)
    project = {"timeframe": "1h", "bars_limit": 4320}  # 研究者要 180 天
    short = [{"symbol": "BTC/USDT", "source": "exchange", "bars": 1500, "start_ts": now - 1500 * hour, "end_ts": now}]
    out = R.data_check(project, short)
    assert not out["formal"] and "非正式" in out["label"] and "覆盖" in out["issues"][0]
    full = [{"symbol": "BTC/USDT", "source": "exchange", "bars": 4320, "start_ts": now - 4320 * hour, "end_ts": now}]
    assert R.data_check(project, full)["formal"]
    stale = [{**full[0], "end_ts": now - 60 * 24 * hour}]
    assert any("不新鲜" in x for x in R.data_check(project, stale)["issues"])


def test_consistency_explains_geometric_excess():
    m = {
        "total_return": -0.013,
        "benchmark_return": 0.275,
        "excess_return": (1 - 0.013) / 1.275 - 1,
        "total_fees": 47.7,
    }
    out = R.consistency(m, initial_cash=10_000)
    assert out["checks"][0]["ok"] and "简单差" in out["checks"][0]["detail"]
    assert "0.48%" in out["checks"][1]["detail"]
    bad = R.consistency({**m, "excess_return": -0.288}, initial_cash=10_000)
    assert not bad["ok"]


def test_zero_trade_diagnosis_counts_records():
    res = {
        "metrics": {"trades": 0},
        "records": {"raw_cross": [{"v": 1}, {"v": 0}, {"v": 1}], "trend_ok": [{"v": 0}] * 3},
    }
    out = R.zero_trade_diagnosis(res)
    assert out["counts"]["raw_cross"]["true_or_positive"] == 2 and out["counts"]["trend_ok"]["true_or_positive"] == 0
    assert "record(" in R.zero_trade_diagnosis({"metrics": {"trades": 0}})["hint"]
    assert R.zero_trade_diagnosis({"metrics": {"trades": 3}}) is None


def test_backtest_tool_returns_rigor_fields(client):
    p = _project(client)
    client.post(f"/api/lab/projects/{p['id']}/code", json={"code": GOOD})
    r = client.post(f"/api/lab/projects/{p['id']}/backtest", json={"wait": True})
    card = [m for m in r.json()["messages"] if m["kind"] == "backtest"][-1]
    d = card["data"]
    assert {"data_check", "definitions", "consistency"} <= set(d)
    # 合成行情只能验证流程：核验不通过
    assert d["data_check"]["formal"] is False and card["content"].startswith("【非正式回测】")
    rows = client.get("/api/research/backtests").json()["backtests"]
    assert rows[0]["lab_project_id"] == p["id"]


# ---------------- 实验记录 / 工具上限 ----------------
def test_diff_summary_and_tool_limit():
    before = "PARAMS = {'a': 1}\n\ndef step(ctx):\n    x = 1\n"
    after = "PARAMS = {'a': 2}\n\ndef step(ctx):\n    x = 2\n"
    d = _diff_summary(before, after)
    assert d["added"] == 2 and d["removed"] == 2 and "step" in d["functions"]
    assert tool_call_limit({}) == 1000 and tool_call_limit({"max_tool_calls": 99999}) == 5000
    msgs = [Message(role="tool", content="x" * 20_000) for _ in range(20)]
    _trim_tool_outputs(msgs)
    assert len(msgs[0].content) < 500 and len(msgs[-1].content) == 20_000


def test_tool_loop_respects_project_limit(client):
    p = _project(client)
    calls = [
        ChatResult(
            content="",
            model="fake-1",
            provider="fake",
            tool_calls=[ToolCall(id=str(i), name="list_versions", arguments={})],
        )
        for i in range(5)
    ]
    get_services().router = ToolRouter([*calls, "完成"])
    r = client.post(
        f"/api/lab/projects/{p['id']}/messages", json={"text": "看看版本", "max_tool_calls": 3, "wait": True}
    )
    assert r.status_code == 200, r.text
    assert any("工具调用上限（3 次" in m["content"] for m in r.json()["messages"] if m["role"] == "event")


# ---------------- 调研报告 ----------------
def test_write_report_and_read(client):
    p = _project(client)
    md = "## 摘要\n" + "测试内容。" * 40
    router = ToolRouter(
        [
            ChatResult(
                content="",
                model="fake-1",
                provider="fake",
                tool_calls=[
                    ToolCall(
                        id="1", name="write_report", arguments={"title": "测试报告", "markdown": md, "summary": "无"}
                    )
                ],
            ),
            "报告已提交。",
        ]
    )
    get_services().router = router
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "写报告", "wait": True})
    card = [m for m in r.json()["messages"] if m["kind"] == "report"][-1]
    rep = client.get(f"/api/lab/projects/{p['id']}/reports/{card['data']['id']}").json()["report"]
    assert rep["title"] == "测试报告" and rep["markdown"] == md
    assert r.json()["project"]["reports"][0]["id"] == card["data"]["id"]


# ---------------- 回测批量删除 ----------------
def test_batch_delete_backtests(client):
    p = _project(client)
    client.post(f"/api/lab/projects/{p['id']}/code", json={"code": GOOD})
    for _ in range(2):
        client.post(f"/api/lab/projects/{p['id']}/backtest", json={"wait": True})
    ids = [b["id"] for b in client.get("/api/research/backtests").json()["backtests"]]
    assert len(ids) == 2
    out = client.post("/api/research/backtests/batch-delete", json={"ids": ids}).json()
    assert out["deleted"] == 2 and client.get("/api/research/backtests").json()["total"] == 0


# ---------------- 行情：不用合成数据 / 同步补缺口 ----------------
class _Adapter:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def fetch_bars(self, symbol, timeframe, start, end):
        self.calls.append((start, end))
        return [b for b in self.bars if start <= b.open_time < end]


def test_no_synthetic_fallback_when_disabled(tmp_path):
    svc = MarketDataService(adapters={}, allow_synthetic=False, cache_root=tmp_path)
    with pytest.raises(Exception, match="未接入数据"):
        svc.get_bars("BTC/USDT", "1h", limit=50)
    with pytest.raises(Exception, match="未接入数据"):
        svc.get_bars("BTC/USDT", "1h", limit=50, source="synthetic")


def test_short_request_not_rejected_for_open_bar(tmp_path):
    bars = synthetic_bars("BTC/USDT", "1h", 24, anchored=True)
    for b in bars:
        b.closed = True
    svc = MarketDataService(adapters={"binance": _Adapter(bars)}, allow_synthetic=False)
    got, src = svc.get_bars("BTC/USDT", "1h", limit=25)  # 最新一根未收盘被过滤：只有 24 根也算取到
    assert src == "exchange" and len(got) >= 22


def test_sync_fills_internal_gaps(tmp_path):
    hour = 3_600_000
    end = int(time.time() * 1000) // hour * hour
    bars = synthetic_bars("BTC/USDT", "1h", 300, end=end, anchored=True)
    for b in bars:
        b.closed = True
    adapter = _Adapter(bars)
    svc = MarketDataService(adapters={"binance": adapter}, allow_synthetic=False, cache_root=tmp_path)
    holey = bars[:100] + bars[150:]
    svc.cache.merge("binance", "BTC/USDT", "1h", holey)
    out = svc.sync("BTC/USDT", "1h", start=bars[0].open_time, end=bars[-1].close_time)
    assert out["gaps"] == 0 and out["segments_filled"] >= 1 and out["fetched"] >= 50


# ---------------- 多因子共线性 ----------------
def test_collinearity_flags_duplicate_factors():
    from quant_agent.research.collinearity import diagnose
    from quant_agent.research.panel import Panel

    rng = np.random.default_rng(0)
    t, n = 300, 6
    close = np.cumprod(1 + rng.normal(0, 0.01, (t, n)), axis=0) * 100
    panel = Panel(
        np.arange(t), [f"S{i}/USDT" for i in range(n)], {"close": close, "volume": rng.uniform(1, 2, (t, n))}, "1h"
    )
    out = diagnose(
        panel,
        [
            {"name": "mom20", "expr": "close / delay(close, 20) - 1"},
            {"name": "mom20b", "expr": "close / delay(close, 20)"},
            {"name": "vol", "expr": "volume"},
        ],
    )
    assert out["ok"], out
    assert any({"mom20", "mom20b"} == set(c) for c in out["clusters"])
    assert len(out["suggest_keep"]) < 3


# ---------------- 联网搜索解析 ----------------
def test_search_parsers():
    bing = '<li class="b_algo"><h2><a href="https://a.com/x">标题 <b>A</b></a></h2><p>摘要一</p></li>'
    assert W._parse_bing(bing)[0] == {"title": "标题 A", "url": "https://a.com/x", "snippet": "摘要一"}
    ddg = (
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.com%2Fy&rut=1">B</a>'
        '<a class="result__snippet" href="#">片段</a>'
    )
    assert W._parse_ddg(ddg)[0]["url"] == "https://b.com/y"
    rss = "<rss><channel><item><title>新闻</title><link>https://c.com/n</link><pubDate>Mon</pubDate></item></channel></rss>"
    assert W._parse_rss(rss)[0]["url"] == "https://c.com/n"
    baidu = '<div class="result c-container" mu="https://d.com/z"><h3 class="t"><a href="http://www.baidu.com/link?url=1">百度标题</a></h3><span>内容</span></div>'
    assert W._parse_baidu(baidu)[0]["url"] == "https://d.com/z"
