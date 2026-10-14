"""持仓管理：多周期、移动止损、ROI、策略回调、入场确认、订单类型、交易分解、交易所端保护止损。"""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx

from quant_agent.data.quality import TIMEFRAME_MS
from quant_agent.engine.live import LiveRunner
from quant_agent.exchanges.base import Bar
from quant_agent.live import demo_broker as DB
from quant_agent.live.oms import DemoOMS, OmsLimits
from quant_agent.sdk import load_source, run_backtest
from quant_agent.sdk.runtimes import RuntimeCost

H = TIMEFRAME_MS["1h"]
T0 = 1_700_006_400_000 - 1_700_006_400_000 % (4 * H)  # 4h 整点对齐


def series(closes, sym="BTC/USDT", start=T0, wick=0.002):
    """按收盘价造 K 线：开盘 = 上一根收盘，高低点在开收之外再留一点影线。"""
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            Bar(
                sym,
                "1h",
                start + i * H,
                start + (i + 1) * H - 1,
                o,
                max(o, c) * (1 + wick),
                min(o, c) * (1 - wick),
                c,
                1e9,
            )
        )
        prev = c
    return out


def bt(src, closes, **kw):
    sym = kw.pop("sym", "BTC/USDT")
    wick = kw.pop("wick", 0.002)
    return run_backtest(
        load_source(src, name="t"),
        {sym: series(closes, sym, wick=wick)},
        cost=RuntimeCost(taker_fee=0, slippage_bps=0),
        **kw,
    )


def reasons(res):
    return [t["exit_reason"] for t in res.round_trips]


# ---------------------------------------------------------------- 多周期
def test_resample_only_complete_higher_bars_no_lookahead():
    closes = [100 + i for i in range(30)]
    src = """
def initialize(ctx):
    g.seen = []

def handle_data(ctx):
    s = ctx.securities[0]
    rows = history(s, 3, ["t", "close_time", "close", "high", "low", "open"], unit="4h")
    g.seen.append([ctx.current_dt, rows["t"], rows["close_time"], rows["close"], rows["high"], rows["open"]])
"""
    res = bt(src, closes)
    bars = series(closes)
    assert res.globals["seen"], "策略应当被调用"
    for now, ts, cts, cl, hi, op in res.globals["seen"]:
        for t, ct, c, h, o in zip(ts, cts, cl, hi, op, strict=True):
            group = [b for b in bars if t <= b.open_time < t + 4 * H]
            assert len(group) == 4 and ct == t + 4 * H - 1
            assert ct <= now, "不能看到还没收盘的大周期 bar"
            assert c == group[-1].close and o == group[0].open and h == max(b.high for b in group)
    last_now, ts = res.globals["seen"][-1][0], res.globals["seen"][-1][1]
    assert ts and ts[-1] + 8 * H > last_now  # 最新那根确实是最近完整的一根


# ---------------------------------------------------------------- 移动止损 / ROI
BUY_THEN_HOLD = """
def initialize(ctx):
    g.n = 0
    {setup}

def handle_data(ctx):
    g.n += 1
    if g.n == 1:
        order_target_percent(ctx.securities[0], {pct}, reason="breakout")
"""


def test_trailing_stop_long_exits_on_pullback_from_high():
    closes = [100, 100, 105, 110, 115, 120, 118, 112, 108, 100, 95]
    res = bt(BUY_THEN_HOLD.format(setup="set_trailing_stop(None, 0.05)", pct=0.5), closes)
    assert reasons(res) == ["trailing_stop"]
    exit_px = float(res.fills[-1]["price"])
    assert 108 <= exit_px <= 116, exit_px  # 最高约 120.2，回撤 5% ≈ 114.2，按触发 bar 开盘或止损价成交


def test_trailing_stop_with_activation_waits_for_profit():
    closes = [100, 100, 99, 98, 97, 96, 95, 94]
    res = bt(BUY_THEN_HOLD.format(setup="set_trailing_stop(None, 0.01, activate=0.05)", pct=0.5), closes)
    assert res.round_trips == []  # 一直没赚到 5%，移动止损不启动


def test_trailing_stop_short_perp():
    closes = [100, 100, 95, 90, 85, 80, 82, 86, 90]
    res = bt(BUY_THEN_HOLD.format(setup="set_trailing_stop(None, 0.05)", pct=-0.5), closes, sym="BTC/USDT:PERP")
    assert reasons(res) == ["trailing_stop"]
    assert float(res.fills[-1]["price"]) < 90


