"""永续合约：做空、杠杆、逐仓保证金、资金费、强平。

黄金样例（手算可复核）：
    入金 1000；10 倍杠杆做空 1 @100，手续费 0.05 → 保证金 10，现金 989.95；
    强平价（空）= 100 × (1 + 1/10) / (1 + 0.005) = 109.4527…；
    费率 0.0001、标记价 95 结算 → 空头收 0.0095；
    买入 2 @95（反手）：平空盈利 5、释放保证金 10，再开多 1、占用保证金 9.5，手续费 0.1 → 现金 995.3595；
    多头在强平价被强平：损失全部保证金 9.5，权益 = 现金 = 995.3595。
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from test_demo_trading import FakeBroker

from quant_agent.data.cache import BarCache
from quant_agent.data.service import MarketDataService, synthetic_bars, synthetic_funding
from quant_agent.engine.live import LiveRunner
from quant_agent.exchanges.base import Bar, UnsupportedFeature
from quant_agent.exchanges.binance import BinanceAdapter
from quant_agent.exchanges.okx import inst_id_of
from quant_agent.live import demo_broker as DB
from quant_agent.live.oms import DemoOMS, OmsLimits
from quant_agent.sdk import load_source
from quant_agent.sdk.ledger import LIQUIDATION, MARGIN, Ledger, dec
from quant_agent.sdk.runtimes import RuntimeCost, run_backtest
from quant_agent.strategies.sdk_templates import get_template

PERP = "BTC/USDT:PERP"
H = 3_600_000
ZERO_COST = RuntimeCost(
    taker_fee=0,
    maker_fee=0,
    slippage_bps=0,
    spread_bps=0,
    min_notional=0,
    rebalance_tolerance=0,
    max_volume_participation=1.0,
)


def _bars(symbol: str, rows: list[tuple[float, float, float, float]], start: int = 0) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timeframe="1h",
            open_time=start + i * H,
            close_time=start + (i + 1) * H,
            open=o,
            high=h,
            low=lo,
            close=c,
            volume=1_000,
        )
        for i, (o, h, lo, c) in enumerate(rows)
    ]


# ---------------------------------------------------------------- 账本黄金样例
def test_ledger_golden_short_funding_flip_liquidation():
    led = Ledger()
    led.deposit(1000)
    led.set_leverage(PERP, 10)
    led.perp_trade(instrument=PERP, side="sell", qty=1, price=100, fee="0.05")
    pos = led.perps[PERP]
    assert (pos.qty, pos.margin, led.cash()) == (Decimal(-1), Decimal(10), Decimal("989.95"))
    assert pos.liquidation_price.quantize(Decimal("1e-6")) == Decimal("109.452736")
    led.mark({PERP: 95})
    assert led.equity() == Decimal("1004.95")  # 989.95 + 保证金 10 + 浮盈 5

    assert led.settle_funding(PERP, rate="0.0001", mark_price=95) == Decimal("0.0095")
    b = led.perp_trade(instrument=PERP, side="buy", qty=2, price=95, fee="0.1")
    assert b.realized == Decimal("4.9")
    assert (pos.qty, pos.entry, pos.margin) == (Decimal(1), Decimal(95), Decimal("9.5"))
    assert led.cash() == Decimal("995.3595")
    assert pos.liquidation_price.quantize(Decimal("1e-6")) == Decimal("85.929648")  # 95 × 0.9 / 0.995

    liq = led.liquidate(PERP, price=pos.liquidation_price)
    assert liq.realized == Decimal("-9.5")
    assert led.equity() == led.cash() == Decimal("995.3595")
    assert all(v == 0 for v in led.trial_balance().values())
    assert led.balance(MARGIN, "USDT") == 0 and led.balance(LIQUIDATION, "USDT") > 0
    assert led.liquidation_loss == Decimal("9.5")

    again = Ledger.from_dict(json.loads(json.dumps(led.to_dict())))
    assert again.perps[PERP].liquidations == 1 and again.equity() == led.equity()


def test_liquidation_price_formula():
    led = Ledger()
    led.deposit(10_000)
    led.set_leverage(PERP, 10)
    led.perp_trade(instrument=PERP, side="buy", qty=1, price=100)
    assert led.perps[PERP].liquidation_price.quantize(Decimal("1e-6")) == Decimal("90.452261")  # 90 / 0.995


def test_margin_is_checked_before_opening():
    led = Ledger()
    led.deposit(100)
    led.set_leverage(PERP, 2)
    with pytest.raises(Exception, match="不足"):
        led.perp_trade(instrument=PERP, side="sell", qty=3, price=100)  # 需要保证金 150
    led.perp_trade(instrument=PERP, side="sell", qty=1, price=100)


# ---------------------------------------------------------------- 运行内核
def test_runtime_liquidates_short_on_bar_high():
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (100, 105, 99, 104), (104, 112, 103, 111), (111, 111, 110, 110)]
    mod = load_source(
        """
