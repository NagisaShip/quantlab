"""策略体检：10 个埋了未来函数的样本都要被抓到，干净策略不能误报；预热、成本、参数平台各自给出正确结论。"""

from __future__ import annotations

import numpy as np
import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.engine import checkup as C
from quant_agent.engine.research import BacktestSpec
from quant_agent.research.signals import to_wire

SPEC = BacktestSpec(securities=["BTC/USDT"], timeframe="1h", warmup_bars=30, slippage_bps=2, spread_bps=1)


@pytest.fixture(scope="module")
def bars():
    return {"BTC/USDT": synthetic_bars("BTC/USDT", "1h", 500)}


FACTOR_STRATEGY = """
PARAMS = {"th": 0.0}

def initialize(ctx):
    pass

def handle_data(ctx):
    s = ctx.securities[0]
    x = factor("x", s)
    if x is None:
        return
    {rec}
    order_target_percent(s, 0.8 if x > PARAMS["th"] else 0.0)
"""


def _closes(b):
    return np.array([x.close for x in b["BTC/USDT"]]), [x.close_time for x in b["BTC/USDT"]]


def _table(fn):
    def build(b):
        c, t = _closes(b)
        v = fn(c)
        return {"factor:x": to_wire(np.array(t), ["BTC/USDT"], np.asarray(v, dtype=float).reshape(-1, 1))}

    return build


def _shift(c, k):
    out = np.full_like(c, np.nan)
    if k < 0:
        out[:k] = c[-k:]
    else:
        out[k:] = c[:-k]
    return out


def _roll(c, w, fn, center=False):
    out = np.full_like(c, np.nan)
    h = w // 2
    for i in range(len(c)):
        lo, hi = (i - h, i + h + 1) if center else (i - w + 1, i + 1)
        if lo >= 0 and hi <= len(c):
            out[i] = fn(c[lo:hi])
    return out


LEAKY = {
    "未来一根收益": lambda c: _shift(c, -1) / c - 1,
    "居中均线偏离": lambda c: c / _roll(c, 11, np.mean, center=True) - 1,
    "下一根收盘": lambda c: _shift(c, -1) - c,
    "全样本标准化": lambda c: (c - c.mean()) / c.std() - 0.0,
    "全样本极值缩放": lambda c: (c - c.min()) / (c.max() - c.min()) - 0.5,
    "全样本去均值收益": lambda c: (c / _shift(c, 1) - 1) - np.nanmean(c / _shift(c, 1) - 1),
    "未来十根最高": lambda c: np.array(
        [c[i + 1 : i + 11].max() / c[i] - 1.01 if i + 11 <= len(c) else np.nan for i in range(len(c))]
    ),
    "全样本去趋势": lambda c: c - np.polyval(np.polyfit(np.arange(len(c)), c, 1), np.arange(len(c))),
    "未来24根方向标签": lambda c: np.sign(_shift(c, -24) - c),
    "全样本分位": lambda c: np.argsort(np.argsort(c)) / len(c) - 0.5,
}
CLEAN = {
    "过去一根收益": lambda c: c / _shift(c, 1) - 1,
    "尾随均线偏离": lambda c: c / _roll(c, 12, np.mean) - 1,
    "扩张窗口标准化": lambda c: np.array([(c[i] - c[: i + 1].mean()) / (c[: i + 1].std() or 1) for i in range(len(c))]),
}


def _lookahead(bars, fn, rec):
    build = _table(fn)
    src = FACTOR_STRATEGY.replace("{rec}", "record(x=x)" if rec else "pass")
    run = C.make_runner(src, name="t", spec=SPEC, research=build(bars))
    return C.lookahead(run, bars, warmup=SPEC.warmup_bars, research_at=lambda ts: build(C._upto(bars, ts)))


@pytest.mark.parametrize("name", list(LEAKY))
def test_planted_lookahead_is_detected(bars, name):
    # 一半样本只靠信号对比（不 record），一半还有 record 指标
    rec = list(LEAKY).index(name) % 2 == 0
    out = _lookahead(bars, LEAKY[name], rec)
    assert out["verdict"] == "bad", (name, out["summary"])


@pytest.mark.parametrize("name", list(CLEAN))
def test_clean_factors_have_no_false_positive(bars, name):
    out = _lookahead(bars, CLEAN[name], True)
    assert out["verdict"] == "ok", (name, out["summary"])


MA = """
PARAMS = {"fast": 8, "slow": 24}

def initialize(ctx):
    pass

def handle_data(ctx):
    s = ctx.securities[0]
    c = history(s, PARAMS["slow"], "close")
    if len(c) < PARAMS["slow"]:
        return
    f = sum(c[-PARAMS["fast"]:]) / PARAMS["fast"]
    sl = sum(c) / len(c)
    record(f=f, s=sl)
    order_target_percent(s, 0.8 if f > sl else 0.0)
"""