def test_roi_table_takes_profit():
    closes = [100 + i for i in range(20)]
    res = bt(BUY_THEN_HOLD.format(setup="set_roi({0: 0.05})", pct=0.5), closes)
    assert reasons(res) == ["roi"]
    assert 104 <= float(res.fills[-1]["price"]) <= 107


# ---------------------------------------------------------------- 策略回调
def test_custom_stoploss_ratchets_and_exits():
    closes = [100, 100, 104, 108, 112, 110, 106, 100, 95]
    src = (
        BUY_THEN_HOLD.format(setup="g.stops = []", pct=0.5)
        + """
def custom_stoploss(ctx, trade):
    g.stops.append(trade.stop_price)
    return 0.03
"""
    )
    res = bt(src, closes)
    stops = [s for s in res.globals["stops"] if s]
    assert stops == sorted(stops), "止损价只会上移"
    assert reasons(res) == ["custom_stop"]


def test_custom_exit_and_entry_tag():
    closes = [100] * 3 + [101] * 10
    src = (
        BUY_THEN_HOLD.format(setup="", pct=0.5)
        + """
def custom_exit(ctx, trade):
    if trade.bars_held >= 3:
        return "held_long_enough"
"""
    )
    res = bt(src, closes)
    assert reasons(res) == ["held_long_enough"]
    assert res.round_trips[0]["entry_tag"] == "breakout"
    bd = res.metrics["breakdown"]
    assert bd["entry_tag"][0]["key"] == "breakout" and bd["entry_tag"][0]["trades"] == 1
    assert {r["key"] for r in bd["exit_reason"]} == {"held_long_enough"}
    assert set(bd) == {"entry_tag", "exit_reason", "security", "weekday", "hour"}


def test_adjust_position_adds_once_and_counts_entries():
    closes = [100, 100, 97, 96, 96, 110, 110]
    src = (
        BUY_THEN_HOLD.format(setup="g.added = False", pct=0.3)
        + """
def adjust_position(ctx, trade):
    if trade.profit_ratio < -0.02 and not g.added:
        g.added = True
        return 1000

def custom_exit(ctx, trade):
    if trade.profit_ratio > 0.05:
        return True
"""
    )
    res = bt(src, closes)
    assert len(res.round_trips) == 1
    assert res.round_trips[0]["entries"] == 2
    assert res.round_trips[0]["exit_reason"] == "custom_exit"


def test_confirm_entry_veto():
    src = (
        BUY_THEN_HOLD.format(setup="g.asked = []", pct=0.5)
        + """
def confirm_entry(ctx, security, side, amount, price, tag):
    g.asked.append([security, side, tag])
    return False
"""
    )
    res = bt(src, [100] * 6)
    assert res.fills == []
    assert res.globals["asked"] == [["BTC/USDT", "buy", "breakout"]]
    assert any(r["code"] == "ENTRY_VETOED" for r in res.rejected)


# ---------------------------------------------------------------- 订单类型
def _limit_src(**kw):
    args = ", ".join(f"{k}={v!r}" for k, v in kw.items())
    return f"""
def initialize(ctx):
    g.n = 0

def handle_data(ctx):
    g.n += 1
    if g.n == 1:
        order_value(ctx.securities[0], 1000, {args})
"""


def test_post_only_rejects_crossing_limit():
    res = bt(_limit_src(limit_price=105.0, time_in_force="post_only"), [100] * 5)
    assert res.fills == [] and any(r["code"] == "POST_ONLY_WOULD_TAKE" for r in res.rejected)


def test_post_only_rests_and_fills_as_maker():
    res = bt(_limit_src(limit_price=98.0, time_in_force="post_only"), [100, 100, 97, 97], wick=0)
    assert len(res.fills) == 1 and float(res.fills[0]["price"]) == 98.0


def test_ioc_cancels_when_not_marketable():
    res = bt(_limit_src(limit_price=90.0, time_in_force="IOC"), [100, 100, 85, 85])
    assert res.fills == []  # 下一根开盘 100 > 90，立即作废；之后跌到 85 也不再成交