def initialize(ctx):
    set_leverage("BTC/USDT:PERP", 10)
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USDT:PERP", -1.0)
""",
        name="short10x",
    )
    r = run_backtest(mod, {PERP: _bars(PERP, rows)}, initial_cash=1000, cost=ZERO_COST)
    liq = [f for f in r.fills if f["reason"] == "liquidation"]
    assert len(liq) == 1 and liq[0]["side"] == "buy"
    opened = next(f for f in r.fills if f["reason"] != "liquidation")
    assert opened["side"] == "sell" and Decimal(opened["amount"]) == Decimal(10)  # 权益 1000 / 价格 100
    # 保证金 = 10 × 100 / 10 = 100，强平后全部损失
    assert Decimal(r.ledger["final_equity"]) == Decimal(900)
    assert r.metrics["liquidations"] == 1 and r.ledger["balanced"]
    assert any("被强平" in x["message"] for x in r.logs)


def test_runtime_settles_funding_at_each_settlement_point():
    rows = [(100, 100, 100, 100)] * 30
    bars = _bars(PERP, rows, start=0)
    times = [8 * H, 16 * H, 24 * H]
    research = {f"funding:{PERP}": {"times": times, "rates": ["0.0001"] * 3, "source": "test"}}
    mod = load_source(
        """
def initialize(ctx):
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USDT:PERP", -0.5)
""",
        name="short",
    )
    r = run_backtest(mod, {PERP: bars}, initial_cash=1000, cost=ZERO_COST, research=research)
    f = r.ledger["funding"]
    # 空 5 张 × 标记价 100 × 0.01% × 3 次 = 0.15，空头收取
    assert f["events"] == 3 and Decimal(f["received"]) == Decimal("0.15") and Decimal(f["paid"]) == 0
    assert Decimal(r.ledger["final_equity"]) == Decimal("1000.15")
    assert r.ledger["balanced"]


def test_missing_funding_data_is_warned_not_invented():
    mod = load_source(
        """
def handle_data(ctx):
    order_target_percent("BTC/USDT:PERP", -0.5)
""",
        name="nofund",
    )
    r = run_backtest(mod, {PERP: _bars(PERP, [(100, 100, 100, 100)] * 5)}, initial_cash=1000, cost=ZERO_COST)
    assert r.ledger["funding"]["events"] == 0
    assert any("没有资金费率数据" in x["message"] for x in r.logs)


def test_spot_still_cannot_short_and_leverage_is_perp_only():
    mod = load_source(
        """
def handle_data(ctx):
    order("BTC/USDT", -1)
""",
        name="spotshort",
    )
    r = run_backtest(
        mod, {"BTC/USDT": _bars("BTC/USDT", [(100, 100, 100, 100)] * 4)}, initial_cash=1000, cost=ZERO_COST
    )
    assert not r.fills and any(x["code"] == "INSUFFICIENT_POSITION" for x in r.rejected)
    bad = load_source(
        """
def initialize(ctx):
    set_leverage("BTC/USDT", 3)
""",
        name="lev",
    )
    with pytest.raises(Exception, match="不是永续合约"):
        run_backtest(bad, {"BTC/USDT": _bars("BTC/USDT", [(100, 100, 100, 100)] * 3)}, initial_cash=1000)


def test_perp_stop_loss_for_short_triggers_on_rise():
    rows = [(100, 100, 100, 100), (100, 100, 100, 100), (100, 103, 99, 102), (102, 107, 101, 106)]
    mod = load_source(
        """
