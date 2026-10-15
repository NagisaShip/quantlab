"""测试：策略模板、上下文管理、通知、适配器契约。"""

from __future__ import annotations

import httpx
import pytest

from quant_agent.agents.guidance import GUIDANCES, render_guidance_prompt
from quant_agent.context.manager import (
    ContextBudget,
    ContextManager,
    LongTermMemory,
    MemoryItem,
    _approx_tokens,
)
from quant_agent.exchanges.base import Bar, UnsupportedFeature
from quant_agent.exchanges.binance import BinanceAdapter
from quant_agent.exchanges.okx import OKXAdapter
from quant_agent.llm.base import ChatResult, Message
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig
from quant_agent.notify.channels import ConsoleNotifier, Notification, NotificationHub, WeComNotifier
from quant_agent.strategies.sdk import StrategyContext
from quant_agent.strategies.templates import STRATEGY_REGISTRY, BuyAndHold, MovingAverageTrend


def _make_bars(n: int, start: float = 100.0, step: float = 1.0, timeframe: str = "1h"):
    bars = []
    for i in range(n):
        price = start + i * step
        bars.append(
            Bar(
                symbol="BTC/USDT",
                timeframe=timeframe,
                open_time=i * 3_600_000,
                close_time=(i + 1) * 3_600_000,
                open=price,
                high=price + 0.5,
                low=price - 0.5,
                close=price,
                volume=10.0,
                closed=True,
            )
        )
    return bars


# ---------------- 策略 SDK ----------------
def test_history_never_returns_future_bars():
    ctx = StrategyContext(run_id="r1")
    ctx.load_history("BTC/USDT", _make_bars(10))
    ctx.set_time(5 * 3_600_000)  # 时钟在 5 根之后
    hist = ctx.history("BTC/USDT", 3)
    assert len(hist) == 3
    assert all(b.close_time <= ctx.now for b in hist), "不得返回未来 bar"


def test_ma_trend_goes_long_on_uptrend():
    strat = MovingAverageTrend(params={"fast": 3, "slow": 5})
    ctx = StrategyContext(run_id="r2")
    ctx.load_history("BTC/USDT", _make_bars(30, start=100, step=2))
    strat.initialize(ctx)
    bars = _make_bars(30, start=100, step=2)
    for b in bars:
        ctx.set_time(b.close_time)
        strat.on_bar(ctx, b)
    assert ctx.target_position > 0, "上升趋势应收多"


def test_buy_and_hold_baseline():
    strat = BuyAndHold()
    ctx = StrategyContext(run_id="r3")
    strat.initialize(ctx)
    strat.on_bar(ctx, _make_bars(1)[0])
    assert ctx.target_position == 1.0


def test_registry_has_five_templates():
    assert set(STRATEGY_REGISTRY) >= {"ma_trend", "mean_reversion", "grid_bounded", "dca_rebalance", "buy_hold"}


# ---------------- 上下文管理 ----------------
def test_approx_tokens_reasonable():
    assert _approx_tokens("hello world") >= 1
    assert _approx_tokens("你好世界") == pytest.approx(4, abs=1)


def test_context_manager_compress_keeps_recent():
    cm = ContextManager(system_prompt="sys", budget=ContextBudget(history_tokens=5))
    for i in range(20):
        cm.append(Message(role="user", content=f"消息{i}" * 5))
    cm.compress(lambda msgs: f"共{len(msgs)}条历史")
    assert len(cm.history) <= 5
    assert "历史摘要" in cm.history[0].content


def test_context_snapshot_hash_stable():
    cm = ContextManager(system_prompt="sys")
    cm.append(Message(role="user", content="a"))
    h1 = cm.snapshot_hash()
    h2 = ContextManager(system_prompt="sys")
    h2.append(Message(role="user", content="a"))
    assert h1 == h2.snapshot_hash()


def test_long_term_memory_keyword_retrieval():
    memo = LongTermMemory()
    memo.add(MemoryItem(key="a", text="网格策略参数是 10 格", kind="conclusion"))
    memo.add(MemoryItem(key="b", text="趋势策略使用均线", kind="note"))
    hits = memo.retrieve("网格 策略")
    assert any("网格" in h.text for h in hits)


