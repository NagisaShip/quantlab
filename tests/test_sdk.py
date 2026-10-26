"""策略 SDK 测试（对标聚宽策略 API）。

重点：无未来数据、目标仓位语义、成本与容差、调度、沙箱、可复现。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.sdk import (
    BacktestRuntime,
    RuntimeCost,
    StrategyLoadError,
    check_source,
    load_source,
    run_backtest,
)

TREND = """
def initialize(ctx):
    set_benchmark("BTC/USDT")
    set_order_cost({"taker_fee": 0.001, "slippage_bps": 5})
    g.fast, g.slow = 12, 48
    run_daily(rebalance, time="close")

def rebalance(ctx):
    s = ctx.securities[0]
    closes = history(s, g.slow, "close")
    if len(closes) < g.slow:
        return
    fast = sum(closes[-g.fast:]) / g.fast
    slow = sum(closes) / g.slow
    order_target_percent(s, 0.8 if fast > slow else 0.0)
"""


@pytest.fixture(scope="module")
def bars():
    return synthetic_bars("BTC/USDT", "1h", 600)


def run(source: str, bars, **kw):
    mod = load_source(source, name="test")
    return run_backtest(mod, {"BTC/USDT": bars}, **kw)


# ---------------- 加载与沙箱 ----------------
def test_trend_strategy_runs_and_trades(bars):
    result = run(TREND, bars, warmup_bars=100)
    assert not result.errors, result.errors
    assert len(result.equity_curve) == len(result.timestamps)
    assert result.fills, "趋势策略应当产生成交"
    assert result.metrics["observations"] > 0


def test_warmup_excluded_from_scoring(bars):
    with_warmup = run(TREND, bars, warmup_bars=100)
    without = run(TREND, bars, warmup_bars=0)
    assert len(without.equity_curve) - len(with_warmup.equity_curve) == 100
    assert with_warmup.equity_curve[0] == 10_000


@pytest.mark.parametrize(
    "source,fragment",
    [
        ("import requests\ndef initialize(ctx): pass", "不允许导入"),
        ("def initialize(ctx):\n    place_order('BTC/USDT', 1)", "不允许出现"),
        ("def initialize(ctx):\n    import socket", "不允许导入"),
        ("def initialize(ctx:\n    pass", "语法错误"),
    ],
)
def test_bad_sources_are_rejected(source, fragment):
    with pytest.raises(StrategyLoadError, match=fragment):
        load_source(source)


def test_check_source_returns_readable_issues():
    issues = check_source("import httpx\ndef initialize(ctx): pass")
    assert issues and "httpx" in issues[0]


def test_strategy_must_define_entry_point():
    with pytest.raises(StrategyLoadError):
        load_source("x = 1\ny = 2\n")


def test_sdk_names_are_injected_without_import(bars):
    """策略文件可以像聚宽那样直接调用 order_* / history，不必 import。"""
    source = """
def initialize(ctx):
    g.seen = bool(history)
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if len(history(s, 5, "close")) >= 5 and ctx.position(s).amount == 0:
        order_value(s, 1000)
"""
    result = run(source, bars, warmup_bars=10)
    assert not result.errors
    assert result.globals["seen"] is True
    assert result.fills


# ---------------- 无未来数据 ----------------
def test_history_never_returns_future_bars():
    """在任意时刻，history 拿到的最后一根 bar 的 close_time 必须 ≤ 当前时刻。"""
    source = """
def initialize(ctx):
    run_daily(check)

def check(ctx):
    s = ctx.securities[0]
    rows = history(s, 5, ["close", "close_time"])
    if rows["close_time"]:
        g.last = rows["close_time"][-1]
        assert rows["close_time"][-1] <= ctx.current_dt, "读到了未来 bar"
"""
    result = run(source, synthetic_bars("BTC/USDT", "1h", 300), warmup_bars=20)
    assert not result.errors, result.errors


def test_get_price_rejects_future_end_dt(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    get_price(ctx.securities[0], 5, ["close"], end_dt=ctx.current_dt + 3_600_000)
"""
    result = run(source, bars, warmup_bars=10)
    assert result.errors, "未来时间戳必须报错"
    assert "未来" in result.errors[0]


# ---------------- 目标仓位语义 ----------------
def test_order_target_percent_reaches_target(bars):
    source = """
def initialize(ctx):
    run_daily(buy_once)

def buy_once(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_target_percent(s, 0.5)
"""
    result = run(source, bars, warmup_bars=5)
    assert result.fills
    ratio = result.portfolio["position_ratio"]
    # 成交后持仓占比应接近 50%（含费用与滑点，允许几个百分点偏差）
    assert 0.40 <= ratio <= 0.56, ratio