def test_on_expire_market_and_chase():
    closes = [100, 101, 102, 103, 104, 105]
    res = bt(_limit_src(limit_price=90.0, expire_bars=1, on_expire="market"), closes, wick=0)
    assert len(res.fills) == 1
    flat = [100, 101, 101.1, 101.1, 101.1, 101.1]
    res = bt(_limit_src(limit_price=90.0, expire_bars=1, on_expire="chase"), flat)
    assert len(res.fills) == 1 and float(res.fills[0]["price"]) <= 101.0  # 按开盘价重挂后做挂单方成交
    res = bt(_limit_src(limit_price=90.0, expire_bars=1, on_expire="cancel"), flat)
    assert res.fills == []


def test_old_style_strategy_unaffected():
    """没写任何新回调、不设移动止损的策略：结果与新增功能无关（指纹见 golden 测试）。"""
    res = bt(BUY_THEN_HOLD.format(setup="", pct=0.5), [100 + i for i in range(10)])
    assert res.round_trips == [] and len(res.fills) == 1


# ---------------------------------------------------------------- 交易所端保护止损
class StopBroker(DB.DemoBroker):
    exchange, env = "binance", "demo"

    def __init__(self, price: float) -> None:
        self.price = Decimal(str(price))
        self.stops: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.orders: dict[str, DB.OrderReport] = {}

    def rules(self, symbol):
        return DB.Rules(
            step=Decimal("0.0001"), min_qty=Decimal("0.0001"), min_notional=Decimal("5"), tick=Decimal("0.01")
        )

    def balances(self):
        return {"USDT": Decimal("100000")}

    def place_market(self, symbol, side, qty, client_order_id):
        self.calls.append(("market", side, client_order_id))
        rep = DB.OrderReport(client_order_id, "filled", qty, self.price, Decimal(0), "USDT", "x", ts=1)
        self.orders[client_order_id] = rep
        return rep

    def query(self, symbol, client_order_id):
        return self.orders[client_order_id]

    def place_stop(self, symbol, side, qty, trigger, client_order_id):
        self.calls.append(("stop", side, client_order_id, trigger))
        self.stops[client_order_id] = {"qty": qty, "trigger": trigger, "status": "new"}
        return DB.OrderReport(client_order_id, "new")

    def cancel_stop(self, symbol, client_order_id):
        self.calls.append(("cancel", client_order_id))
        if self.stops.get(client_order_id, {}).get("status") == "new":
            self.stops[client_order_id]["status"] = "canceled"

    def query_stop(self, symbol, client_order_id):
        s = self.stops[client_order_id]
        if s["status"] == "filled":
            return DB.OrderReport(client_order_id, "filled", s["qty"], s["trigger"], Decimal("0.1"), "USDT", "e1", ts=9)
        return DB.OrderReport(client_order_id, s["status"])

    def trigger_all(self):
        for s in self.stops.values():
            if s["status"] == "new":
                s["status"] = "filled"


PROTECTED = """
def initialize(ctx):
    g.n = 0
    set_trailing_stop(None, 0.05)

def handle_data(ctx):
    g.n += 1
    if g.n == 1:
        order_target_percent(ctx.securities[0], 0.5, reason="entry")
    if g.n == 4:
        order_target_percent(ctx.securities[0], 0, reason="signal_exit")
"""


def _route(live, oms, out):
    """与 api/tasks._route 同样的循环：出 outbox → OMS → 回报 → 内核。"""
    protects, outbox, fills = out.get("protects") or [], out.get("outbox") or [], []
    for _ in range(3):
        if not outbox and not protects:
            break
        reps = oms.process(outbox, [], protects)
        protects = []
        if not reps:
            break
        res = live.apply_reports("p", reps)
        fills += res["fills"]
        outbox = res.get("outbox") or []
    return fills


def _start(closes, wick=0.002):
    bars = series(closes, wick=wick)
    live = LiveRunner()
    live.start(
        "p",
        load_source(PROTECTED, name="p"),
        {"BTC/USDT": bars[:3]},
        timeframe="1h",
        initial_cash=10_000,
        cost=RuntimeCost(taker_fee=0.001),
        started_at=0,
        fill_mode="external",
    )
    return live, bars[3:]


