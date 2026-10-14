"""v2.0 协议边角的验收测试：回调、五层归因、策略版本、跟单扩展、归因口径。

这些用例对应一轮针对"声称 vs 实现"差距的整改，每条都直接断言行为，而不是断言文档字符串：

- 回调：on_bar / on_fill / on_trade / on_timer 真的会被调用；snapshot / restore 真的随检查点往返
- 五层归因：订单层与权益层真的存在，且缺数据时明确标注而不是编数字
- Context：策略真的能读到自己跑的是哪个版本
- 跟单：三种模式真的按跟随者自己的权益折算；源策略收益不得冒充跟随收益
- 归因：交易所原始成交号真的能直接匹配；账户维度真的参与匹配
"""

from __future__ import annotations

import pytest

from quant_agent.engine.paper import LAYER_ORDER, LiveFill, reconcile
from quant_agent.live.attribution import attribute
from quant_agent.live.copy import (
    COPY_MODES,
    CopyError,
    CopyPolicy,
    assert_not_source_returns,
    follower_report,
    plan_exit,
)
from quant_agent.live.envelope import EnvelopeValidator
from quant_agent.sdk import load_source, run_backtest
from quant_agent.sdk.runtimes import RuntimeCost

ZERO = RuntimeCost(slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1.0)


def _bars(n: int = 40):
    """n 根单调上行的 1 小时 bar（open/high/low/close 都随序号上升）。"""
    from test_ledger import mk

    return mk([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(n)])


# ---------------------------------------------------------------------------
# 1. 回调：声明了就必须真的被调用
# ---------------------------------------------------------------------------
CALLBACKS = """
def initialize(ctx):
    g.bars = 0
    g.fills = 0
    run_timer(3 * 3600 * 1000)   # 每 3 根 1 小时 bar 触发一次 on_timer

def on_bar(ctx, bar):
    g.bars += 1
    if g.bars == 3:
        order_target_percent(ctx.securities[0], 0.5)

def on_timer(ctx):
    record(timer=g.bars)

def on_fill(ctx, fill):
    g.fills += 1

def snapshot(ctx):
    return {"bars": g.bars, "fills": g.fills}

def restore(ctx, state):
    g.bars = state["bars"]
    g.fills = state["fills"]
"""


def test_on_bar_and_on_fill_are_actually_dispatched():
    """on_bar 每根 bar 被调用；成交通知真的进到 on_fill。"""
    src = """
def initialize(ctx):
    g.bars = 0
    g.fills = 0

def on_bar(ctx, bar):
    g.bars += 1
    record(bars=g.bars)
    if g.bars == 3:
        order_target_percent(ctx.securities[0], 0.5)

def on_fill(ctx, fill):
    g.fills += 1
    record(fills=g.fills)
"""
    mod = load_source(src, name="cb")
    res = run_backtest(mod, {"BTC/USDT": _bars(30)}, initial_cash=10_000, cost=ZERO, warmup_bars=0)
    assert res.errors == []
    # on_bar 每根 bar 一次（30 根）
    assert res.records["bars"][-1]["v"] == 30
    # 第 3 根下的单在第 4 根开盘成交，on_fill 至少被调用一次
    assert res.records["fills"][-1]["v"] >= 1


def test_on_timer_fires_on_interval_not_every_bar():
    """run_timer 注册后按间隔触发，而不是每根 bar 都触发。"""
    mod = load_source(CALLBACKS, name="cb")
    res = run_backtest(mod, {"BTC/USDT": _bars(30)}, initial_cash=10_000, cost=ZERO, warmup_bars=0)
    fired = res.records.get("timer") or []
    # 30 根 1 小时 bar、间隔 3 小时 → 远少于 30 次
    assert 0 < len(fired) < 30


def test_on_trade_and_on_order_filled_receive_same_fill():
    """三个成交回调名等价：覆写哪个都能收到成交。"""
    src = """
def initialize(ctx):
    g.seen = []

def handle_data(ctx):
    if ctx.bar_index == 2:
        order_target_percent(ctx.securities[0], 0.5)

def on_trade(ctx, trade):
    record(hit="trade")

def on_order_filled(ctx, trade):
    record(hit="filled")
"""
    mod = load_source(src, name="cb2")
    res = run_backtest(mod, {"BTC/USDT": _bars(20)}, initial_cash=10_000, cost=ZERO, warmup_bars=0)
    hits = {p["v"] for p in res.records["hit"]}
    assert {"trade", "filled"} <= hits