def test_order_target_percent_zero_clears_position(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_target_percent(s, 0.6)
    elif ctx.bar_index == 5:
        order_target_percent(s, 0.0)
"""
    result = run(source, bars, warmup_bars=2)
    assert result.portfolio["positions"] == {}
    assert any(f["side"] == "sell" for f in result.fills)


def test_order_target_sets_exact_amount(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_target(s, 0.1)
"""
    result = run(source, bars, warmup_bars=2)
    held = result.portfolio["positions"].get("BTC/USDT", {}).get("amount", 0)
    assert float(held) == pytest.approx(0.1, abs=1e-6)


def test_order_value_relative_and_negative(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_value(s, 2000)
    elif ctx.bar_index == 6:
        order_value(s, -1000)
"""
    result = run(source, bars, warmup_bars=2)
    sides = [f["side"] for f in result.fills]
    assert sides == ["buy", "sell"], sides
    # 卖出金额约为买入的一半
    buy = float(result.fills[0]["notional"])
    sell = float(result.fills[1]["notional"])
    assert 0.4 * buy < sell < 0.6 * buy


def test_rebalance_tolerance_suppresses_churn(bars):
    """容差内的微小漂移不应成交 —— 否则目标仓位法会把手续费吃光。"""
    from quant_agent.sdk import Context, OrderRequest
    from quant_agent.sdk.runner import StrategyRunner

    mod = load_source("def initialize(ctx): pass\n", name="churn")
    runtime = BacktestRuntime({"BTC/USDT": bars}, initial_cash=10_000, cost=RuntimeCost())
    ctx = Context(run_id="t", mode="backtest")
    StrategyRunner(mod, runtime, ctx).start()
    runtime.advance(bars[0].close_time)
    ctx.current_dt = bars[0].close_time

    def target(percent: float):
        return runtime.submit(
            OrderRequest(
                security="BTC/USDT",
                side="buy",
                amount=0.0,
                target_kind="percent_target",
                target_value=percent,
            )
        )

    assert target(0.5).ok
    second = target(0.5)  # 同一目标、同一价格 → 差额为 0，必须被容差挡下
    assert not second.ok
    assert "容差" in second.message


def test_min_notional_blocks_tiny_orders(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    if ctx.bar_index == 1:
        order_value(ctx.securities[0], 1)
"""
    result = run(source, bars, warmup_bars=2)
    assert not result.fills


# ---------------- 成本 ----------------
def test_fees_are_deducted_and_reported(bars):
    result = run(TREND, bars, warmup_bars=100)
    assert float(result.portfolio["total_fees"]) > 0
    assert all(float(f["fee"]) > 0 for f in result.fills)
    assert result.cost_model["taker_fee"] == 0.001


def test_higher_cost_never_improves_return(bars):
    cheap = run(TREND, bars, warmup_bars=100, cost=RuntimeCost(taker_fee=0.0005, slippage_bps=1))
    pricey = run(TREND, bars, warmup_bars=100, cost=RuntimeCost(taker_fee=0.004, slippage_bps=20))
    assert pricey.metrics["total_return"] <= cheap.metrics["total_return"] + 1e-9


def test_set_order_cost_from_strategy_overrides_defaults(bars):
    source = """
def initialize(ctx):
    set_order_cost(taker_fee=0.005, slippage_bps=30)
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_value(s, 1000)
"""
    result = run(source, bars, warmup_bars=2)
    assert result.cost_model["taker_fee"] == 0.005
    assert result.cost_model["slippage_bps"] == 30


def test_volume_participation_caps_fill(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_target_percent(s, 1.0)
"""
    tiny = [type(b)(**{**b.__dict__, "volume": 0.001}) for b in bars]
    result = run(source, tiny, warmup_bars=2)
    partials = [f for f in result.fills if f["partial"]]
    assert partials, "成交量占比限制应当触发截断"
    assert "成交量占比" in partials[0]["note"]


# ---------------- 调度 ----------------
def test_run_daily_runs_every_bar(bars):
    source = """
def initialize(ctx):
    run_daily(tick)

def tick(ctx):
    g.count = (g.count + 1) if hasattr(g, "count") else 1
"""
    result = run(source, bars, warmup_bars=10)
    assert result.globals["count"] == 590  # 600 - 10 根预热


def test_run_weekly_only_fires_on_that_weekday(bars):
    source = """
def initialize(ctx):
    run_weekly(tick, weekday=1)

def tick(ctx):
    g.count = (g.count + 1) if hasattr(g, "count") else 1
"""
    result = run(source, bars, warmup_bars=10)
    # 590 根 1h bar ≈ 24.6 天 → 大约 3~4 个周一
    assert 2 <= result.globals["count"] <= 5, result.globals


@pytest.mark.parametrize("count,expected", [(600, 1), (100, 0)])
def test_run_monthly_only_fires_on_that_day(count, expected):
    source = """
def initialize(ctx):
    g.count = 0
    g.calls = []
    run_monthly(tick, monthday=1)

def tick(ctx):
    g.count += 1
    g.calls.append(ctx.current_dt)
"""
    # A fixed interval must cover the month boundary after warmup; the wall clock
    # can otherwise put the entire first day in the excluded warmup window.
    end = int(datetime(2024, 1, 20, tzinfo=timezone.utc).timestamp() * 1000)
    bars = synthetic_bars("BTC/USDT", "1h", count, end=end)
    result = run(source, bars, warmup_bars=10)
    assert not result.errors
    assert result.globals["count"] == expected, result.globals
    assert all(datetime.fromtimestamp(ts / 1000, tz=timezone.utc).day == 1 for ts in result.globals["calls"])


def test_handle_data_is_default_when_no_schedule(bars):
    source = """
def initialize(ctx):
    g.n = 0

def handle_data(ctx):
    g.n += 1
"""
    result = run(source, bars, warmup_bars=10)
    assert result.globals["n"] == 590


def test_before_and_after_trading_hooks(bars):
    source = """
def initialize(ctx):
    g.open_calls, g.close_calls = 0, 0

def before_trading_start(ctx):
    g.open_calls += 1

def after_trading_end(ctx):
    g.close_calls += 1

def handle_data(ctx):
    pass
"""
    result = run(source, bars, warmup_bars=5)
    assert result.globals["open_calls"] == 1
    assert result.globals["close_calls"] == 1


# ---------------- 输出与隔离 ----------------
def test_log_and_record_are_captured(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    closes = history(s, 3, "close")
    log.info("bars=%d" % len(closes))
    record(n=len(closes))
"""
    result = run(source, bars, warmup_bars=10)
    assert result.logs and "bars=3" in result.logs[0]["message"]
    assert result.records["n"][0]["v"] == 3


def test_globals_are_isolated_between_runs(bars):
    source = """
def initialize(ctx):
    g.counter = 0

def handle_data(ctx):
    g.counter += 1
"""
    a = run(source, bars, warmup_bars=10)
    b = run(source, bars, warmup_bars=10)
    assert a.globals["counter"] == b.globals["counter"] == 590


def test_accessing_unset_global_gives_readable_error():
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    _ = g.never_set
"""
    result = run(source, synthetic_bars("BTC/USDT", "1h", 150), warmup_bars=5)
    assert result.errors
    assert "g.never_set" in result.errors[0]


def test_repeat_run_is_identical(bars):
    a = run(TREND, bars, warmup_bars=100)
    b = run(TREND, bars, warmup_bars=100)
    assert a.equity_curve == b.equity_curve
    assert a.fills == b.fills


def test_strategy_error_does_not_kill_the_run(bars):
    source = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    if ctx.bar_index == 3:
        raise ValueError("故意出错")
"""
    result = run(source, bars, warmup_bars=5)
    assert result.errors and "故意出错" in result.errors[0]
    assert len(result.equity_curve) > 0, "单根 bar 出错不应中断整轮回测"


# ---------------- 与平台模板策略对齐 ----------------
def test_sdk_and_platform_engine_agree_on_buy_and_hold():
    """同一份"满仓买入持有"逻辑，SDK 与平台引擎应给出接近的结果。

    两者都按"上一根收盘决策、当根收盘成交、双向收费"的口径，
    差异只来自 SDK 的调仓容差，所以允许一个小容差。
    """
    from quant_agent.engine import BacktestEngine, CostModel
    from quant_agent.strategies.templates import build_strategy

    data = synthetic_bars("BTC/USDT", "1h", 600)
    sdk = run(
        """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.position(s).amount == 0:
        order_target_percent(s, 1.0)
""",
        data,
        warmup_bars=100,
        cost=RuntimeCost(taker_fee=0.001, slippage_bps=5, spread_bps=2, rebalance_tolerance=0.0),
    )
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    platform = engine.run(build_strategy("buy_hold"), data, warmup_bars=100)
    a = sdk.metrics["total_return"]
    b = platform.metrics.total_return
    assert abs(a - b) < 0.02, f"SDK {a:.4f} vs 平台 {b:.4f} 差得太多"


# ---------------- 成本优先级 / 预置指标 ----------------
def test_set_order_cost_overrides_platform_cost_and_reports_source(bars):
    result = run(TREND, bars, warmup_bars=100, cost=RuntimeCost(taker_fee=0.002, slippage_bps=8))
    assert result.cost_model["cost_source"] == "strategy"
    assert result.cost_model["taker_fee"] == 0.001
    assert result.cost_model["slippage_bps"] == 5.0
    assert result.cost_model["strategy_cost_overrides"]["taker_fee"] == 0.001
    assert result.assumptions["cost"]["cost_source"] == "strategy"


def test_platform_cost_is_used_when_code_does_not_override(bars):
    source = """
def initialize(ctx):
    run_daily(go, time="close")

def go(ctx):
    order_target_percent(ctx.securities[0], 0.5)
"""
    result = run(source, bars, warmup_bars=10, cost=RuntimeCost(taker_fee=0.002, slippage_bps=8))
    assert result.cost_model["cost_source"] == "platform"
    assert result.cost_model["taker_fee"] == 0.002
    assert result.cost_model["slippage_bps"] == 8.0


def test_precomputed_indicators_run_and_are_causal(bars):
    source = """
def initialize(ctx):
    set_benchmark("BTC/USDT")
    run_daily(go, time="close")

def go(ctx):
    s = ctx.securities[0]
    fast = ctx.indicators.ema(s, 24)
    slow = ctx.indicators.ema(s, 48)
    band = ctx.indicators.atr(s, 14)
    record(fast=fast, slow=slow, atr=band)
    if fast == fast and slow == slow:
        order_target_percent(s, 0.6 if fast > slow else 0.0)
"""
    result = run(source, bars, warmup_bars=60)
    assert not result.errors, result.errors
    assert result.records["fast"][-1]["v"] == result.records["fast"][-1]["v"]  # 非 NaN
    assert len(result.fills) > 0


def test_tick_execution_mode_and_dynamic_slippage_are_reported(bars):
    result = run(
        TREND,
        bars,
        warmup_bars=100,
        cost=RuntimeCost(execution_mode="tick", dynamic_slippage=True, vol_slippage=0.5),
    )
    assert not result.errors, result.errors
    assert result.cost_model["execution_mode"] == "tick"
    assert result.cost_model["dynamic_slippage"] is True
    assert result.counters["tick_path_fills"] > 0
    assert result.assumptions["fill_model"] == "tick"


def test_liquidity_drought_rejects_orders(bars):
    source = """
def initialize(ctx):
    run_daily(go, time="close")

def go(ctx):
    order_target_percent(ctx.securities[0], 0.5)
"""
    result = run(
        source,
        bars,
        warmup_bars=10,
        cost=RuntimeCost(min_liquidity_usd=1e18),
    )
    assert result.fills == []
    assert result.counters["liquidity_drought"] > 0


def test_dynamic_slippage_changes_fill_prices(bars):
    base = run(TREND, bars, warmup_bars=100, cost=RuntimeCost())
    dyn = run(TREND, bars, warmup_bars=100, cost=RuntimeCost(dynamic_slippage=True, vol_slippage=1.0))
    assert base.fills and dyn.fills
    assert base.fills[0]["price"] != dyn.fills[0]["price"]


def test_class_only_strategy_is_rejected_before_save():
    source = """
class Strategy:
    def initialize(self, ctx):
        pass

    def on_bar(self, ctx, bar):
        ctx.set_target_position(0.0)
"""
    issues = check_source(source)
    assert any("模块级函数" in issue for issue in issues)


def test_every_sdk_template_runs_directly():
    from quant_agent.strategies.sdk_templates import TEMPLATES

    for tpl in TEMPLATES:
        bars = {s: synthetic_bars(s, tpl.timeframe, 700) for s in tpl.securities}
        result = run_backtest(
            load_source(tpl.code, name=tpl.id),
            bars,
            initial_cash=10_000,
            warmup_bars=80,
            cost=RuntimeCost(taker_fee=0.001, slippage_bps=5),
        )
        assert not result.errors, (tpl.id, result.errors)
        assert result.timestamps, tpl.id


def test_strategy_generation_fallback_is_runnable():
    from quant_agent.agents.strategy_gen import StrategyGenAgent

    parsed = StrategyGenAgent._parse("模型没有返回代码块")
    assert check_source(parsed.code) == []
    result = run_backtest(
        load_source(parsed.code, name="generated_fallback"),
        {"BTC/USDT": synthetic_bars("BTC/USDT", "1h", 200)},
        warmup_bars=20,
    )
    assert not result.errors
