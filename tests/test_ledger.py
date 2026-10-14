"""Decimal 复式账本、撮合规则与指标的黄金样例（plan v2.0 必测项）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quant_agent.engine.metrics import drawdown_details
from quant_agent.exchanges.base import Bar
from quant_agent.sdk import load_source, run_backtest
from quant_agent.sdk.ledger import InsufficientFunds, Ledger, LedgerError, dstr
from quant_agent.sdk.objects import Portfolio, ReadOnlyError
from quant_agent.sdk.runtimes import BacktestRuntime, RiskConfig, RuntimeCost

H = 3_600_000
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def mk(rows, symbol="BTC/USDT"):
    """rows: [(open, high, low, close[, volume])]"""
    out = []
    for i, r in enumerate(rows):
        o, h, lo, c = r[:4]
        v = r[4] if len(r) > 4 else 1_000.0
        out.append(
            Bar(
                symbol=symbol,
                timeframe="1h",
                open_time=T0 + i * H,
                close_time=T0 + (i + 1) * H,
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=v,
            )
        )
    return out


ZERO_COST = dict(slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1.0)


# ---------------------------------------------------------------- 黄金样例：记账
def test_golden_fixture_cash_and_equity_exact():
    led = Ledger()
    led.deposit(1000)
    led.trade(instrument="BTC/USDT", side="buy", qty=1, price=100, fee="0.1")
    assert led.cash() == Decimal("899.9")
    led.mark({"BTC/USDT": 110})
    assert led.equity() == Decimal("1009.9")
    booking = led.trade(instrument="BTC/USDT", side="sell", qty=1, price=110, fee="0.11")
    assert led.cash() == Decimal("1009.79")
    assert led.equity() == Decimal("1009.79")
    assert booking.realized == Decimal("9.79") and booking.closed
    assert all(v == 0 for v in led.trial_balance().values())
    assert led.fees_value == Decimal("0.21")


def test_float_inputs_do_not_leak_binary_error():
    led = Ledger()
    led.deposit(1000.0)
    led.trade(instrument="BTC/USDT", side="buy", qty=1.0, price=100.0, fee=0.1)
    led.trade(instrument="BTC/USDT", side="sell", qty=1.0, price=110.0, fee=0.11)
    assert str(led.cash()) == "1009.79"  # float 版会得到 1009.7900000000001


def test_ledger_rejects_overdraft_and_oversell():
    led = Ledger()
    led.deposit(100)
    with pytest.raises(InsufficientFunds):
        led.trade(instrument="BTC/USDT", side="buy", qty=1, price=100, fee="0.1")
    with pytest.raises(InsufficientFunds):
        led.trade(instrument="BTC/USDT", side="sell", qty=1, price=100)
    with pytest.raises(InsufficientFunds):
        led.withdraw(101)
    with pytest.raises(LedgerError):
        led.deposit(-5)


def test_fee_in_received_currency():
    led = Ledger()
    led.deposit(1000)
    b = led.trade(instrument="BTC/USDT", side="buy", qty=1, price=100, fee="0.001", fee_currency="BTC")
    assert led.cash() == Decimal("900")
    assert led.holdings["BTC/USDT"].qty == Decimal("0.999")
    assert b.fee_value == Decimal("0.1")
    assert led.fees_by_currency == {"BTC": Decimal("0.001")}
    assert all(v == 0 for v in led.trial_balance().values())


# ---------------------------------------------------------------- 第三币种手续费（BNB 抵扣）
def test_golden_third_currency_fee_is_converted_at_given_rate():
    """0.01 BNB @600 USDT/BNB → 总手续费 "6" USDT（详见 tests/test_fee_conversion.py）。"""
    led = Ledger()
    led.deposit(100_000)
    b = led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source="binance:BNBUSDT@1791850800000",
    )
    assert dstr(led.fees_value) == "6"
    assert b.fee_value == Decimal("6") and b.fee_converted
    assert led.unconverted_fees == {}
    assert all(v == 0 for v in led.trial_balance().values())


def test_third_currency_fee_without_rate_is_not_estimated():
    """拿不到汇率时不估算：费用单列在 unconverted_fees，不进 fees_value。"""
    led = Ledger()
    led.deposit(100_000)
    b = led.trade(instrument="BTC/USDT", side="buy", qty=1, price=60_000, fee="0.01", fee_currency="BNB")
    assert b.fee_value == Decimal("0") and not b.fee_converted
    assert led.fees_value == Decimal("0")
    assert led.unconverted_fees == {"BNB": Decimal("0.01")}
    assert led.unconverted_fees_report()["by_currency"] == {"BNB": "0.01"}
    # 借贷仍然平衡，费用仍记在 BNB 上
    assert led.balance("expenses:fees", "BNB") == Decimal("0.01")
    assert all(v == 0 for v in led.trial_balance().values())


def test_third_currency_fee_requires_rate_source():
    """给了汇率却不给来源无法复核，直接拒绝。"""
    led = Ledger()
    led.deposit(100_000)
    with pytest.raises(LedgerError):
        led.trade(instrument="BTC/USDT", side="buy", qty=1, price=60_000, fee="0.01", fee_currency="BNB", fee_rate=600)


def test_ledger_snapshot_roundtrip():
    led = Ledger()
    led.deposit(1000)
    led.trade(instrument="BTC/USDT", side="buy", qty="0.3", price="123.45", fee="0.037035")
    again = Ledger.from_dict(led.to_dict())
    assert again.cash() == led.cash() and again.holdings["BTC/USDT"].cost == led.holdings["BTC/USDT"].cost


def test_portfolio_is_read_only():
    p = Portfolio(starting_cash=1000)
    with pytest.raises(ReadOnlyError):
        p.available_cash = 1e9
    pos = p.position("BTC/USDT")
    with pytest.raises(ReadOnlyError):
        pos.amount = 5


def test_strategy_cannot_replace_portfolio():
    src = """
