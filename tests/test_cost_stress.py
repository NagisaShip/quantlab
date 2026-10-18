"""成本压力测试：倍数在撮合时统一生效（策略自己 set_order_cost 也照样放大）；成本扫描并行找出极限。"""

from __future__ import annotations

from quant_agent.agents.strategy_lab import (
    LabStore,
    LabTools,
    StrategyLab,
    cost_fee_check,
    new_project,
    sweep_limit,
    sweep_summary,
)
from quant_agent.engine.live import clock_synthetic_bars
from quant_agent.sdk import RuntimeCost, load_source
from quant_agent.sdk.runtimes import run_backtest
from quant_agent.store import JsonStore

# 策略在 initialize 里写死了自己的成本：以前压力测试放大的费率会被它覆盖，2 倍成本下手续费不变
OWN_COST = """
def initialize(ctx):
    set_order_cost({"taker_fee": 0.001, "slippage_bps": 5, "spread_bps": 2})
    run_daily(flip, time="close")


def flip(ctx):
    s = ctx.securities[0]
    order_target_percent(s, 0.0 if ctx.position(s).amount > 0 else 0.5)
"""

DAY = 86_400_000


def _run(scale: float):
    bars = {"BTC/USDT": clock_synthetic_bars("BTC/USDT", "1d", 1_700_000_000_000, 1_700_000_000_000 + 80 * DAY)}
    return run_backtest(
        load_source(OWN_COST, name="own_cost"), bars, cost=RuntimeCost(), timeframe="1d", cost_scale=scale
    )


def _fee_rate(res) -> float:
    fills = res.fills
    assert fills, "测试策略应该有成交"
    fee = sum(float(f["fee"]) for f in fills)
    notional = sum(float(f["amount"]) * float(f["price"]) for f in fills)
    return fee / notional


def test_stress_doubles_fees_even_when_strategy_sets_its_own_cost():
    base, stressed = _run(1.0), _run(2.0)
    assert abs(_fee_rate(base) - 0.001) < 1e-9
    assert abs(_fee_rate(stressed) - 0.002) < 1e-9, "策略 set_order_cost 不能让压力倍数失效"
    assert stressed.cost_model["cost_scale"] == 2.0 and abs(stressed.cost_model["taker_fee"] - 0.002) < 1e-12
    assert base.cost_model.get("cost_scale") is None and base.cost_model["taker_fee"] == 0.001


def test_stress_also_widens_spread_and_slippage():
    b, s = _run(1.0).fills[0], _run(2.0).fills[0]
    ref = float(b["market_price"] or 0) or float(b["price"])
    slip_b = abs(float(b["price"]) - ref) / ref * 1e4
    slip_s = abs(float(s["price"]) - float(s["market_price"] or ref)) / float(s["market_price"] or ref) * 1e4
    assert abs(slip_s - 2 * slip_b) < 0.5  # 价差一半 + 滑点 = 6 bps → 12 bps


def test_fee_check_explains_ratio():
    c = cost_fee_check({"total_fees": 10.0, "trades": 10}, {"total_fees": 17.0, "trades": 9}, 2.0)
    assert c["total_ratio"] == 1.7 and c["per_trade_ratio"] == round((17 / 9) / 1.0, 3)
    assert "交易笔数 10→9" in c["note"]
    assert cost_fee_check(None, {"total_fees": 1}, 2.0) is None


def test_sweep_limit_and_summary():
    rows = [
        {"scale": 1.0, "ok": True, "passed": True},
        {"scale": 2.0, "ok": True, "passed": True},
        {"scale": 3.0, "ok": True, "passed": False},
        {"scale": 4.0, "ok": True, "passed": True},  # 之后又“达标”不算：极限按第一次失败
    ]
    lim = sweep_limit(rows)
    assert lim == {"pass_max": 2.0, "fail_min": 3.0}
    assert "2×–3×" in sweep_summary(lim, "总收益 > 0")
    assert "1× 基准成本下就不满足" in sweep_summary(sweep_limit([{"scale": 1.0, "ok": True, "passed": False}]), "x")


def test_cost_sweep_runs_in_parallel_and_refines_the_limit(tmp_path):
    """收益在成本 2.5× 以上转负：第一轮 1、2、3 倍找到区间，第二轮在 2–3 之间补 3 档把极限收窄。"""
    import threading
    import time

    seen: list[float] = []
    threads: set[int] = set()
    lock = threading.Lock()

    def backtest(project, code, *, cost_scale=1.0, label=""):
        # 回测本身是纯函数、耗时极短；留一点重叠时间，避免线程池复用单线程导致
        # “是否并行”的断言变成调度相关的偶发失败。
        time.sleep(0.02)
        with lock:
            seen.append(cost_scale)
            threads.add(threading.get_ident())
        ret = 0.05 - 0.02 * cost_scale  # 2.5× 时为 0
        return {
            "ok": True,
            "metrics": {"total_return": ret, "sharpe": ret * 10, "trades": 20, "total_fees": 4.0 * cost_scale},
        }

    tools = LabTools(backtest=backtest, market=lambda *a: {}, templates=lambda q: [])
    lab = StrategyLab(router=None, store=LabStore(JsonStore(tmp_path)), tools=tools)
    p = new_project({"idea": "x"})
    p["code"], p["current_v"] = "def initialize(ctx):\n    pass\n", 1
    lab.store.save(p)
    out = lab.cost_sweep(p["id"], [1, 2, 3])
    assert sorted(seen) == [1.0, 2.0, 2.25, 2.5, 2.75, 3.0]
    assert out["limit"] == {"pass_max": 2.25, "fail_min": 2.5}
    assert len(threads) > 1, "各档应并行回测"
    row2 = next(r for r in out["rows"] if r["scale"] == 2.0)
    assert row2["fee_check"]["per_trade_ratio"] == 2.0
    card = lab.store.messages(p["id"])[-1]
    assert card["kind"] == "cost_sweep" and "2.25×–2.5×" in card["content"]