# ---------------- 通知 ----------------
def test_notification_hub_pending_on_failure():
    class Failing:
        def send(self, note):
            return False

    hub = NotificationHub({"wecom": Failing()})
    ok = hub.send(Notification(title="t", body="b", channel="wecom"))
    assert not ok
    assert len(hub.pending) == 1


def test_wecom_notifier_success_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 0})

    n = WeComNotifier("https://example.invalid/robot", transport=httpx.MockTransport(handler))
    assert n.send(Notification(title="t", body="b", channel="wecom")) is True


def test_wecom_notifier_failure_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 93000})

    n = WeComNotifier("https://example.invalid/robot", transport=httpx.MockTransport(handler))
    assert n.send(Notification(title="t", body="b", channel="wecom")) is False


# ---------------- 交易所适配器 ----------------
def test_binance_bar_closed_flag_depends_on_end():
    """close_time < end → 已收盘；否则标记为未完成（关键：不得把未完成 K 线当已收盘）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/klines"):
            return httpx.Response(
                200,
                json=[[0, "100", "101", "99", "100.5", "5", 3_600_000, "0", 1, "0", "0", "0"]],
            )
        return httpx.Response(404)

    a = BinanceAdapter(transport=httpx.MockTransport(handler))
    # end 远小于 close_time → 未完成
    unclosed = a.fetch_bars("BTC/USDT", "1h", 0, 2_000_000)
    assert unclosed and unclosed[0].closed is False
    # end 远大于 close_time → 已收盘
    closed = a.fetch_bars("BTC/USDT", "1h", 0, 2_000_000_000_000)
    assert closed and closed[0].closed is True


def test_binance_public_domain_chinese_note():
    a = BinanceAdapter()
    assert "data-api.binance.vision" in a.base


def test_okx_requires_simulated_header():
    a = OKXAdapter(simulated=True)
    assert a._headers().get("x-simulated-trading") == "1"


def test_okx_balance_requires_key():
    a = OKXAdapter(simulated=True)
    with pytest.raises(UnsupportedFeature):
        a.fetch_balances()


def test_binance_public_balance_unsupported():
    a = BinanceAdapter(simulated=False)
    with pytest.raises(UnsupportedFeature):
        a.fetch_balances()


# ---------------- 路由与降级 ----------------
def test_router_falls_back():
    calls = []

    class FakeProvider:
        def __init__(self, name):
            self.name = name

        def chat(self, messages, *, model, temperature=0.2, tools=None, max_tokens=None, stream=False):
            calls.append(self.name)
            if self.name == "primary":
                from quant_agent.llm.base import LLMError

                raise LLMError("boom")
            return ChatResult(content="ok", model=model, provider=self.name)

    cfg = AgentConfig(
        providers={
            "primary": ProviderConfig("primary", "openai_compatible", "http://x", "k"),
            "backup": ProviderConfig("backup", "openai_compatible", "http://y", "k"),
        },
        routing={"default": "primary/m1", "fallback": ["backup/m2"]},
        limits={},
        trace={},
    )
    router = ModelRouter(cfg)
    router._clients = {"primary": FakeProvider("primary"), "backup": FakeProvider("backup")}
    result = router.chat([Message(role="user", content="hi")])
    assert result.content == "ok"
    assert calls == ["primary", "backup"]


def test_router_model_settings_map_request_model_and_effort():
    seen = []

    class FakeProvider:
        def chat(self, messages, *, model, temperature=0.2, tools=None, max_tokens=None, effort=None):
            seen.append((model, effort))
            return ChatResult(content="ok", model=model, provider="gw")

    settings = {
        "fast": {"label": "快速", "request_model": "vendor-fast-2026", "context_window": 64000, "effort": "low"}
    }
    cfg = AgentConfig(
        providers={"gw": ProviderConfig("gw", "openai_compatible", "http://x", "k", ["fast"], settings)},
        routing={"default": "gw/fast"},
        limits={},
        trace={},
    )
    router = ModelRouter(cfg)
    router._clients = {"gw": FakeProvider()}
    router.chat([Message(role="user", content="hi")])
    router.chat([Message(role="user", content="hi")], effort="high")
    assert seen == [("vendor-fast-2026", "low"), ("vendor-fast-2026", "high")]
    [m] = router.available_models()
    assert (m["target"], m["label"], m["context_window"], m["default_effort"]) == ("gw/fast", "快速", 64000, "low")


# ---------------- 引导 ----------------
def test_guidance_render():
    g = GUIDANCES["trend"]
    text = render_guidance_prompt(g, g.filled_defaults())
    assert "趋势" in text and "BTC/USDT" in text


# ---------------- 配置加载 ----------------
def test_config_load_merges_and_reads_notify(tmp_path):
    cfg_file = tmp_path / "agent.config.toml"
    cfg_file.write_text(
        """