def test_snapshot_and_restore_hooks_round_trip():
    """策略 snapshot 的返回值能取到，restore 能收回同一份状态。"""
    from quant_agent.sdk.runner import StrategyRunner
    from quant_agent.sdk.runtimes import BacktestRuntime

    mod = load_source(CALLBACKS, name="cb3")
    rt = BacktestRuntime({"BTC/USDT": _bars(10)}, initial_cash=1000, cost=ZERO)
    runner = StrategyRunner(mod, rt, rt.ctx)  # 必须共用同一个 Context
    runner.start()
    runner.ctx._g.bars = 7
    state = runner.snapshot_state()
    assert state == {"bars": 7, "fills": 0}
    runner.ctx._g.bars = 0
    runner.restore_state(state)
    assert runner.ctx._g.bars == 7


def test_strategy_can_read_its_own_version_id():
    """v2.0 要求 Context 携带策略版本：策略必须真的读得到。"""
    src = """
def initialize(ctx):
    pass

def handle_data(ctx):
    record(v=ctx.strategy_version_id, via_api=get_strategy_version())
"""
    mod = load_source(src, name="ver")
    res = run_backtest(
        mod,
        {"BTC/USDT": _bars(10)},
        initial_cash=1000,
        cost=ZERO,
        warmup_bars=0,
        strategy_version_id="sv-abc123",
    )
    pts = res.records["v"]
    assert pts and all(p["v"] == "sv-abc123" for p in pts)
    via = res.records["via_api"]
    assert via and all(p["v"] == "sv-abc123" for p in via)


# ---------------------------------------------------------------------------
# 2. 五层归因
# ---------------------------------------------------------------------------
def _sim_fills():
    return [
        {
            "ts": 1000,
            "security": "BTC/USDT",
            "side": "buy",
            "amount": 1.0,
            "price": 100.0,
            "fee": 0.1,
            "client_order_id": "c-1",
            "order_id": "o-1",
            "realized_pnl": 0,
        },
        {
            "ts": 2000,
            "security": "BTC/USDT",
            "side": "sell",
            "amount": 1.0,
            "price": 110.0,
            "fee": 0.11,
            "client_order_id": "c-2",
            "order_id": "o-2",
            "realized_pnl": 10.0,
        },
    ]


def _live_fills():
    return [
        LiveFill(
            ts=1000,
            security="BTC/USDT",
            side="buy",
            amount=1.0,
            price=100.1,
            fee=0.1,
            client_order_id="c-1",
            order_id="o-1",
            fill_id="f-1",
        ),
        LiveFill(
            ts=2000,
            security="BTC/USDT",
            side="sell",
            amount=1.0,
            price=110.2,
            fee=0.11,
            client_order_id="c-2",
            order_id="o-2",
            fill_id="f-2",
        ),
    ]


def test_reconcile_has_all_five_layers_in_order():
    """五层必须齐全且顺序稳定，前端才能按固定顺序渲染。"""
    rep = reconcile(_sim_fills(), _live_fills()).to_dict()
    assert list(rep["layers"]) == list(LAYER_ORDER)
    assert set(LAYER_ORDER) == {"信号层", "订单层", "成交层", "费用层", "权益层"}


def test_order_layer_reports_order_fate_not_just_fills():
    """订单层关心订单的命运：撤单/拒单/未成交要能看出来。"""
    sim_orders = [
        {"order_key": "o-1", "status": "filled"},
        {"order_key": "o-2", "status": "filled"},
        {"order_key": "o-3", "status": "canceled"},
        {"order_key": "o-4", "status": "rejected"},
    ]
    live_orders = [
        {"order_key": "o-1", "status": "filled"},
        {"order_key": "o-2", "status": "filled"},
        {"order_key": "o-9", "status": "filled"},
    ]
    rep = reconcile(_sim_fills(), _live_fills(), simulated_orders=sim_orders, live_orders=live_orders).to_dict()
    layer = rep["layers"]["订单层"]
    assert layer["模拟订单数"] == 4 and layer["实盘订单数"] == 3
    assert layer["未成交单_模拟"] == 2  # canceled + rejected
    assert layer["仅实盘侧"] == 1  # o-9
    assert rep["layer_status"]["订单层"] == "已计算"


def test_order_layer_degrades_honestly_without_order_lists():
    """只给成交时订单层要标注数据不足，不能假装完整。"""
    rep = reconcile(_sim_fills(), _live_fills()).to_dict()
    layer = rep["layers"]["订单层"]
    assert "数据不足" in layer
    assert "退化" in rep["layer_status"]["订单层"]


def test_equity_layer_splits_explained_and_residual():
    """权益层把差额拆成已解释部分与未解释残差，且比较的是变动而非绝对值。"""
    sim_eq = [{"ts": 0, "value": 1000}, {"ts": 3000, "value": 1010}]
    live_eq = [{"ts": 0, "value": 5000}, {"ts": 3000, "value": 5015}]
    rep = reconcile(_sim_fills(), _live_fills(), simulated_equity=sim_eq, live_equity=live_eq).to_dict()
    layer = rep["layers"]["权益层"]
    assert layer["模拟权益变动"] == 10.0
    assert layer["实盘权益变动"] == 15.0
    assert layer["权益变动差"] == 5.0
    assert "未解释残差" in layer
    assert rep["layer_status"]["权益层"] == "已计算"