def initialize(ctx):
    pass

def handle_data(ctx):
    ctx.portfolio = None
"""
    bars = mk([(100, 101, 99, 100)] * 5)
    r = run_backtest(load_source(src), {"BTC/USDT": bars})
    assert any("只读" in e for e in r.errors)
    assert r.ledger["balanced"]


# ---------------------------------------------------------------- 黄金样例：回撤
def test_golden_drawdown_details():
    d = drawdown_details([100, 110, 88, 99, 121], [1, 2, 3, 4, 5])
    assert d["max_drawdown"] == pytest.approx(0.2)
    assert (d["peak_index"], d["trough_index"], d["recovery_index"]) == (1, 2, 4)
    assert d["recovered"] is True and d["recovery_ts"] == 5
    d2 = drawdown_details([100, 110, 88, 99])
    assert d2["recovered"] is False and d2["recovery_index"] is None


# ---------------------------------------------------------------- 资金不足默认拒绝
BUY_QTY = """
def initialize(ctx):
    pass

def handle_data(ctx):
    if ctx.bar_index == 1:
        order(ctx.securities[0], {qty})
"""


def test_insufficient_funds_rejected_by_default():
    bars = mk([(100, 101, 99, 100)] * 4)
    r = run_backtest(
        load_source(BUY_QTY.format(qty=20)), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST)
    )
    assert r.fills == []
    assert r.rejected and r.rejected[0]["code"] == "INSUFFICIENT_FUNDS"
    assert r.signals[0]["status"] == "rejected"


def test_insufficient_funds_reduce_when_explicit():
    bars = mk([(100, 101, 99, 100)] * 4)
    r = run_backtest(
        load_source(BUY_QTY.format(qty=20)),
        {"BTC/USDT": bars},
        initial_cash=1000,
        cost=RuntimeCost(**ZERO_COST, insufficient_funds="reduce"),
    )
    assert len(r.fills) == 1 and r.fills[0]["partial"] is True
    assert Decimal(r.fills[0]["amount"]) * 100 * Decimal("1.001") <= 1000


def test_oversell_rejected():
    src = """
def initialize(ctx):
    pass

def handle_data(ctx):
    if ctx.bar_index == 1:
        order(ctx.securities[0], 1)
    if ctx.bar_index == 3:
        order(ctx.securities[0], -5)
"""
    bars = mk([(100, 101, 99, 100)] * 6)
    r = run_backtest(load_source(src), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST))
    assert [f["side"] for f in r.fills] == ["buy"]
    assert r.rejected[-1]["code"] == "INSUFFICIENT_POSITION"


# ---------------------------------------------------------------- 未平仓不计为完成交易
def test_open_position_is_not_a_completed_trade():
    bars = mk([(100, 101, 99, 100)] * 3 + [(100, 121, 99, 120)] * 3)
    r = run_backtest(
        load_source(BUY_QTY.format(qty=1)), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST)
    )
    assert r.metrics["trades"] == 0 and r.metrics["win_rate"] is None
    assert len(r.open_positions) == 1 and r.round_trips == []
    assert r.metrics["open_positions"] == 1
    assert all(f["reason"] != "final_liquidation" for f in r.fills)


def test_round_trip_return_and_holding_time():
    src = """
