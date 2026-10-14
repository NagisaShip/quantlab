"""智能调参（optuna）：参数空间声明、检查点恢复、多目标，以及与网格搜索同预算的样本外对比。"""

from __future__ import annotations

import statistics
from dataclasses import replace

import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.engine.research import BacktestSpec, declared_space, normalize_space, optimize

pytest.importorskip("optuna")

SRC = """
PARAMS = {"fast": 8, "slow": 24, "band": 0.0, "weight": 0.8}
PARAM_SPACE = {"fast": (3, 20), "slow": (30, 90), "band": (0.0, 0.02), "weight": (0.5, 1.0)}

def initialize(ctx):
    pass

def handle_data(ctx):
    s = ctx.securities[0]
    c = history(s, PARAMS["slow"], "close")
    if len(c) < PARAMS["slow"]:
        return
    f = sum(c[-PARAMS["fast"]:]) / PARAMS["fast"]
    sl = sum(c) / len(c)
    if f > sl * (1 + PARAMS["band"]):
        order_target_percent(s, PARAMS["weight"])
    elif f < sl * (1 - PARAMS["band"]):
        order_target_percent(s, 0.0)
"""
SPEC = BacktestSpec(warmup_bars=100)


def _bars(sym, n=1200):
    return {"BTC/USDT": [replace(b, symbol="BTC/USDT") for b in synthetic_bars(sym, "1h", n)]}


def test_space_parsing():
    sp = normalize_space(declared_space(SRC))
    assert sp["fast"] == {"low": 3, "high": 20, "int": True}
    assert sp["band"]["int"] is False
    assert normalize_space({"mode": ["a", "b"]}) == {"mode": {"choices": ["a", "b"]}}
    with pytest.raises(ValueError):
        normalize_space({"x": (5, 1)})
    with pytest.raises(ValueError):
        normalize_space({})
    assert declared_space("PARAM_SPACE = make()") == {}


def test_bayes_records_every_trial_and_resumes():
    bars = _bars("ETH/USDT", 700)
    seen = []
    out = optimize(
        SRC,
        name="t",
        bars=bars,
        spec=SPEC,
        grid={},
        method="bayes",
        space=declared_space(SRC),
        n_trials=8,
        on_row=seen.append,
    )
    assert out["method"] == "bayes" and out["n_trials"] == len(out["rows"]) == len(seen) <= 8
    assert out["best"]["in_rank"] == 1 and all("out_sample" in r for r in out["rows"])
    # 检查点恢复：已算完的试验不重算
    again = []
    done = {__import__("json").dumps(r["params"], sort_keys=True): r for r in seen[:5]}
    out2 = optimize(
        SRC,
        name="t",
        bars=bars,
        spec=SPEC,
        grid={},
        method="bayes",
        space=declared_space(SRC),
        n_trials=8,
        done_rows=done,
        on_row=again.append,
    )
    assert len(again) <= 3 and out2["n_trials"] <= 8


def test_multi_objective_marks_pareto_front():
    out = optimize(
        SRC,
        name="t",
        bars=_bars("SOL/USDT", 700),
        spec=SPEC,
        grid={},
        method="bayes",
        space=declared_space(SRC),
        n_trials=10,
        multi_objective=True,
    )
    assert out["multi_objective"] and out["diagnostics"]["pareto_size"] >= 1
    assert out["best"].get("pareto") is True


def test_bayes_out_of_sample_not_worse_than_grid_at_same_budget():
    """同样 16 次回测：4 个参数的网格每维只能取 2 个值，optuna 在连续范围里搜。比较 4 段行情的样本外夏普中位数。"""
    grid = {"fast": [5, 15], "slow": [40, 80], "band": [0.0, 0.01], "weight": [0.5, 1.0]}
    g_oos, b_oos = [], []
    for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT"):
        bars = _bars(sym)
        g = optimize(SRC, name="t", bars=bars, spec=SPEC, grid=grid)
        b = optimize(
            SRC, name="t", bars=bars, spec=SPEC, grid={}, method="bayes", space=declared_space(SRC), n_trials=16
        )
        g_oos.append(g["best"]["out_sample"]["sharpe"])
        b_oos.append(b["best"]["out_sample"]["sharpe"])
    assert statistics.median(b_oos) >= statistics.median(g_oos) - 1e-9, (g_oos, b_oos)