def test_equity_layer_refuses_to_guess_without_curves():
    """没有权益曲线时不给估算值——权益差不能由成交反推。"""
    rep = reconcile(_sim_fills(), _live_fills()).to_dict()
    layer = rep["layers"]["权益层"]
    assert "数据不足" in layer
    assert "不能由成交记录反推" in layer["说明"]
    assert "未解释残差" not in layer
    assert rep["layer_status"]["权益层"].startswith("数据不足")


def test_equity_layer_ignores_initial_position_difference():
    """实盘账户本来就有底仓时，不该把底仓算成策略收益（比较变动而非绝对值）。"""
    sim_eq = [{"ts": 0, "value": 1000}, {"ts": 1000, "value": 1100}]
    live_eq = [{"ts": 0, "value": 100_000}, {"ts": 1000, "value": 100_100}]
    layer = reconcile(_sim_fills(), _live_fills(), simulated_equity=sim_eq, live_equity=live_eq).to_dict()["layers"][
        "权益层"
    ]
    assert layer["权益变动差"] == 0.0


# ---------------------------------------------------------------------------
# 3. 跟单扩展
# ---------------------------------------------------------------------------
def test_copy_modes_are_all_implemented():
    assert set(COPY_MODES) == {"fixed_amount", "equity_ratio", "risk_budget"}


def test_copy_fixed_amount_is_capped_by_max_weight():
    """固定金额也必须受单标的权重上限约束，否则小账户会开出超过 100% 权益的仓位。"""
    p = CopyPolicy(mode="fixed_amount", fixed_amount="1000", max_weight=0.5, min_order_value="1")
    plan = p.plan(envelope={"signal_id": "s1", "target_weight": 1.0}, equity=1000, price=100)
    assert plan["action"] == "order"
    assert float(plan["notional"]) == 500.0  # 被 max_weight=0.5 削到 500
    assert float(plan["qty"]) == 5.0


def test_copy_equity_ratio_scales_with_follower_equity():
    """权益比例模式按跟随者自己的权益折算，不照搬源策略数量。"""
    p = CopyPolicy(mode="equity_ratio", equity_ratio=0.25, min_order_value="1")
    small = p.plan(envelope={"signal_id": "s", "target_weight": 1.0}, equity=1000, price=100)
    big = p.plan(envelope={"signal_id": "s", "target_weight": 1.0}, equity=100_000, price=100)
    assert float(small["notional"]) == 250.0
    assert float(big["notional"]) == 25_000.0
    assert float(big["qty"]) > float(small["qty"])


def test_copy_risk_budget_derives_size_from_stop_distance():
    """风险预算模式：仓位 = 权益 × 风险预算 / 止损距离。"""
    p = CopyPolicy(mode="risk_budget", risk_budget=0.02, stop_loss_pct=0.05, min_order_value="1")
    plan = p.plan(envelope={"signal_id": "s", "target_weight": 1.0}, equity=10_000, price=50)
    # 200 / 0.05 = 4000
    assert float(plan["notional"]) == 4000.0


def test_copy_respects_source_weight_and_gross_exposure():
    """源策略只要 20% 时不该买满；总敞口到顶时不下单。"""
    p = CopyPolicy(mode="equity_ratio", equity_ratio=1.0, min_order_value="1")
    plan = p.plan(envelope={"signal_id": "s", "target_weight": 0.2}, equity=10_000, price=100)
    assert float(plan["notional"]) == 2000.0

    blocked = p.plan(
        envelope={"signal_id": "s", "target_weight": 1.0},
        equity=10_000,
        price=100,
        gross_exposure=1.0,
    )
    assert blocked["action"] == "skip" and "总敞口" in blocked["reason"]


def test_copy_skips_below_min_order_value():
    p = CopyPolicy(mode="fixed_amount", fixed_amount="5", min_order_value="10")
    plan = p.plan(envelope={"signal_id": "s", "target_weight": 1.0}, equity=10_000, price=100)
    assert plan["action"] == "skip" and "最小下单金额" in plan["reason"]


def test_copy_source_closed_policy_is_explicit():
    """源策略关闭后的三种处置都要有明确行为，不能默认"什么都不做"。"""
    keep = CopyPolicy(mode="equity_ratio", equity_ratio=0.5, on_source_closed="keep")
    assert (
        keep.plan(envelope={"signal_id": "s", "target_weight": 1.0}, equity=1000, price=100, source_closed=True)[
            "action"
        ]
        == "order"
    )

    stop = CopyPolicy(mode="equity_ratio", equity_ratio=0.5, on_source_closed="stop_new")
    out = stop.plan(envelope={"signal_id": "s", "target_weight": 1.0}, equity=1000, price=100, source_closed=True)
    assert out["action"] == "skip" and "stop_new" in out["reason"]

    exit_p = CopyPolicy(mode="equity_ratio", equity_ratio=0.5, on_source_closed="exit")
    plan = plan_exit(exit_p, positions={"BTC/USDT": "0.5"}, prices={"BTC/USDT": "100"})
    assert plan and plan[0]["side"] == "sell" and float(plan[0]["qty"]) == 0.5