def initialize(ctx):
    pass

def handle_data(ctx):
    if ctx.bar_index == 1:
        order(ctx.securities[0], 1)
    if ctx.bar_index == 3:
        order_target(ctx.securities[0], 0)
"""
    bars = mk([(100, 100, 100, 100)] * 3 + [(110, 110, 110, 110)] * 4)
    r = run_backtest(
        load_source(src), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST, taker_fee=0.001)
    )
    assert len(r.round_trips) == 1
    trip = r.round_trips[0]
    # 买 1@100 费 0.1；卖 1@110 费 0.11 → 实现 9.79，投入 100.1
    assert Decimal(trip["realized"]) == Decimal("9.79")
    assert trip["holding_ms"] == 2 * H
    assert r.ledger["final_equity"] == "1009.79"
    assert r.metrics["avg_holding_hours"] == pytest.approx(2)


# ---------------------------------------------------------------- 限价单保守模型
LIMIT = """
def initialize(ctx):
    pass

def handle_data(ctx):
    if ctx.bar_index == 1:
        order(ctx.securities[0], 1, limit_price=95, expire_bars=10)
"""


def test_limit_touch_is_ambiguous_and_not_filled():
    bars = mk([(100, 101, 99, 100), (100, 101, 95, 100), (100, 101, 96, 100)])
    r = run_backtest(load_source(LIMIT), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST))
    assert r.fills == []
    assert r.counters["limit_touch_ambiguous"] == 1


def test_limit_strict_cross_fills_at_limit_as_maker():
    bars = mk([(100, 101, 99, 100), (100, 101, 94.9, 100), (100, 101, 96, 100)])
    r = run_backtest(load_source(LIMIT), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST))
    assert len(r.fills) == 1
    assert Decimal(r.fills[0]["price"]) == 95 and r.fills[0]["liquidity"] == "maker"


def test_limit_gap_fills_at_open():
    bars = mk([(100, 101, 99, 100), (90, 92, 89, 91)])
    r = run_backtest(load_source(LIMIT), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST))
    assert Decimal(r.fills[0]["price"]) == 90


# ---------------------------------------------------------------- 止损止盈：同根止损优先
def test_stop_loss_first_when_both_hit_same_bar():
    src = """
def initialize(ctx):
    set_stop_loss(ctx.securities[0], 0.05)
    set_take_profit(ctx.securities[0], 0.05)

def handle_data(ctx):
    if ctx.bar_index == 1:
        order(ctx.securities[0], 1)
"""
    bars = mk([(100, 100, 100, 100), (100, 100, 100, 100), (100, 106, 94, 100), (100, 100, 100, 100)])
    r = run_backtest(load_source(src), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST))
    sells = [f for f in r.fills if f["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["reason"] == "stop_loss"
    assert Decimal(sells[0]["price"]) == 95
    assert r.counters["bracket_same_bar"] == 1


# ---------------------------------------------------------------- 风控
def test_risk_max_weight_clips_target_and_rejects_explicit():
    pct = """
def initialize(ctx):
    pass

def handle_data(ctx):
    if ctx.bar_index == 1:
        order_target_percent(ctx.securities[0], 1.0)
"""
    bars = mk([(100, 100, 100, 100)] * 4)
    r = run_backtest(
        load_source(pct),
        {"BTC/USDT": bars},
        initial_cash=1000,
        cost=RuntimeCost(**ZERO_COST),
        risk=RiskConfig(max_position_weight=0.5),
    )
    assert Decimal(r.fills[0]["amount"]) == 5
    r2 = run_backtest(
        load_source(BUY_QTY.format(qty=8)),
        {"BTC/USDT": bars},
        initial_cash=1000,
        cost=RuntimeCost(**ZERO_COST),
        risk=RiskConfig(max_position_weight=0.5),
    )
    assert r2.fills == [] and r2.rejected[0]["code"] == "RISK_LIMIT"


# ---------------------------------------------------------------- 资金流与时间加权收益
def test_deposit_does_not_inflate_twr():
    hold = """
def initialize(ctx):
    pass

def handle_data(ctx):
    pass