def test_python_template_strategy_is_clean_and_report_shape(bars):
    rep = C.run_checkup(MA, name="ma", bars=bars, spec=SPEC)
    by = {i["id"]: i for i in rep["items"]}
    assert set(by) == {"lookahead", "warmup", "cost", "plateau"}
    assert by["lookahead"]["verdict"] == "ok"
    assert by["warmup"]["verdict"] == "ok"  # slow=24 < 预热 30
    assert by["cost"]["verdict"] in ("ok", "warn", "bad")
    assert {r["param"] for r in by["plateau"]["details"]["rows"]} == {"fast", "slow"}
    assert rep["overall"] in C.VERDICTS and rep["metrics"]["trades"] is not None


def test_random_strategy_is_reported_nondeterministic(bars):
    src = """
import random

def initialize(ctx):
    pass

def handle_data(ctx):
    order_target_percent(ctx.securities[0], 0.8 if random.random() > 0.5 else 0.0)
"""
    run = C.make_runner(src, name="r", spec=SPEC)
    out = C.lookahead(run, bars, warmup=SPEC.warmup_bars)
    assert out["verdict"] == "warn" and out["details"]["deterministic"] is False


def test_insufficient_warmup_is_flagged(bars):
    src = MA.replace('{"fast": 8, "slow": 24}', '{"fast": 8, "slow": 50}')
    run = C.make_runner(src, name="w", spec=SPEC)
    out = C.warmup_check(run, bars, warmup=30)
    assert (out["verdict"] in ("warn", "bad") and out["details"]["needed"] >= 50) or out["details"]["needed"] == 60


def test_ema_recursion_needs_long_warmup(bars):
    # 预热期不调用策略；但用 history 取"所有能拿到的"K 线算 EMA 时，起点不同结果就不同
    src = """
def initialize(ctx):
    pass

def handle_data(ctx):
    s = ctx.securities[0]
    c = history(s, 1000, "close")
    e = c[0]
    for x in c[1:]:
        e += 0.02 * (x - e)
    record(ema=e)
    order_target_percent(s, 0.8 if c[-1] > e else 0.0)
"""
    run = C.make_runner(src, name="e", spec=SPEC)
    out = C.warmup_check(run, bars, warmup=30)
    assert out["verdict"] in ("warn", "bad"), out["summary"]


def test_cost_check_flags_fee_eaten_profit(bars):
    churn = """
def initialize(ctx):
    g.n = 0

def handle_data(ctx):
    g.n += 1
    order_target_percent(ctx.securities[0], 0.9 if g.n % 2 else 0.0)
"""
    run = C.make_runner(churn, name="c", spec=SPEC)
    out = C.cost_check(run, bars)
    assert out["details"]["stressed"]["fees"] > out["details"]["base"]["fees"]
    assert out["details"]["stressed"]["total_return"] < out["details"]["base"]["total_return"]


@pytest.mark.parametrize("base_scale", [1.0, 1.5])
@pytest.mark.parametrize("own_cost", [False, True])
def test_checkup_cost_matches_engine_stress(bars, base_scale, own_cost):
    from dataclasses import replace

    from quant_agent.engine.research import execute

    src = '''
def initialize(ctx):
    COST_SETUP
    g.n = 0

def handle_data(ctx):
    g.n += 1
    order_target_percent(ctx.securities[0], 0.5 if g.n % 2 else 0.0)
'''.replace(
        "COST_SETUP",
        'set_order_cost({"taker_fee": 0.001, "slippage_bps": 5, "spread_bps": 2})'
        if own_cost else "pass",
    )
    spec = replace(SPEC, cost_scale=base_scale)
    run = C.make_runner(src, name="consistent_cost", spec=spec)
    base = run(bars)
    stressed = run(bars, cost_mult=2)
    expected = execute(src, name="consistent_cost", bars=bars, spec=replace(spec, cost_scale=base_scale * 2))
    assert stressed.metrics == expected.metrics
    assert stressed.fills == expected.fills
    assert stressed.cost_model == expected.cost_model
    notional = sum(float(f["amount"]) * float(f["price"]) for f in stressed.fills)
    assert notional > 0
    fees = sum(float(f["fee"]) for f in stressed.fills)
    assert fees / notional == pytest.approx(0.001 * base_scale * 2)
    assert run(bars).metrics == base.metrics  # 压力测试不能修改复用的基准规格
    assert spec.cost_scale == base_scale


def test_plateau_neighbors():
    assert [n for _, n in C._neighbors(10)] == [8, 9, 11, 12]
    assert [n for _, n in C._neighbors(2)] == [0, 1, 3, 4] or 0 not in [n for _, n in C._neighbors(2)]
    assert C._neighbors(True) == []
    assert [n for _, n in C._neighbors(0.5)] == [0.4, 0.45, 0.55, 0.6]


def test_cut_points_match_lookahead(bars):
    pts = C.cut_points(bars, 30)
    tl = C._timeline(bars)
    assert len(pts) == len(C.CUT_FRACTIONS) and all(p in tl for p in pts) and pts == sorted(pts)
