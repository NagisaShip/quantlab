"""同账户多策略：内部对冲只送净额、总敞口上限、按比例分回成交、资金分配。"""

from __future__ import annotations

from decimal import Decimal

from test_trade_mgmt import StopBroker, series

from quant_agent.engine.live import LiveRunner
from quant_agent.live import demo_broker as DB
from quant_agent.live.netting import AccountNetter, allocate
from quant_agent.live.oms import DemoOMS, OmsLimits
from quant_agent.sdk import load_source
from quant_agent.sdk.runtimes import RuntimeCost


class FeeBroker(StopBroker):
    """手续费 0.1%，记下每一笔市价单。"""

    def place_market(self, symbol, side, qty, client_order_id):
        self.calls.append(("market", side, client_order_id, qty))
        rep = DB.OrderReport(
            client_order_id, "filled", qty, self.price, qty * self.price * Decimal("0.001"), "USDT", "x", ts=1
        )
        self.orders[client_order_id] = rep
        return rep


def intent(coid, side, qty, px="100", sec="BTC/USDT"):
    return {"client_order_id": coid, "security": sec, "side": side, "qty": str(qty), "ref_price": px}


def oms(price=100):
    b = FeeBroker(price)
    return b, DemoOMS(b, OmsLimits(max_order_notional=1e9, max_orders_per_minute=10_000), initial_cash=100_000)


def test_opposite_orders_cross_internally_and_only_net_is_sent():
    b, o = oms()
    n = AccountNetter(o)
    out = n.process({"A": [intent("a1", "buy", "0.3")], "B": [intent("b1", "sell", "0.2")]})
    markets = [c for c in b.calls if c[0] == "market"]
    assert len(markets) == 1 and markets[0][1] == "buy" and markets[0][3] == Decimal("0.1")
    a, bb = out["A"][0], out["B"][0]
    assert a["status"] == "filled" and Decimal(a["filled_qty"]) == Decimal("0.3")
    assert bb["status"] == "filled" and Decimal(bb["fee"]) == 0 and bb["exchange_order_id"] == "internal"
    # 手续费只按净额 0.1 收：0.1 × 100 × 0.1%
    assert Decimal(a["fee"]) == Decimal("0.01")
    assert n.stats.crossed_notional == 40.0


def test_fully_offsetting_orders_send_nothing():
    b, o = oms()
    out = AccountNetter(o).process({"A": [intent("a1", "buy", "0.2")], "B": [intent("b1", "sell", "0.2")]})
    assert not [c for c in b.calls if c[0] == "market"]
    assert all(r[0]["status"] == "filled" and Decimal(r[0]["fee"]) == 0 for r in out.values())


def test_net_fill_is_split_pro_rata_between_same_side_members():
    b, o = oms(101)
    out = AccountNetter(o).process(
        {"A": [intent("a1", "buy", "0.3")], "B": [intent("b1", "buy", "0.1")], "C": [intent("c1", "sell", "0.2")]}
    )
    assert len([c for c in b.calls if c[0] == "market"]) == 1
    qa, qb = Decimal(out["A"][0]["filled_qty"]), Decimal(out["B"][0]["filled_qty"])
    assert qa == Decimal("0.3") and qb == Decimal("0.1")
    # 内部对冲按参考价 100，送出去的按 101：两人均价相同（按比例）
    assert Decimal(out["A"][0]["avg_price"]) == Decimal(out["B"][0]["avg_price"]) == Decimal("100.5")


def test_rejected_net_order_leaves_crossed_part_filled():
    _b, o = oms()
    o.halt("测试")
    out = AccountNetter(o).process({"A": [intent("a1", "buy", "0.3")], "B": [intent("b1", "sell", "0.2")]})
    assert out["B"][0]["status"] == "filled"
    assert out["A"][0]["status"] == "partial" and Decimal(out["A"][0]["filled_qty"]) == Decimal("0.2")


