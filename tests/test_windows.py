"""固定观察窗口与预热口径测试（对应 plan.md 第 5 章）。"""

from __future__ import annotations

from quant_agent.data.service import synthetic_bars
from quant_agent.engine import BacktestEngine, CostModel, compute_windows, window_status_text
from quant_agent.engine.windows import HOUR_MS, WindowResult
from quant_agent.strategies.templates import build_strategy

SYNTH = synthetic_bars("BTC/USDT", "1h", 900)


def test_warmup_bars_are_excluded_from_scoring():
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    with_warmup = engine.run(build_strategy("ma_trend"), SYNTH, warmup_bars=100)
    without = engine.run(build_strategy("ma_trend"), SYNTH, warmup_bars=0)
    # 计分点数 = 起点 + (900 - 100) 根
    assert len(with_warmup.equity_curve) == 801
    assert len(without.equity_curve) == 901
    # 起点净值恒为初始资金
    assert with_warmup.equity_curve[0] == 10_000
    assert any("预热" in n for n in with_warmup.notes)


def test_window_is_fixed_from_start_not_rolling():
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    result = engine.run(build_strategy("ma_trend"), SYNTH, warmup_bars=100)
    w = result.windows
    start = w["start_ts"]
    days7 = w["windows"]["7"]
    assert days7["cutoff_ts"] == start + 7 * 24 * HOUR_MS
    assert days7["complete"] is True
    assert days7["hours"] == 168.0
    assert w["windows"]["30"]["hours"] == 720.0
    # 滚动窗口锚在末端，与固定窗口不是一回事
    assert w["rolling"]["7"]["cutoff_ts"] == w["end_ts"]
    assert w["rolling"]["7"]["metrics"]["total_return"] != days7["metrics"]["total_return"]


def test_partial_window_is_marked_incomplete():
    short = synthetic_bars("BTC/USDT", "1h", 260)  # 计分约 6.7 天
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    result = engine.run(build_strategy("ma_trend"), short, warmup_bars=100)
    w7 = result.windows["windows"]["7"]
    w30 = result.windows["windows"]["30"]
    assert w7["complete"] is False
    assert w30["complete"] is False
    assert w7["hours"] < 168
    assert "未满" in window_status_text(WindowResult(**{**w7, "metrics": result.metrics}))
    assert "7 天窗口完整" in window_status_text(
        WindowResult(**{**result.windows["windows"]["7"], "complete": True, "metrics": result.metrics})
    )


def test_empty_curve_is_safe():
    """还没有观测点时，窗口结构也要存在，界面才能显示「窗口未满」。"""
    ws = compute_windows([], [], initial=10_000)
    assert ws.scored_bars == 0
    assert set(ws.windows) == {"7", "30"}
    assert all(w.complete is False and w.hours == 0.0 for w in ws.windows.values())
    assert set(ws.rolling) == {"7", "30"}


def test_repeat_run_is_identical():
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    a = engine.run(build_strategy("ma_trend"), SYNTH, warmup_bars=100)
    b = engine.run(build_strategy("ma_trend"), SYNTH, warmup_bars=100)
    assert a.equity_curve == b.equity_curve
    assert a.windows == b.windows
