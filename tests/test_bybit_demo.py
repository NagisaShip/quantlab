"""Bybit 模拟交易（V5）下单客户端：签名、现货、USDT 永续、拒单、实盘域名拒绝。

不连交易所：HTTP 全部走 httpx.MockTransport。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from quant_agent.live import demo_accounts
from quant_agent.live import demo_broker as DB

PERP = "BTC/USDT:PERP"
CREDS = {"api_key": "bybit-key-123", "api_secret": "bybit-secret-456"}


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def err(code: int, msg: str) -> httpx.Response:
    return httpx.Response(200, json={"retCode": code, "retMsg": msg, "result": {}})


def _broker(handler, env: str = "demo") -> DB.BybitDemoBroker:
    b = DB.make_broker("bybit", env, CREDS, transport=httpx.MockTransport(handler))
    assert isinstance(b, DB.BybitDemoBroker)
    return b


def _payload(req: httpx.Request) -> str:
    return req.url.query.decode() if req.method == "GET" else req.content.decode()


def _verify(req: httpx.Request) -> None:
    h = req.headers
    want = hmac.new(
        CREDS["api_secret"].encode(),
        f"{h['X-BAPI-TIMESTAMP']}{CREDS['api_key']}{h['X-BAPI-RECV-WINDOW']}{_payload(req)}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert h["X-BAPI-API-KEY"] == CREDS["api_key"]
    assert h["X-BAPI-SIGN"] == want


def test_signing_headers_for_get_and_post():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        _verify(req)
        if req.url.path == "/v5/account/wallet-balance":
            return ok(
                {
                    "list": [
                        {
                            "accountType": "UNIFIED",
                            "coin": [
                                {"coin": "USDT", "walletBalance": "50000.5"},
                                {"coin": "BTC", "walletBalance": "1"},
                            ],
                        }
                    ]
                }
            )
        return ok({"orderId": "o1", "orderLinkId": "q1"})

    b = _broker(handler)
    assert b.balances() == {"USDT": Decimal("50000.5"), "BTC": Decimal(1)}
    assert parse_qs(seen[0].url.query.decode()) == {"accountType": ["UNIFIED"]}
    b.place_market("BTC/USDT", "buy", Decimal("0.01"), "q1")
    assert seen[1].method == "POST" and seen[1].headers["X-BAPI-RECV-WINDOW"] == "10000"
    assert {r.url.host for r in seen} == {"api-demo.bybit.com"}


def _spot_handler(orders: dict, bodies: list):
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        if p == "/v5/market/instruments-info":
            assert q == {"category": "spot", "symbol": "BTCUSDT"}
            return ok(
                {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "lotSizeFilter": {
                                "basePrecision": "0.000001",
                                "minOrderQty": "0.000048",
                                "minOrderAmt": "1",
                            },
                            "priceFilter": {"tickSize": "0.01"},
                        }
                    ]
                }
            )
        _verify(req)
        if p == "/v5/order/create":
            body = json.loads(req.content)
            bodies.append(body)
            return ok({"orderId": f"oid-{body['orderLinkId']}", "orderLinkId": body["orderLinkId"]})
        if p == "/v5/order/realtime":
            o = orders.get(q["orderLinkId"])
            return ok({"list": [o] if o else []})
        if p == "/v5/order/history":
            return ok({"list": []})
        if p == "/v5/execution/list":
            if q["orderId"] == "oid-b1":
                return ok(
                    {
                        "list": [
                            {"execFee": "0.00001", "feeCurrency": "BTC"},
                            {"execFee": "0.00002", "feeCurrency": "BTC"},
                        ]
                    }
                )
            return ok({"list": [{"execFee": "1.2", "feeCurrency": "USDT"}]})
        return httpx.Response(404, json={})

    return handler


def test_spot_buy_and_sell_reports_with_fees():
    bodies: list = []
    orders = {
        "b1": {
            "orderId": "oid-b1",
            "orderLinkId": "b1",
            "side": "Buy",
            "orderStatus": "Filled",
            "cumExecQty": "0.03",
            "avgPrice": "60000",
            "cumExecFee": "0.00003",
            "updatedTime": "1700000000000",
        },
        "s1": {
            "orderId": "oid-s1",
            "orderLinkId": "s1",
            "side": "Sell",
            "orderStatus": "PartiallyFilledCanceled",
            "cumExecQty": "0.02",
            "avgPrice": "",
            "cumExecValue": "1201",
            "updatedTime": "1700000001000",
        },
    }
    b = _broker(_spot_handler(orders, bodies))
    r = b.rules("BTC/USDT")
    assert (r.step, r.min_qty, r.min_notional) == (Decimal("0.000001"), Decimal("0.000048"), Decimal(1))

    rep = b.place_market("BTC/USDT", "buy", Decimal("0.0300"), "b1")
    assert rep.status == "new" and rep.exchange_order_id == "oid-b1"
    assert bodies[0] == {
        "category": "spot",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.03",
        "orderLinkId": "b1",
        "marketUnit": "baseCoin",
    }
    rep = b.query("BTC/USDT", "b1")
    assert (rep.status, rep.filled_qty, rep.avg_price) == ("filled", Decimal("0.03"), Decimal(60000))
    assert (rep.fee, rep.fee_currency, rep.ts) == (Decimal("0.00003"), "BTC", 1700000000000)

    b.place_market("BTC/USDT", "sell", Decimal("0.02"), "s1")
    assert bodies[1]["side"] == "Sell"
    rep = b.query("BTC/USDT", "s1")
    assert (rep.status, rep.filled_qty, rep.avg_price) == ("canceled", Decimal("0.02"), Decimal("60050"))
    assert (rep.fee, rep.fee_currency) == (Decimal("1.2"), "USDT")


def test_query_falls_back_to_history_and_unknown_is_not_sent():
    def handler(req: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        if req.url.path == "/v5/order/realtime":
            return ok({"list": []})
        if req.url.path == "/v5/order/history":
            if q["orderLinkId"] == "old":
                return ok(
                    {
                        "list": [
                            {
                                "orderId": "9",
                                "side": "Buy",
                                "orderStatus": "Filled",
                                "cumExecQty": "1",
                                "avgPrice": "10",
                                "cumExecFee": "0.001",
                            }
                        ]
                    }
                )
            return ok({"list": []})
        if req.url.path == "/v5/execution/list":
            return err(10001, "params error")
        return httpx.Response(404, json={})

    b = _broker(handler)
    rep = b.query("ETH/USDT", "old")
    # 成交明细取不到时用订单上的累计手续费，币种按现货买入=标的币推断
    assert (rep.status, rep.fee, rep.fee_currency) == ("filled", Decimal("0.001"), "ETH")
    missing = b.query("ETH/USDT", "nope")
    assert missing.status == "rejected" and "未送达" in missing.message


def test_rejected_order():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v5/order/create":
            return err(170131, "Insufficient balance.")
        return ok(
            {
                "list": [
                    {
                        "orderId": "7",
                        "side": "Sell",
                        "orderStatus": "Rejected",
                        "cumExecQty": "0",
                        "avgPrice": "0",
                        "rejectReason": "EC_NoEnoughQtyToFill",
                    }
                ]
            }
        )

    b = _broker(handler)
    with pytest.raises(DB.BrokerError, match=r"170131.*Insufficient balance"):
        b.place_market("BTC/USDT", "buy", Decimal(1), "r1")
    rep = b.query("BTC/USDT", "r2")
    assert rep.status == "rejected" and rep.filled_qty == 0 and rep.message == "EC_NoEnoughQtyToFill"


def test_perp_prepare_order_and_positions():
    calls: list[tuple[str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        if req.method == "POST":
            _verify(req)
            body = json.loads(req.content)
            calls.append((p, body))
            if p == "/v5/position/switch-mode":
                return err(110025, "Position mode is not modified")
            if p == "/v5/position/switch-isolated":
                return err(100028, "The API cannot be executed under unified account.")
            if p == "/v5/position/set-leverage":
                return err(110043, "leverage not modified")
            if p == "/v5/order/create":
                return ok({"orderId": "p1", "orderLinkId": body["orderLinkId"]})
        if p == "/v5/market/instruments-info":
            assert q["category"] == "linear"
            return ok(
                {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
                        }
                    ]
                }
            )
        if p == "/v5/order/realtime":
            assert q == {"category": "linear", "symbol": "BTCUSDT", "orderLinkId": "q1"}
            return ok(
                {
                    "list": [
                        {
                            "orderId": "p1",
                            "side": "Sell",
                            "orderStatus": "Filled",
                            "cumExecQty": "0.01",
                            "avgPrice": "60000",
                            "cumExecFee": "0.33",
                            "updatedTime": "5",
                        }
                    ]
                }
            )
        if p == "/v5/execution/list":
            return ok({"list": [{"execFee": "0.33"}]})  # 线性合约可能不带 feeCurrency
        if p == "/v5/position/list":
            assert q["category"] == "linear" and q["settleCoin"] == "USDT"
            if "cursor" not in q:
                return ok(
                    {
                        "list": [
                            {"symbol": "BTCUSDT", "side": "Sell", "size": "0.01"},
                            {"symbol": "XRPUSDT", "side": "", "size": "0"},
                        ],
                        "nextPageCursor": "c2",
                    }
                )
            return ok({"list": [{"symbol": "ETHUSDT", "side": "Buy", "size": "0.5"}], "nextPageCursor": ""})
        return httpx.Response(404, json={})

    b = _broker(handler)
    assert b.rules(PERP) == DB.Rules(Decimal("0.001"), Decimal("0.001"), Decimal(5))
    b.prepare_perp(PERP, Decimal(3))
    assert [c[0] for c in calls] == [
        "/v5/position/switch-mode",
        "/v5/position/switch-isolated",
        "/v5/position/set-leverage",
    ]
    assert calls[1][1] == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "tradeMode": 1,
        "buyLeverage": "3",
        "sellLeverage": "3",
    }
    assert calls[2][1]["buyLeverage"] == calls[2][1]["sellLeverage"] == "3"

    b.place_market(PERP, "sell", Decimal("0.010"), "q1")
    order = calls[-1][1]
    assert order == {
        "category": "linear",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "orderType": "Market",
        "qty": "0.01",
        "orderLinkId": "q1",
        "positionIdx": 0,
    }
    rep = b.query(PERP, "q1")
    assert (rep.status, rep.filled_qty, rep.avg_price, rep.fee, rep.fee_currency) == (
        "filled",
        Decimal("0.01"),
        Decimal(60000),
        Decimal("0.33"),
        "USDT",
    )
    assert b.positions() == {PERP: Decimal("-0.01"), "ETH/USDT:PERP": Decimal("0.5")}


def test_prepare_perp_raises_on_real_leverage_error():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v5/position/set-leverage":
            return err(110013, "Cannot set leverage due to risk limit level.")
        return ok({})

    with pytest.raises(DB.BrokerError, match="110013"):
        _broker(handler).prepare_perp(PERP, Decimal(50))


def test_long_client_order_id_maps_deterministically():
    platform_id = "q" + "a" * 31
    assert DB.BybitDemoBroker.link_id(platform_id) == platform_id
    long_id = "x" * 50
    a, b = DB.BybitDemoBroker.link_id(long_id), DB.BybitDemoBroker.link_id(long_id)
    assert a == b and len(a) <= 36 and a != long_id
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            seen.append(json.loads(req.content)["orderLinkId"])
            return ok({"orderId": "1"})
        seen.append(parse_qs(req.url.query.decode())["orderLinkId"][0])
        return ok({"list": [{"orderId": "1", "side": "Buy", "orderStatus": "New", "cumExecQty": "0"}]})

    br = _broker(handler)
    br.place_market("BTC/USDT", "buy", Decimal(1), long_id)
    rep = br.query("BTC/USDT", long_id)
    assert seen == [a, a] and rep.client_order_id == long_id and rep.status == "new"


def test_mainnet_host_refused(monkeypatch):
    with pytest.raises(ValueError):
        DB.make_broker("bybit", "live", CREDS)
    with pytest.raises(ValueError):
        DB.BybitDemoBroker("k", "s", env="mainnet")
    assert not any(
        "api.bybit.com" in m["base"] or "bytick" in m["base"] for (ex, _), m in DB.ENVIRONMENTS.items() if ex == "bybit"
    )
    # 即使有人把注册表改成实盘域名，客户端也拒绝构造
    monkeypatch.setitem(
        DB.ENVIRONMENTS, ("bybit", "demo"), {**DB.ENVIRONMENTS[("bybit", "demo")], "base": "https://api.bybit.com"}
    )
    with pytest.raises(ValueError, match=r"api\.bybit\.com"):
        DB.make_broker("bybit", "demo", CREDS)


def test_registry_lists_bybit_without_passphrase():
    envs = {(e["exchange"], e["env"]): e for e in demo_accounts.DemoConnectionService.environments()}
    assert envs[("bybit", "demo")]["host"] == "https://api-demo.bybit.com"
    assert envs[("bybit", "demo")]["realistic"] is True and "模拟交易" in envs[("bybit", "demo")]["key_help"]
    assert envs[("bybit", "testnet")]["realistic"] is False


def test_testnet_uses_testnet_host():
    hosts = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        return ok({"list": []})

    assert _broker(handler, env="testnet").balances() == {}
    assert hosts == ["api-testnet.bybit.com"]