def test_protect_placed_after_entry_and_canceled_before_platform_exit():
    live, rest = _start([100] * 3 + [100, 101, 102, 103, 103])
    broker = StopBroker(100)
    oms = DemoOMS(broker, OmsLimits(max_order_notional=0), poll_seconds=0)
    _route(live, oms, live.step("p", {"BTC/USDT": rest[:1]}))
    rt = live._states["p"].runtime
    assert len(rt.protects) == 1
    prot = next(iter(rt.protects.values()))
    assert prot["side"] == "sell" and Decimal(prot["trigger"]) < 100
    assert [c[0] for c in broker.calls] == ["market", "stop"]
    # 行情上涨：移动止损上移超过 0.1%，先撤旧单再挂新单
    _route(live, oms, live.step("p", {"BTC/USDT": rest[1:2]}))
    kinds = [c[0] for c in broker.calls]
    assert kinds[2:] == ["cancel", "stop"]
    new = next(iter(rt.protects.values()))
    assert Decimal(new["trigger"]) > Decimal(prot["trigger"])
    # 策略自己平仓：先撤保护止损，再送卖单
    _route(live, oms, live.step("p", {"BTC/USDT": rest[2:3]}))
    _route(live, oms, live.step("p", {"BTC/USDT": rest[3:4]}))
    tail = [c[0] for c in broker.calls][-2:]
    assert tail == ["cancel", "market"] and rt.protects == {}
    assert any(o.get("kind") == "protect" and o["status"] == "canceled" for o in oms.state.orders)


def test_exchange_stop_fill_is_booked_even_after_restart():
    live, rest = _start([100] * 3 + [100, 100, 100, 100])
    broker = StopBroker(100)
    oms = DemoOMS(broker, OmsLimits(max_order_notional=0), poll_seconds=0)
    _route(live, oms, live.step("p", {"BTC/USDT": rest[:1]}))
    snap = live.snapshot("p")
    # 平台停机，期间交易所上的保护止损被触发；重启后按检查点恢复
    broker.trigger_all()
    live2 = LiveRunner()
    bars = series([100] * 3)
    live2.start(
        "p",
        load_source(PROTECTED, name="p"),
        {"BTC/USDT": bars},
        timeframe="1h",
        initial_cash=10_000,
        cost=RuntimeCost(taker_fee=0.001),
        started_at=0,
        fill_mode="external",
    )
    live2.restore("p", snap)
    rt = live2._states["p"].runtime
    protects = list(rt.protects.values())
    assert len(protects) == 1
    fills = _route(live2, oms, {"protects": protects})
    assert len(fills) == 1 and fills[0]["side"] == "sell"
    assert rt.ledger.holdings["BTC/USDT"].qty == 0 and rt.protects == {}
    trips = [t for t in rt.round_trips if t.exit_reason]
    assert trips[-1].exit_reason == "exchange_stop"
    # 同一张止损单重复回报不重复记账
    again = oms.check_protects(protects)
    assert live2.apply_reports("p", again)["fills"] == []


def test_platform_exit_skipped_when_exchange_stop_already_filled():
    live, rest = _start([100] * 3 + [100, 100, 100, 100, 100], wick=0)  # 价格不动：止损价不变，不会因改单先查到
    broker = StopBroker(100)
    oms = DemoOMS(broker, OmsLimits(max_order_notional=0), poll_seconds=0)
    _route(live, oms, live.step("p", {"BTC/USDT": rest[:1]}))
    broker.trigger_all()  # 交易所先触发了，平台还没来得及查
    for b in rest[1:3]:
        _route(live, oms, {**live.step("p", {"BTC/USDT": [b]}), "protects": []})
    _route(live, oms, live.step("p", {"BTC/USDT": rest[3:4]}))  # 策略自己要平仓：撤单前查到已成交
    rt = live._states["p"].runtime
    assert [c for c in broker.calls if c[0] == "market"][-1][1] == "buy"  # 没有再送卖单
    assert rt.ledger.holdings["BTC/USDT"].qty == 0
    assert [t.exit_reason for t in rt.round_trips if t.exit_reason][-1] == "exchange_stop"
    assert any("保护止损已先成交" in o["message"] for o in oms.state.orders)