def initialize(ctx):
    set_stop_loss("BTC/USDT:PERP", 0.05)
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USDT:PERP", -0.5)
""",
        name="sl",
    )
    r = run_backtest(mod, {PERP: _bars(PERP, rows)}, initial_cash=1000, cost=ZERO_COST)
    sl = [f for f in r.fills if f["reason"] == "stop_loss"]
    assert len(sl) == 1 and sl[0]["side"] == "buy" and Decimal(sl[0]["price"]) == Decimal(105)


def test_portfolio_view_shows_short_side_margin_and_liq_price():
    mod = load_source(
        """
def initialize(ctx):
    set_leverage("BTC/USDT:PERP", 5)

def handle_data(ctx):
    order_target_percent("BTC/USDT:PERP", -0.5)
""",
        name="view",
    )
    r = run_backtest(mod, {PERP: _bars(PERP, [(100, 100, 100, 100)] * 4)}, initial_cash=1000, cost=ZERO_COST)
    p = r.portfolio["positions"][PERP]
    assert p["side"] == "short" and Decimal(p["amount"]) == Decimal(-5) and Decimal(p["margin"]) == Decimal(100)
    assert Decimal(p["liquidation_price"]) > 100


# ---------------------------------------------------------------- 资金费套利：三种模式
def _arb_inputs(n: int = 400):
    t = get_template("funding_arb")
    bars = {s: synthetic_bars(s, "1h", n) for s in t.securities}
    rows = synthetic_funding(PERP, bars[PERP][0].open_time, bars[PERP][-1].close_time)
    return t, bars, rows


def test_funding_arb_backtest_is_hedged_and_earns_funding():
    t, bars, rows = _arb_inputs(800)
    research = {f"funding:{PERP}": {"times": [a for a, _ in rows], "rates": [b for _, b in rows], "source": "s"}}
    r = run_backtest(load_source(t.code, name="arb"), bars, research=research, warmup_bars=10)
    assert not r.errors and r.ledger["balanced"]
    legs = {f["security"] for f in r.fills}
    assert legs == {"BTC/USDT", PERP}
    assert Decimal(r.ledger["funding"]["received"]) > 0
    # 对冲：两腿方向相反、数量接近
    pos = r.portfolio["positions"]
    if PERP in pos:
        assert Decimal(pos[PERP]["amount"]) < 0
        assert abs(Decimal(pos["BTC/USDT"]["amount"]) + Decimal(pos[PERP]["amount"])) < Decimal("0.01")


def _drive_live(fill_mode: str, broker=None):
    t, bars, rows = _arb_inputs(400)
    live = LiveRunner()
    hist = {s: b[:300] for s, b in bars.items()}
    live.start(
        "arb",
        load_source(t.code, name="arb"),
        hist,
        timeframe="1h",
        initial_cash=10_000,
        cost=RuntimeCost(),
        started_at=0,
        fill_mode=fill_mode,
    )
    oms = (
        DemoOMS(
            broker, OmsLimits(max_order_notional=100_000, max_orders_per_minute=0), initial_cash=10_000, poll_seconds=0
        )
        if broker
        else None
    )
    if oms:
        oms.snapshot_baseline()
    funding = {PERP: [list(r) for r in rows]}
    fills = []
    for i in range(300, 400):
        latest = {s: [b[i]] for s, b in bars.items()}
        if broker:
            broker.price = Decimal(str(bars["BTC/USDT"][i].open))
        out = live.step("arb", latest, funding=funding)
        fills += out["fills"]
        if oms and out["outbox"]:
            reports = oms.process(out["outbox"], [])
            fills += live.apply_reports("arb", reports)["fills"]
    return live._states["arb"].runtime, fills, oms


def test_funding_arb_paper_mode():
    rt, fills, _ = _drive_live("internal")
    assert {f["security"] for f in fills} == {"BTC/USDT", PERP}
    assert rt.ledger.funding_received > 0
    assert all(v == 0 for v in rt.ledger.trial_balance().values())


class FakePerpBroker(FakeBroker):
    """在 FakeBroker 上加永续：逐仓保证金不动钱包，只记持仓、手续费和已实现盈亏。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.pos: dict[str, Decimal] = {}
        self.entry: dict[str, Decimal] = {}
        self.prepared: list[tuple[str, Decimal]] = []

    def prepare_perp(self, symbol, leverage):
        self.prepared.append((symbol, leverage))

    def positions(self):
        return {k: v for k, v in self.pos.items() if v}

    def place_market(self, symbol, side, qty, client_order_id):
        if not DB.is_perp(symbol):
            return super().place_market(symbol, side, qty, client_order_id)
        fee = (qty * self.price * Decimal("0.0005")).quantize(Decimal("0.00000001"))
        self.pos[symbol] = self.pos.get(symbol, Decimal(0)) + (qty if side == "buy" else -qty)
        self.bal["USDT"] -= fee
        rep = DB.OrderReport(client_order_id, "filled", qty, self.price, fee, "USDT", f"p{len(self.orders)}", ts=1)
        self.orders[client_order_id] = rep
        self.placed.append((symbol, side, qty, client_order_id))
        return rep