def test_account_risk_cap_rejects_largest_adds_but_never_reductions():
    _b, o = oms()
    n = AccountNetter(o, risk_cap=0.5, positions_fn=lambda: {"ETH/USDT": "10"}, equity_fn=lambda: 10_000)
    out = n.process(
        {
            "A": [intent("a1", "buy", "40")],  # 加 4000
            "B": [intent("b1", "buy", "10")],  # 加 1000
            "C": [intent("c1", "sell", "5", "100", "ETH/USDT")],  # 减仓，永远放行
        }
    )
    assert out["A"][0]["status"] == "rejected" and out["A"][0]["code"] == "ACCOUNT_RISK_CAP"
    assert out["B"][0]["status"] == "filled" and out["C"][0]["status"] == "filled"
    assert n.stats.capped == 1


def test_shared_account_drops_member_exchange_protects():
    b, o = oms()
    out = AccountNetter(o).process(
        {
            "A": [
                {
                    "kind": "protect",
                    "action": "place",
                    "client_order_id": "p",
                    "security": "BTC/USDT",
                    "side": "sell",
                    "qty": "1",
                    "trigger": "90",
                }
            ]
        }
    )
    assert out == {"A": []} and not b.calls


# ---------------------------------------------------------------- 两个真实内核共用一个账户
FLIP = """
def initialize(ctx):
    g.n = 0

def handle_data(ctx):
    g.n += 1
    s = ctx.securities[0]
    order_target_percent(s, {first} if g.n < 3 else {second})
"""


def test_two_strategies_hedging_on_one_account_send_only_net():
    closes = [100.0] * 8
    bars = series(closes)
    cost = RuntimeCost(taker_fee=0.001, slippage_bps=0)
    live = LiveRunner()
    for sid, (a, c) in {"A": (0.5, 0.1), "B": (0.1, 0.5)}.items():
        live.start(
            sid,
            load_source(FLIP.replace("{first}", str(a)).replace("{second}", str(c)), name=sid),
            {"BTC/USDT": bars[:2]},
            timeframe="1h",
            initial_cash=10_000,
            cost=cost,
            started_at=0,
            fill_mode="external",
        )
    b, o = oms()
    net = AccountNetter(o)
    for bar in bars[2:]:
        b.price = Decimal(str(bar.open))
        batches = {sid: live.step(sid, {"BTC/USDT": [bar]}).get("outbox") or [] for sid in ("A", "B")}
        for sid, reps in net.process(batches).items():
            live.apply_reports(sid, reps)
    markets = [c for c in b.calls if c[0] == "market"]
    # 第一轮两人都买（合并成一笔），换仓那一轮 A 卖 B 买、数量正好相抵：交易所一笔也没有
    assert len(markets) == 1
    for sid, want in (("A", 0.1), ("B", 0.5)):
        rt = live._states[sid].runtime
        pos = rt.ctx.portfolio.positions["BTC/USDT"].amount
        assert abs(float(pos) * 100 / float(rt.ctx.portfolio.total_value) - want) < 0.01
    assert net.stats.crossed_notional > 0


def test_allocate_tilts_by_regime_and_recent_performance():
    good = [0.01, 0.012, 0.008, 0.011, 0.009, 0.01]
    bad = [-0.01, -0.012, -0.008, -0.011, -0.009, -0.01]
    w = allocate(
        {
            "trend": {"base": 1, "returns": good, "regimes": {"range": 0.2}},
            "revert": {"base": 1, "returns": bad, "regimes": {"range": 2.0}},
        }
    )
    assert w["trend"] > w["revert"] and abs(sum(w.values()) - 1) < 1e-9
    w2 = allocate(
        {"trend": {"base": 1, "regimes": {"range": 0.2}}, "revert": {"base": 1, "regimes": {"range": 2.0}}},
        regime="range",
    )
    assert w2["revert"] > w2["trend"]