[providers.deepseek]
type = "openai_compatible"
base_url = "https://api.deepseek.com/v1"
api_key = "${DEEPSEEK_API_KEY}"

[routing]
default = "deepseek/deepseek-chat"

[notify]
channel = "console"
wecom_webhook = "https://example.invalid/robot"
""",
        encoding="utf-8",
    )
    cfg = AgentConfig.load(cfg_file)
    assert "deepseek" in cfg.providers
    assert cfg.routing["default"] == "deepseek/deepseek-chat"
    assert cfg.notify["wecom_webhook"] == "https://example.invalid/robot"


def test_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # 项目根目录可能已有 agent.config.toml（dev 脚本会复制一份）
    from quant_agent.llm.base import LLMError

    with pytest.raises(LLMError):
        AgentConfig.load(tmp_path / "nope.toml", allow_default=False)


def test_config_default_when_missing(tmp_path, monkeypatch):
    """没有配置文件时也要能起来——否则交付第一眼就是报错。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = AgentConfig.load(tmp_path / "nope.toml")
    assert "deepseek" in cfg.providers
    assert cfg.routing["default"]
    assert cfg.notify["channel"] == "console"


# ---------------- Agent 循环 ----------------
def test_agent_executes_tool_then_finishes():
    from quant_agent.agents.base import Agent, tool
    from quant_agent.context.manager import ContextManager
    from quant_agent.llm.base import ToolCall

    class ScriptedRouter:
        def __init__(self):
            self.n = 0

        def chat(self, messages, *, role="default", model=None, temperature=0.2, tools=None, max_tokens=None):
            self.n += 1
            if self.n == 1:
                return ChatResult(
                    content="",
                    model="m",
                    provider="p",
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
                )
            return ChatResult(content="done", model="m", provider="p")

    calls = []
    echo = tool(
        "echo",
        "回显",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        handler=lambda text: calls.append(text) or f"echo:{text}",
    )
    agent = Agent(
        name="t",
        role="default",
        router=ScriptedRouter(),
        context=ContextManager(system_prompt="sys"),
        tools=[echo],
    )
    run = agent.run("do it")
    assert run.status == "done"
    assert run.final == "done"
    assert calls == ["hi"]
    assert any(s.kind == "tool" and s.tool_name == "echo" for s in run.steps)


def test_agent_blocks_when_llm_fails():
    from quant_agent.agents.base import Agent
    from quant_agent.context.manager import ContextManager
    from quant_agent.llm.base import LLMError

    class BoomRouter:
        def chat(self, *a, **k):
            raise LLMError("all down")

    agent = Agent(name="t", role="default", router=BoomRouter(), context=ContextManager(system_prompt="s"))
    run = agent.run("x")
    assert run.status == "blocked"
    assert "失败" in run.final


# ---------------- 长期 Agent 幂等 ----------------
def test_daily_digest_idempotent():
    from quant_agent.agents.long_running import DailyDigestAgent, DigestData
    from quant_agent.notify.channels import NotificationHub

    class StubRouter:
        def chat(self, messages, *, role="default", model=None, temperature=0.2, tools=None, max_tokens=None):
            return ChatResult(content="【策略简报】净值 10120.5", model="m", provider="p")

    data = DigestData(date="2026-10-12", nav=10120.5, period_change=0.012)
    agent = DailyDigestAgent(
        router=StubRouter(),
        hub=NotificationHub({"console": ConsoleNotifier()}),
        collect_data=lambda: data,
    )
    r1 = agent.run_once(date="2026-10-12", dry_run=True)
    r2 = agent.run_once(date="2026-10-12", dry_run=True)
    assert r1.status != "skipped"
    assert r2.status == "skipped", "同一 run_key 不得重复执行/推送"
    assert "非投资建议" in r1.output