# ---------------------------------------------------------------- 各交易所止损单请求
def test_binance_stop_requests():
    seen = []

    def handler(req):
        seen.append(req)
        q = parse_qs(urlparse(str(req.url)).query)
        if req.url.path == "/api/v3/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"},
                                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                            ],
                        }
                    ]
                },
            )
        if req.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(
                200,
                json={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "filters": [
                                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                            ],
                        }
                    ]
                },
            )
        if req.url.path == "/api/v3/order" and req.method == "POST":
            assert q["type"] == ["STOP_LOSS_LIMIT"] and q["stopPrice"] == ["95.12"]
            assert float(q["price"][0]) < 95.12
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "qp1",
                    "orderId": 1,
                    "status": "NEW",
                    "executedQty": "0",
                    "cummulativeQuoteQty": "0",
                },
            )
        if req.url.path == "/fapi/v1/algoOrder" and req.method == "POST":
            assert q["type"] == ["STOP_MARKET"] and q["reduceOnly"] == ["true"] and q["clientAlgoId"] == ["qp2"]
            assert q["triggerPrice"] == ["95.1"]
            return httpx.Response(200, json={"algoId": 5, "algoStatus": "NEW"})
        if req.url.path == "/fapi/v1/algoOrder" and req.method == "DELETE":
            return httpx.Response(400, json={"code": -2011, "msg": "Unknown order sent."})
        return httpx.Response(404, json={})

    b = DB.make_broker("binance", "demo", {"api_key": "k", "api_secret": "s"}, transport=httpx.MockTransport(handler))
    assert b.place_stop("BTC/USDT", "sell", Decimal("0.5"), Decimal("95.1234"), "qp1").status == "new"
    assert b.place_stop("BTC/USDT:PERP", "sell", Decimal("0.5"), Decimal("95.1234"), "qp2").exchange_order_id == "5"
    b.cancel_stop("BTC/USDT:PERP", "qp2")  # 已经不存在：不报错
    assert all("demo" in r.url.host for r in seen)


def test_okx_and_bybit_stop_requests():
    okx = []

    def okx_handler(req):
        okx.append(req)
        if req.url.path == "/api/v5/public/instruments":
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {"instId": "BTC-USDT", "lotSz": "0.0001", "minSz": "0.0001", "tickSz": "0.1", "ctVal": "1"}
                    ],
                },
            )
        if req.url.path == "/api/v5/trade/order-algo":
            body = json.loads(req.content)
            assert body["ordType"] == "conditional" and body["slOrdPx"] == "-1" and body["algoClOrdId"] == "qp1"
            assert body["slTriggerPx"] == "95.1"
            return httpx.Response(200, json={"code": "0", "data": [{"algoId": "77", "sCode": "0"}]})
        return httpx.Response(200, json={"code": "0", "data": []})

    b = DB.make_broker(
        "okx",
        "demo",
        {"api_key": "k", "api_secret": "s", "passphrase": "p"},
        transport=httpx.MockTransport(okx_handler),
    )
    b.place_stop("BTC/USDT", "sell", Decimal("0.5"), Decimal("95.1234"), "qp1")
    assert all(r.headers["x-simulated-trading"] == "1" for r in okx)

    bb = []

    def bybit_handler(req):
        bb.append(req)
        if req.url.path == "/v5/market/instruments-info":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "lotSizeFilter": {
                                    "basePrecision": "0.0001",
                                    "qtyStep": "0.001",
                                    "minOrderQty": "0.001",
                                },
                                "priceFilter": {"tickSize": "0.1"},
                            }
                        ]
                    },
                },
            )
        if req.url.path == "/v5/order/create":
            body = json.loads(req.content)
            assert body["orderType"] == "Market" and body["triggerDirection"] == 2
            assert body["triggerPrice"] == "95.1" and body["reduceOnly"] is True
            return httpx.Response(200, json={"retCode": 0, "result": {"orderId": "9", "orderLinkId": "qp3"}})
        if req.url.path == "/v5/order/cancel":
            return httpx.Response(200, json={"retCode": 110001, "retMsg": "order not exists"})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

    b = DB.make_broker(
        "bybit", "demo", {"api_key": "k", "api_secret": "s"}, transport=httpx.MockTransport(bybit_handler)
    )
    b.place_stop("BTC/USDT:PERP", "sell", Decimal("0.5"), Decimal("95.1234"), "qp3")
    b.cancel_stop("BTC/USDT:PERP", "qp3")
    assert all("demo" in r.url.host for r in bb)