"""
    bars = mk([(100, 100, 100, 100)] * 6)
    r = run_backtest(
        load_source(hold), {"BTC/USDT": bars}, initial_cash=1000, cash_flows=[{"ts": T0 + 3 * H, "amount": 1000}]
    )
    assert r.equity_curve[-1] == 2000
    assert r.unit_nav[-1] == pytest.approx(1.0)
    assert r.metrics["total_return"] == pytest.approx(0.0)
    assert r.metrics["return_basis"] == "time_weighted_unit_nav"
    assert r.ledger["net_contributions"] == "2000"


# ---------------------------------------------------------------- 信号与去重
def test_signal_ids_are_deterministic_and_events_deduplicated():
    bars = mk([(100, 100, 100, 100)] * 5)
    a = run_backtest(load_source(BUY_QTY.format(qty=1)), {"BTC/USDT": bars}, run_id="r1", strategy_version_id="v1")
    b = run_backtest(load_source(BUY_QTY.format(qty=1)), {"BTC/USDT": bars}, run_id="r1", strategy_version_id="v1")
    assert a.signals[0]["signal_id"] == b.signals[0]["signal_id"]
    assert a.fills[0]["signal_id"] == a.signals[0]["signal_id"]
    rt = BacktestRuntime({"BTC/USDT": bars})
    assert rt.accept_event(T0 + H) and not rt.accept_event(T0 + H) and not rt.accept_event(T0)
    assert rt.counters["duplicate_events"] == 2


def test_snapshot_restore_does_not_duplicate_orders():
    from quant_agent.sdk.runner import StrategyRunner

    bars = mk([(100, 100, 100, 100)] * 6)
    rt = BacktestRuntime({"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST), run_id="x")
    rt.ctx.securities = ["BTC/USDT"]
    mod = load_source(BUY_QTY.format(qty=1))
    runner = StrategyRunner(mod, rt, rt.ctx)
    runner.start()
    ts1 = bars[0].close_time
    rt.advance(ts1)
    rt.accept_event(ts1)
    runner.on_bar(ts1)  # bar_index 1 → 下单（排队）
    snap = rt.snapshot()
    # 崩溃后恢复：新 runtime 从检查点继续，再次收到同一事件也不会重复下单
    rt2 = BacktestRuntime({"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO_COST), run_id="x")
    rt2.restore(snap)
    assert not rt2.accept_event(ts1)
    ts2 = bars[1].close_time
    rt2.advance(ts2)
    assert rt2.accept_event(ts2)
    rt2.execute()
    assert len(rt2.fills) == 1 and rt2.ledger.cash() == Decimal("899.9")


# ---------------------------------------------------------------- 研究：锁定测试区间与 walk-forward
def test_optimizer_holdout_and_walk_forward():
    from quant_agent.engine.research import BacktestSpec, optimize, walk_forward
    from quant_agent.strategies.sdk_templates import get_template

    tpl = get_template("ma_cross")
    import math

    rows = [(100 + 10 * math.sin(i / 15), 0, 0, 0) for i in range(900)]
    bars = mk([(o, o * 1.01, o * 0.99, o * 1.001) for o, *_ in rows])
    spec = BacktestSpec(warmup_bars=60, params={})
    out = optimize(
        tpl.code,
        name="t",
        bars={"BTC/USDT": bars},
        spec=spec,
        grid={"fast": [5, 10], "slow": [30]},
        holdout=0.2,
        embargo_bars=5,
    )
    assert out["holdout_from"] and "holdout" in out["diagnostics"]
    wf = walk_forward(
        tpl.code, name="t", bars={"BTC/USDT": bars}, spec=spec, grid={"fast": [5, 10], "slow": [30]}, folds=3
    )
    assert len(wf["folds"]) == 3 and all("test" in f for f in wf["folds"])


def test_recurring_deposits_do_not_count_as_return():
    from quant_agent.engine.research import BacktestSpec, execute

    hold = "def initialize(ctx):\n    pass\n\ndef handle_data(ctx):\n    pass\n"
    bars = mk([(100, 100, 100, 100)] * (24 * 30))
    spec = BacktestSpec(warmup_bars=0, initial_cash=1000, recurring_deposit=500, recurring_every_days=7)
    r = execute(hold, name="dca", bars={"BTC/USDT": bars}, spec=spec)
    assert len(r.flows) == 4 and r.equity_curve[-1] == 3000
    assert r.metrics["total_return"] == pytest.approx(0.0) and r.metrics["return_basis"] == "time_weighted_unit_nav"
