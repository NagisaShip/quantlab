"""SDK 第二版：沙箱、参数、下一根开盘成交、基准与相对指标、时长守卫。"""

from __future__ import annotations

import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.sdk import (
    RuntimeCost,
    StrategyLoadError,
    check_source,
    declared_params,
    load_source,
    run_backtest,
)


@pytest.fixture(scope="module")
def bars():
    return synthetic_bars("BTC/USDT", "1h", 400)


BUY_ONCE = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index == 1:
        order_target_percent(s, 0.5)
"""


# ---------------- 沙箱 ----------------
@pytest.mark.parametrize(
    "source",
    [
        "def initialize(ctx):\n    __import__('os').system('echo hi')",
        "def initialize(ctx):\n    x = ().__class__.__mro__",
        "def initialize(ctx):\n    getattr(ctx, 'portfolio')",
        "import os\ndef initialize(ctx): pass",
        "from pathlib import Path\ndef initialize(ctx): pass",
        "def initialize(ctx):\n    open('/etc/passwd').read()",
        "def initialize(ctx):\n    eval('1+1')",
        "def initialize(ctx):\n    ctx._runtime.cost.taker_fee = 0",
        "import numpy as np\ndef initialize(ctx):\n    np.load('x.npy')",
    ],
)
def test_sandbox_blocks_escape_routes(source):
    assert check_source(source), "沙箱应当拦下这段代码"
    with pytest.raises(StrategyLoadError):
        load_source(source)


def test_sandbox_allows_ordinary_math():
    src = """
import math
from collections import deque
from statistics import mean

def initialize(ctx):
    g.window = deque(maxlen=3)
    g.v = math.sqrt(16) + mean([1, 2, 3])
"""
    assert check_source(src) == []
    load_source(src)


def test_runtime_import_is_whitelisted_even_if_audit_is_bypassed():
    """受限 __builtins__ 是第二道门：即使 AST 漏判，运行时也导不进 os。"""
    from quant_agent.sdk.sandbox import _safe_import

    with pytest.raises(ImportError):
        _safe_import("os")
    assert _safe_import("math").sqrt(4) == 2


def test_infinite_loop_is_stopped(bars):
    src = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    while True:
        pass
"""
    result = run_backtest(load_source(src, name="loop"), {"BTC/USDT": bars}, time_limit=0.5)
    assert result.timed_out
    assert any("超过" in e for e in result.errors)


# ---------------- 参数 ----------------
PARAM_SRC = """
PARAMS = {"fast": 5, "slow": 20, "weight": 0.8}

def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    closes = history(s, get_param("slow"), "close")
    if len(closes) < PARAMS["slow"]:
        return
    fast = sum(closes[-PARAMS["fast"]:]) / PARAMS["fast"]
    slow = sum(closes) / len(closes)
    record(fast_period=PARAMS["fast"])
    order_target_percent(s, get_param("weight") if fast > slow else 0)
"""


def test_declared_params_reads_literal_without_executing():
    assert declared_params(PARAM_SRC) == {"fast": 5, "slow": 20, "weight": 0.8}


def test_params_override_keeps_declared_types(bars):
    mod = load_source(PARAM_SRC, params={"fast": 7.0, "weight": "0.5"})
    assert mod.params == {"fast": 7, "slow": 20, "weight": 0.5}
    result = run_backtest(mod, {"BTC/USDT": bars}, warmup_bars=30)
    assert result.params["fast"] == 7
    assert result.records["fast_period"][0]["v"] == 7


def test_unknown_param_is_rejected():
    with pytest.raises(StrategyLoadError, match="不在策略的 PARAMS"):
        load_source(PARAM_SRC, params={"nope": 1})


# ---------------- 成交时点 ----------------
def test_default_fill_is_next_bar_open(bars):
    result = run_backtest(load_source(BUY_ONCE), {"BTC/USDT": bars}, warmup_bars=10)
    fill = result.fills[0]
    # 第一个计分 bar 是 bars[10]，决策在它收盘，成交在 bars[11] 开盘
    nxt = bars[11]
    assert fill["ts"] == nxt.open_time
    cost = result.cost_model
    expected = nxt.open * (1 + (cost["spread_bps"] / 2 + cost["slippage_bps"]) / 10_000)
    assert float(fill["price"]) == pytest.approx(expected, rel=1e-9)


def test_close_fill_mode_is_still_available(bars):
    result = run_backtest(load_source(BUY_ONCE), {"BTC/USDT": bars}, warmup_bars=10, cost=RuntimeCost(fill_at="close"))
    assert result.fills[0]["ts"] == bars[10].close_time


def test_last_bar_signal_is_reported_as_unfilled(bars):
    src = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    order_target_percent(ctx.securities[0], 1.0 if ctx.bar_index % 2 else 0.0)
"""
    result = run_backtest(load_source(src), {"BTC/USDT": bars[:60]}, warmup_bars=5)
    assert result.unfilled in (0, 1)
    if result.unfilled:
        assert any("未成交" in log["message"] for log in result.logs)


# ---------------- 基准与相对指标 ----------------
def test_benchmark_curve_and_relative_metrics(bars):
    hold = """
def initialize(ctx):
    set_benchmark("BTC/USDT")
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.position(s).amount == 0:
        order_target_percent(s, 1.0)
"""
    result = run_backtest(
        load_source(hold),
        {"BTC/USDT": bars},
        warmup_bars=20,
        cost=RuntimeCost(taker_fee=0, slippage_bps=0, spread_bps=0, rebalance_tolerance=0),
    )
    m = result.metrics
    assert result.benchmark == "BTC/USDT"
    assert len(result.benchmark_curve) == len(result.equity_curve)
    # 无成本满仓持有 ≈ 基准，beta 接近 1
    assert m["beta"] == pytest.approx(1.0, abs=0.1)
    assert m["benchmark_return"] is not None
    assert abs(m["excess_return"]) < 0.05
    assert result.daily and {"date", "return", "benchmark_return", "positions"} <= set(result.daily[0])
    assert 0 <= m["daily_win_rate"] <= 1


def test_cash_strategy_has_zero_beta(bars):
    src = "def initialize(ctx):\n    pass\n"
    result = run_backtest(load_source(src), {"BTC/USDT": bars}, warmup_bars=20)
    assert result.metrics["total_return"] == 0
    assert result.metrics["beta"] == pytest.approx(0.0, abs=1e-12)


def test_multi_security_universe(bars):
    eth = synthetic_bars("ETH/USDT", "1h", 400)
    src = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    if ctx.bar_index == 1:
        for s in ctx.securities:
            order_target_percent(s, 0.4)
"""
    result = run_backtest(load_source(src), {"BTC/USDT": bars, "ETH/USDT": eth}, warmup_bars=10)
    assert {f["security"] for f in result.fills} == {"BTC/USDT", "ETH/USDT"}
    assert result.portfolio["position_ratio"] == pytest.approx(0.8, abs=0.1)


def test_to_dict_keeps_final_point(bars):
    result = run_backtest(load_source(BUY_ONCE), {"BTC/USDT": bars}, warmup_bars=10)
    body = result.to_dict(max_points=50)
    assert body["equity_curve"][-1] == result.equity_curve[-1]
    assert len(body["equity_curve"]) == len(body["benchmark_curve"]) == len(body["timestamps"])