def test_copy_rejects_unknown_mode_and_source_closed_action():
    with pytest.raises(CopyError):
        CopyPolicy(mode="turbo")
    with pytest.raises(CopyError):
        CopyPolicy(on_source_closed="whatever")


def test_follower_report_never_presents_source_returns_as_follower_returns():
    """源策略收益只能作为参考信息，不得成为跟随账户业绩。"""
    rep = follower_report(
        fills=[{"ts": 1, "instrument": "BTC/USDT", "side": "buy", "qty": "1", "price": "100", "fee": "0.1"}],
        source_returns={"total_return": 9.99},
    )
    assert rep["basis"] == "跟随账户自己的成交与资金流"
    assert "不是" in rep["reference"]["label"]
    assert "source_returns" in rep["reference"]
    # 顶层不能出现源策略收益字段
    for k in ("source_returns", "source_return", "source_equity", "source_nav"):
        assert k not in rep
    with pytest.raises(CopyError):
        assert_not_source_returns({"source_returns": {"total_return": 1.0}})


# ---------------------------------------------------------------------------
# 4. 归因口径
# ---------------------------------------------------------------------------
def test_attribution_matches_raw_exchange_fill_id():
    """用交易所导出文件里的原始成交号做映射时必须命中，不能静默落到启发式。"""
    sim = [
        {
            "ts": 1000,
            "security": "BTC/USDT",
            "side": "buy",
            "amount": 1.0,
            "price": 100.0,
            "fill_id": "sig-1",
            "signal_id": "sig-1",
        }
    ]
    live = [
        {
            "ts": 1000,
            "instrument": "BTC/USDT",
            "side": "buy",
            "qty": "1",
            "price": "100",
            "fill_id": "x:11",
            "exchange_fill_id": "11",
        }
    ]
    rep = attribute(sim, live, mapping={"11": "sig-1"})
    assert rep.pairs and rep.pairs[0].method == "explicit_mapping"
    assert rep.pairs[0].confidence == 1.0


def test_attribution_refuses_cross_account_match():
    """同一策略挂在多个账户上时，A 账户的成交不能算到 B 账户的模拟结果上。"""
    sim = [{"ts": 1000, "security": "BTC/USDT", "side": "buy", "amount": 1.0, "price": 100.0, "account_id": "acc-A"}]
    live = [{"ts": 1000, "instrument": "BTC/USDT", "side": "buy", "qty": "1", "price": "100", "account_id": "acc-B"}]
    rep = attribute(sim, live)
    assert not rep.pairs
    assert rep.unattributed and rep.unmatched_sim


def test_attribution_still_matches_when_account_matches():
    sim = [{"ts": 1000, "security": "BTC/USDT", "side": "buy", "amount": 1.0, "price": 100.0, "account_id": "acc-A"}]
    live = [{"ts": 1000, "instrument": "BTC/USDT", "side": "buy", "qty": "1", "price": "100", "account_id": "acc-A"}]
    rep = attribute(sim, live)
    assert rep.pairs and rep.pairs[0].method == "heuristic"


# ---------------------------------------------------------------------------
# 5. 信封校验
# ---------------------------------------------------------------------------
def _env(**over):
    base = {
        "schema_version": 1,
        "signal_id": "sig-00000001",
        "source_strategy_version": "sv-1",
        "instrument_id": "BTC/USDT",
        "target_weight": 0.4,
        "created_at": 1000,
        "expires_at": 5000,
    }
    base.update(over)
    return base


def test_envelope_rejects_bad_instrument_id():
    """instrument_id 的格式在 schema 里声明了，check() 也必须真的校验。"""
    v = EnvelopeValidator()
    ok, msg = v.check(_env(instrument_id="not-a-pair"), now=2000)
    assert not ok and "instrument_id" in msg


def test_envelope_rejects_copy_mode_as_unknown_field():
    """信封是封闭协议：未知字段（例如把跟单模式塞进来）必须拒绝。"""
    v = EnvelopeValidator()
    ok, msg = v.check(_env(copy_mode="fixed_amount"), now=2000)
    assert not ok and "未知字段" in msg


def test_envelope_accepts_well_formed():
    v = EnvelopeValidator(max_weight=0.5)
    assert v.check(_env(), now=2000) == (True, "通过")