def test_funding_arb_demo_mode_routes_both_legs_with_perp_flags():
    broker = FakePerpBroker(usdt="50000")
    broker.use_futures = True
    rt, _fills, oms = _drive_live("external", broker)
    perp_orders = [p for p in broker.placed if DB.is_perp(p[0])]
    spot_orders = [p for p in broker.placed if not DB.is_perp(p[0])]
    assert perp_orders and spot_orders
    assert broker.prepared and broker.prepared[0][0] == PERP
    assert perp_orders[0][1] == "sell"  # 做空腿
    assert rt.ledger.perps[PERP].qty == broker.pos[PERP]
    rec = oms.reconcile(
        rt.ledger.cash(),
        {k: h.qty for k, h in rt.ledger.holdings.items()},
        perps={k: p.qty for k, p in rt.ledger.perps.items() if p.qty},
        margin=sum((p.margin for p in rt.ledger.perps.values()), Decimal(0)),
    )
    assert any(r["asset"] == PERP and r["ok"] for r in rec["rows"])


def test_oms_halt_blocks_opening_short_but_allows_closing():
    oms = DemoOMS(FakePerpBroker(), OmsLimits(max_orders_per_minute=0), poll_seconds=0)
    oms.halt("测试")
    base = {
        "security": PERP,
        "qty": "1",
        "ref_price": "100",
        "signal_id": "s",
        "reason": "t",
        "perp": True,
        "leverage": "1",
    }
    r1 = oms.submit({**base, "client_order_id": "a", "side": "sell", "opens": True})
    assert r1["status"] == "rejected" and "熔断" in r1["message"]
    r2 = oms.submit({**base, "client_order_id": "b", "side": "buy", "opens": False})
    assert r2["status"] == "filled"


# ---------------------------------------------------------------- 交易所接口
def test_binance_perp_demo_uses_futures_demo_host():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        p = req.url.path
        if p == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change margin type."})
        if p == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 2})
        if p == "/fapi/v1/order":
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "q1",
                    "orderId": 5,
                    "status": "FILLED",
                    "executedQty": "0.01",
                    "avgPrice": "60000",
                    "updateTime": 9,
                },
            )
        if p == "/fapi/v1/userTrades":
            return httpx.Response(200, json=[{"commission": "0.24", "commissionAsset": "USDT"}])
        if p == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "positionAmt": "-0.01"}])
        return httpx.Response(404, json={})

    b = DB.make_broker("binance", "demo", {"api_key": "k", "api_secret": "s"}, transport=httpx.MockTransport(handler))
    b.prepare_perp(PERP, Decimal(2))
    rep = b.place_market(PERP, "sell", Decimal("0.01"), "q1")
    assert rep.status == "filled" and rep.avg_price == Decimal(60000) and rep.fee == Decimal("0.24")
    assert b.positions() == {PERP: Decimal("-0.01")}
    assert {r.url.host for r in seen} == {"demo-fapi.binance.com"}


