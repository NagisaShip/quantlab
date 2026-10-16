"""团队评审汇总不再「未产出内容」、没设目标时不编造目标、行情连不上时用缓存顶上、联网搜索来源可配置。"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from quant_agent.agents.base import Agent
from quant_agent.agents.blocks_protocol import BLOCK_TYPES, BLOCKS_GUIDE
from quant_agent.agents.roles import RoleStore
from quant_agent.agents.strategy_lab import NO_GOAL_TEXT, goal_line, new_project, project_has_goal
from quant_agent.agents.team import TeamRunner, TeamRunStep, builtin_teams, fallback_synthesis
from quant_agent.context.manager import ContextManager
from quant_agent.data.service import DataUnavailable, MarketDataService
from quant_agent.exchanges.base import Bar
from quant_agent.llm.base import ChatResult, Tool, ToolCall
from quant_agent.llm.websearch import CUSTOM_KEY, WebSearch
from quant_agent.store import JsonStore


# ---------------------------------------------------------------- 团队评审汇总
class ToolHappyRouter:
    """有工具时永远只调工具（模拟汇总人把步数花在查数据上）；没有工具时才写文字。"""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def chat(self, messages, **kw):
        tools = kw.get("tools")
        self.calls.append(bool(tools))
        if tools:
            return ChatResult(content="", model="m", provider="fake", tool_calls=[ToolCall("1", tools[0].name, {})])
        return ChatResult(content="汇总：共识与分歧……", model="m", provider="fake")


def test_agent_forces_final_answer_after_step_cap():
    router = ToolHappyRouter()
    tool = Tool("noop", "什么也不做", {"type": "object", "properties": {}}, lambda **kw: {"ok": True})
    agent = Agent(name="a", role="reasoning", router=router, context=ContextManager(system_prompt="s"), tools=[tool], max_steps=2)
    run = agent.run("任务")
    assert run.final == "汇总：共识与分歧……"
    assert router.calls == [True, True, False]  # 两步都在调工具，最后补一次不带工具的最终答复


def test_team_synthesis_runs_without_tools(tmp_path):
    router = ToolHappyRouter()
    runner = TeamRunner(router, RoleStore(JsonStore(tmp_path)), store=JsonStore(tmp_path))
    runner.tools.register("noop", lambda **kw: {}, parameters={"type": "object", "properties": {}})
    team = next(t for t in builtin_teams() if t.id == "lab_review")
    run = runner.run(team, "评审策略")
    assert run.synthesis.startswith("汇总")
    assert "未产出内容" not in run.synthesis


def test_fallback_synthesis_lists_successful_roles():
    steps = [
        TeamRunStep("a", "市场分析师", "m", "ok", "趋势向上，但样本只有 60 天。"),
        TeamRunStep("b", "审计员", "m", "error", "", error="超时"),
    ]
    text = fallback_synthesis(steps, reason="额度用完")
    assert "规则汇总" in text and "市场分析师" in text and "审计员" not in text
    assert fallback_synthesis([steps[1]]) == ""


# ---------------------------------------------------------------- 目标
def test_new_project_without_goal_does_not_claim_default_goal():
    p = new_project({"idea": "BTC 均线"})
    assert not project_has_goal(p)
    assert goal_line(p) == NO_GOAL_TEXT
    assert "夏普" not in goal_line(p)
    p2 = new_project({"idea": "BTC 均线", "goals": {"min_sharpe": 1.5}})
    assert project_has_goal(p2) and "1.5" in goal_line(p2)


# ---------------------------------------------------------------- 行情：交易所连不上时用缓存
class FlakyAdapter:
    def __init__(self) -> None:
        self.fail = False
        self.calls = 0

    def fetch_bars(self, symbol, timeframe, start, end):
        self.calls += 1
        if self.fail:
            raise httpx.PoolTimeout("pool timeout")
        step = 3_600_000
        t = start - start % step
        out = []
        while t + step <= end:
            out.append(Bar(symbol=symbol, timeframe=timeframe, open_time=t, close_time=t + step - 1,
                           open=1, high=1, low=1, close=1, volume=1, closed=True))
            t += step
        return out


def test_quality_falls_back_to_cache_when_exchange_times_out(tmp_path, monkeypatch):
    adapter = FlakyAdapter()
    svc = MarketDataService(adapters={"binance": adapter}, cache_root=tmp_path, allow_synthetic=False)
    monkeypatch.setattr(MarketDataService, "PULL_RETRY_DELAYS", (0.0, 0.0))
    q = svc.quality("BTC/USDT", "1h", limit=200)
    assert q.bars >= 150 and not q.stale
    adapter.fail = True
    # 让缓存尾部落后，迫使下一次去补拉
    monkeypatch.setattr(time, "time", lambda: (q.coverage_end + 5 * 3_600_000) / 1000)
    q2 = svc.quality("BTC/USDT", "1h", limit=200)
    assert q2.stale and q2.warning and q2.bars > 0
    assert adapter.calls >= 4  # 首次 1 次 + 补拉时重试 3 次


def test_quality_without_cache_still_reports_unavailable(tmp_path, monkeypatch):
    adapter = FlakyAdapter()
    adapter.fail = True
    monkeypatch.setattr(MarketDataService, "PULL_RETRY_DELAYS", (0.0,))
    svc = MarketDataService(adapters={"binance": adapter}, cache_root=tmp_path, allow_synthetic=False)
    with pytest.raises(DataUnavailable):
        svc.quality("BTC/USDT", "1h", limit=200)


# ---------------------------------------------------------------- 联网搜索来源
def _ws(secrets: dict[str, str], handler=None) -> WebSearch:
    transport = httpx.MockTransport(handler or (lambda req: httpx.Response(404)))
    return WebSearch(lambda: secrets, client_factory=lambda: httpx.Client(transport=transport),
                     resolver=lambda host: ["93.184.216.34"])


def test_custom_source_added_by_agent_must_be_public(tmp_path, monkeypatch):
    secrets: dict[str, str] = {}
    ws = _ws(secrets)
    write = lambda s: secrets.update(s)  # noqa: E731
    bad = ws.add_custom(write, label="内网", url="http://127.0.0.1:8080", kind="searxng", trusted=False)
    assert not bad["ok"]
    no_q = ws.add_custom(write, label="rss", url="https://example.com/rss", kind="rss", trusted=False)
    assert not no_q["ok"] and "{q}" in no_q["error"]
    ok = ws.add_custom(write, label="SearX", url="https://searx.example.org", kind="searxng", trusted=False)
    assert ok["ok"] and ok["source"]["added_by"] == "agent"
    assert json.loads(secrets[CUSTOM_KEY])[0]["url"] == "https://searx.example.org"
    assert ws.status()["custom"][0]["label"] == "SearX"
    assert ws.remove_custom(write, ok["source"]["id"]) and ws.status()["custom"] == []


def test_custom_searxng_source_is_searched(monkeypatch):
    monkeypatch.setenv("QAGENT_FREE_SEARCH", "0")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/search" and req.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [{"title": "资金费率", "url": "https://a.example/x", "content": "摘要"}]})

    secrets = {CUSTOM_KEY: json.dumps([{"id": "c_1", "label": "SearX", "url": "https://searx.example.org", "kind": "searxng", "added_by": "admin"}])}
    out = _ws(secrets, handler).search("资金费率", source="custom")
    assert out["ok"] and out["results"][0]["url"] == "https://a.example/x" and "ql:citation" in out["note"]


def test_google_without_key_says_so(monkeypatch):
    monkeypatch.setenv("QAGENT_FREE_SEARCH", "1")

    def handler(req: httpx.Request) -> httpx.Response:
        rss = "<rss><channel><item><title>BTC</title><link>https://news.example/1</link></item></channel></rss>"
        return httpx.Response(200, text=rss)

    out = _ws({}, handler).search("BTC", source="google")
    assert out["ok"] and "没有配置密钥" in out["note"]


def test_google_cse_provider(monkeypatch):
    monkeypatch.setenv("QAGENT_FREE_SEARCH", "0")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["cx"] == "cx1" and req.url.params["key"] == "k1"
        return httpx.Response(200, json={"items": [{"title": "t", "link": "https://g.example/1", "snippet": "s"}]})

    out = _ws({"search:google_cse": "k1", "search:google_cse_cx": "cx1"}, handler).search("x", source="google")
    assert out["ok"] and out["sources"] == ["Google"]


# ---------------------------------------------------------------- 协议
def test_blocks_guide_mentions_every_type():
    for t in BLOCK_TYPES:
        assert f"ql:{t}" in BLOCKS_GUIDE, t


# ---------------------------------------------------------------- 毫秒级周期
def test_timeframe_parsing_and_tables():
    from quant_agent.data.quality import TIMEFRAME_MS
    from quant_agent.engine.metrics import BARS_PER_YEAR
    from quant_agent.timeframes import check_timeframe, is_sub_minute, parse_timeframe_ms

    assert parse_timeframe_ms("250ms") == 250 and parse_timeframe_ms("1s") == 1000 and parse_timeframe_ms("4h") == 14_400_000
    assert parse_timeframe_ms("10ms") is None and parse_timeframe_ms("1x") is None
    assert TIMEFRAME_MS["500ms"] == 500 and TIMEFRAME_MS.get("30m") == 1_800_000 and TIMEFRAME_MS.get("bad", 7) == 7
    assert "1s" in TIMEFRAME_MS and "bad" not in TIMEFRAME_MS
    assert BARS_PER_YEAR["1h"] == 8_760 and round(BARS_PER_YEAR["1s"]) == 365 * 86_400
    assert is_sub_minute("5s") and not is_sub_minute("1m")
    with pytest.raises(ValueError):
        check_timeframe("7q")


def test_binance_builds_sub_second_bars_from_trades():
    from quant_agent.exchanges.binance import BinanceAdapter

    t0 = 1_700_000_000_000
    trades = [{"T": t0 + 10, "p": "100", "q": "1"}, {"T": t0 + 120, "p": "101", "q": "2"}, {"T": t0 + 760, "p": "99", "q": "1"}]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/aggTrades"
        start = int(req.url.params["startTime"])
        return httpx.Response(200, json=[t for t in trades if t["T"] >= start])

    a = BinanceAdapter(transport=httpx.MockTransport(handler))
    bars = a.fetch_bars("BTC/USDT", "250ms", t0, t0 + 1000)
    assert [b.open_time - t0 for b in bars] == [0, 250, 500, 750]
    assert bars[0].open == 100 and bars[0].close == 101 and bars[0].volume == 3
    assert bars[1].volume == 0 and bars[1].close == 101  # 没成交的区间用上一根收盘价补齐
    assert bars[3].close == 99


def test_binance_resamples_seconds_from_1s_klines():
    from quant_agent.exchanges.binance import BinanceAdapter

    t0 = 1_700_000_000_000

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["interval"] == "1s"
        rows = [[t0 + i * 1000, "1", str(2 + i), "0.5", str(1 + i), "1", t0 + i * 1000 + 999] for i in range(10)]
        return httpx.Response(200, json=rows)

    bars = BinanceAdapter(transport=httpx.MockTransport(handler)).fetch_bars("BTC/USDT", "5s", t0, t0 + 10_000)
    assert len(bars) == 2 and bars[0].high == 6 and bars[0].close == 5 and bars[0].volume == 5


# ---------------------------------------------------------------- 数据库缓存
def test_db_cache_remember_and_stats(tmp_path):
    from quant_agent.data.dbcache import DbCache

    c = DbCache(tmp_path / "c.sqlite3")
    calls = []
    fn = lambda: calls.append(1) or {"ok": True, "v": len(calls)}  # noqa: E731
    assert c.remember("ns", "k", 60, fn)["v"] == 1
    assert c.remember("ns", "k", 60, fn)["v"] == 1 and len(calls) == 1
    assert c.remember("ns", "bad", 60, lambda: {"ok": False}, keep=lambda v: v["ok"]) == {"ok": False}
    assert c.get("ns", "bad") is None
    st = {r["ns"]: r for r in c.stats()["namespaces"]}
    assert st["ns"]["entries"] == 1 and st["ns"]["hits"] >= 1
    assert c.clear("ns") == 1


def test_websearch_uses_cache(monkeypatch):
    from quant_agent.data.dbcache import DbCache
    import tempfile, os

    monkeypatch.setenv("QAGENT_FREE_SEARCH", "0")
    hits = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(200, json={"results": [{"title": "t", "url": "https://x.example/1", "content": "c"}]})

    ws = _ws({"search:tavily": "k"}, handler)
    ws.cache = DbCache(os.path.join(tempfile.mkdtemp(), "c.sqlite3"))
    assert ws.search("btc")["ok"] and ws.search("btc")["ok"]
    assert len(hits) == 1


# ---------------------------------------------------------------- 跨对话与长文本
def _lab_with_conv(tmp_path, started):
    from quant_agent.agents.strategy_lab import LabStore, LabTools, StrategyLab

    def create(payload):
        p = new_project({"idea": payload["idea"]})
        lab.store.save(p)
        return p

    tools = LabTools(backtest=lambda *a, **k: {}, market=lambda *a: {}, templates=lambda q: [],
                     conversations={"start": lambda pid, text: started.append((pid, text)), "create": create})
    lab = StrategyLab(router=None, store=LabStore(JsonStore(tmp_path)), tools=tools)
    return lab


def test_conversation_tools_find_read_and_send(tmp_path):
    started: list = []
    lab = _lab_with_conv(tmp_path, started)
    a, b = new_project({"idea": "BTC 均线"}), new_project({"idea": "资金费率因子"})
    lab.store.save(a)
    lab.store.save(b)
    lab._post(b["id"], role="assistant", kind="text", content="资金费率 IC 0.04")
    tools = lab._conversation_tools(a["id"], {"type": "object"})
    found = tools["list_conversations"].handler(query="资金费率")
    assert found["total"] == 1 and found["conversations"][0]["id"] == b["id"]
    assert "IC 0.04" in found["conversations"][0]["last_reply"]
    read = tools["read_conversation"].handler(conversation_id=b["id"])
    assert read["ok"] and any("IC 0.04" in m["content"] for m in read["messages"])
    sent = tools["send_to_conversation"].handler(conversation_id=b["id"], message="用同一数据复现")
    assert sent["ok"] and started == [(b["id"], "用同一数据复现")]
    last = lab.store.messages(b["id"])[-1]
    assert last["role"] == "user" and last["data"]["from_conversation"] == a["id"] and last["data"]["relay_hops"] == 1
    assert not tools["send_to_conversation"].handler(conversation_id=a["id"], message="x")["ok"]  # 不能发给自己
    made = tools["create_conversation"].handler(idea="拆出去的子问题", message="开始")
    assert made["ok"] and made["created"]


def test_relay_hops_are_capped(tmp_path):
    started: list = []
    lab = _lab_with_conv(tmp_path, started)
    a, b = new_project({"idea": "A"}), new_project({"idea": "B"})
    lab.store.save(a)
    lab.store.save(b)
    lab.store.append(a["id"], {"role": "user", "kind": "text", "content": "转发来的", "data": {"relay_hops": 3}})
    out = lab._conversation_tools(a["id"], {"type": "object"})["send_to_conversation"].handler(conversation_id=b["id"], message="再转")
    assert not out["ok"] and "转发" in out["error"]


def test_file_pins_keep_full_text_and_read_in_pages(tmp_path):
    lab = _lab_with_conv(tmp_path, [])
    p = new_project({"idea": "长文"})
    lab.store.save(p)
    text = "甲" * 9000 + "乙" * 3000
    pin = lab.pin(p["id"], kind="file", title="长文本.txt", text=text)
    assert len((lab.store.get(p["id"])["pins"][0])["text"]) == 12000
    block = lab._context_block(lab.store.get(p["id"]))
    assert "read_pinned_file" in block and "共 12000 字" in block
    tools = lab._all_tools(p["id"])
    out = tools["read_pinned_file"].handler(pin_id=pin["id"], offset=8000, length=8000)
    assert out["ok"] and out["text"].startswith("甲" * 1000) and out["next_offset"] is None and out["total_chars"] == 12000