def test_okx_perp_orders_in_contracts_isolated():
    bodies = []

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        assert req.headers["x-simulated-trading"] == "1"
        if p == "/api/v5/public/instruments":
            return httpx.Response(200, json={"code": "0", "data": [{"ctVal": "0.01", "lotSz": "0.1", "minSz": "0.1"}]})
        if p == "/api/v5/trade/order" and req.method == "POST":
            bodies.append(json.loads(req.content))
            return httpx.Response(200, json={"code": "0", "data": [{"ordId": "1", "sCode": "0"}]})
        if p == "/api/v5/trade/order":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [{"state": "filled", "accFillSz": "3", "avgPx": "60000", "fee": "-0.1", "feeCcy": "USDT"}],
                },
            )
        return httpx.Response(200, json={"code": "0", "data": []})

    b = DB.make_broker(
        "okx", "demo", {"api_key": "k", "api_secret": "s", "passphrase": "p"}, transport=httpx.MockTransport(handler)
    )
    assert b.rules(PERP).step == Decimal("0.001")
    b.place_market(PERP, "sell", Decimal("0.03"), "q1")
    assert bodies[0]["instId"] == "BTC-USDT-SWAP" and bodies[0]["tdMode"] == "isolated"
    assert Decimal(bodies[0]["sz"]) == 3 and "tgtCcy" not in bodies[0]
    assert b.query(PERP, "q1").filled_qty == Decimal("0.03")


def test_binance_adapter_fetches_perp_klines_and_funding():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "fapi.binance.com"
        if req.url.path == "/fapi/v1/fundingRate":
            return httpx.Response(
                200,
                json=[
                    {"fundingTime": 8 * H, "fundingRate": "0.0001", "markPrice": "100"},
                    {"fundingTime": 16 * H, "fundingRate": "-0.00002", "markPrice": "101"},
                ],
            )
        if req.url.path == "/fapi/v1/klines":
            return httpx.Response(200, json=[[0, "1", "2", "0.5", "1.5", "10", H - 1]])
        return httpx.Response(404)

    a = BinanceAdapter(transport=httpx.MockTransport(handler))
    rates = a.fetch_funding_rates(PERP, 0, 20 * H)
    assert [r.rate for r in rates] == ["0.0001", "-0.00002"]
    assert a.fetch_bars(PERP, "1h", 0, H)[0].close == 1.5
    with pytest.raises(UnsupportedFeature):
        a.fetch_funding_rates("BTC/USDT", 0, H)


def test_funding_rates_cached_and_perp_cache_file_has_no_colon(tmp_path):
    class A:
        calls = 0

        def fetch_funding_rates(self, symbol, start, end):
            A.calls += 1
            from quant_agent.exchanges.base import FundingRate

            return [FundingRate(symbol, t, "0.0001") for t in range((start // (8 * H) + 1) * 8 * H, end + 1, 8 * H)]

    svc = MarketDataService(adapters={"binance": A()}, cache_root=tmp_path)
    rows, src, err = svc.funding_rates(PERP, start=0, end=48 * H)
    assert src == "exchange" and len(rows) == 6 and not err
    svc.funding_rates(PERP, start=0, end=48 * H)
    assert A.calls == 1
    assert all(":" not in p.name for p in tmp_path.iterdir())
    BarCache(tmp_path).merge("binance", PERP, "1h", synthetic_bars(PERP, "1h", 5))
    assert any(e["symbol"] == PERP for e in BarCache(tmp_path).entries())
    assert inst_id_of(PERP) == "BTC-USDT-SWAP" and inst_id_of("BTC/USDT") == "BTC-USDT"


def test_synthetic_perp_tracks_spot():
    s, p = synthetic_bars("BTC/USDT", "1h", 200), synthetic_bars(PERP, "1h", 200)
    assert all(abs(a.close / b.close - 1) < 0.001 for a, b in zip(s, p, strict=True))
    assert dec(synthetic_funding(PERP, 0, 24 * H)[0][1]) != 0
