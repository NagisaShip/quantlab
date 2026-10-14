"""Bybit 公共行情：K 线翻页（新→旧整理成旧→新）、资金费率、行情快照、能力矩阵、WebSocket 解析与订阅。

不连交易所：REST 用 httpx.MockTransport，WebSocket 用本机假服务（test_stream.FakeServer）。
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest
from test_stream import FakeServer, _no_proxy, _wait  # noqa: F401  (_no_proxy 是 autouse fixture)

from quant_agent.api.schemas import DemoConnectIn, PaperStartIn
from quant_agent.data import stream as S
from quant_agent.data.service import MarketDataService
from quant_agent.exchanges import BinanceAdapter, BybitAdapter, OKXAdapter, public_adapter
from quant_agent.exchanges.base import UnsupportedFeature

H = 3_600_000


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def _q(req: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}


def _kline_handler(seen: list, total: int, page: int = 1000):
    """假交易所：共 total 根 1h K 线（开盘 0..total-1 小时），按 [start, end] 返回最新的 page 根，新→旧。"""

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.bybit.com" and "X-BAPI-SIGN" not in req.headers
        q = _q(req)
        seen.append(q)
        assert req.url.path == "/v5/market/kline" and q["interval"] == "60" and q["symbol"] == "BTCUSDT"
        start, end = int(q["start"]), int(q["end"])
        ts = [i * H for i in range(total) if start <= i * H <= end][-page:]
        rows = [[str(t), "1", "2", "0.5", str(1 + t / H), "10", "100"] for t in reversed(ts)]
        return ok({"category": q["category"], "list": rows})

    return handler


def test_fetch_bars_pages_backwards_and_returns_oldest_first():
    seen: list = []
    a = BybitAdapter(transport=httpx.MockTransport(_kline_handler(seen, 2500)))
    bars = a.fetch_bars("BTC/USDT", "1h", 0, 2499 * H)
    assert [b.open_time for b in bars] == [i * H for i in range(2500)]
    assert bars[0].close_time == H and bars[-1].close == 2500.0 and all(b.closed for b in bars)
    assert len(seen) == 3 and seen[0]["category"] == "spot"
    assert int(seen[1]["end"]) == 1500 * H - 1  # 下一页从上一页最老那根之前开始


def test_fetch_bars_perp_uses_linear_and_service_accepts_them():
    seen: list = []
    a = BybitAdapter(transport=httpx.MockTransport(_kline_handler(seen, 300)))
    svc = MarketDataService(adapters={"bybit": a})
    bars, source = svc.get_bars("BTC/USDT:PERP", "1h", exchange="bybit", start=0, end=300 * H, source="exchange")
    assert source == "exchange" and len(bars) == 300 and seen[0]["category"] == "linear"


def test_funding_history_and_ticker():
    def handler(req: httpx.Request) -> httpx.Response:
        q = _q(req)
        if req.url.path == "/v5/market/funding/history":
            assert q["category"] == "linear" and q["symbol"] == "ETHUSDT"
            start, end = int(q["startTime"]), int(q["endTime"])
            ts = [i * 8 * H for i in range(450) if start <= i * 8 * H <= end][-200:]
            return ok(
                {
                    "list": [
                        {"symbol": "ETHUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(t)}
                        for t in reversed(ts)
                    ]
                }
            )
        if req.url.path == "/v5/market/tickers":
            if q["category"] == "linear":
                return ok(
                    {
                        "list": [
                            {
                                "lastPrice": "3000",
                                "bid1Price": "2999.9",
                                "ask1Price": "3000.1",
                                "markPrice": "3000.05",
                                "fundingRate": "0.0001",
                                "nextFundingTime": "28800000",
                            }
                        ]
                    }
                )
            return ok({"list": [{"lastPrice": "3001", "bid1Price": "3000", "ask1Price": "3002"}]})
        return httpx.Response(404)

    a = BybitAdapter(transport=httpx.MockTransport(handler))
    rates = a.fetch_funding_rates("ETH/USDT:PERP", 0, 449 * 8 * H)
    assert [r.ts for r in rates] == [i * 8 * H for i in range(450)] and rates[0].rate == "0.0001"
    assert rates[0].source == "bybit:public"
    with pytest.raises(UnsupportedFeature):
        a.fetch_funding_rates("ETH/USDT", 0, H)
    t = a.fetch_ticker("ETH/USDT:PERP")
    assert t["mark"] == "3000.05" and t["next_funding_time"] == 28800000 and t["bid"] == "2999.9"
    assert a.fetch_ticker("ETH/USDT") == {"price": "3001", "bid": "3000", "ask": "3002"}


def test_error_capabilities_and_host_allowlist():
    a = BybitAdapter(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"retCode": 10001, "retMsg": "params error", "result": {}})
        )
    )
    with pytest.raises(UnsupportedFeature, match="10001"):
        a.fetch_bars("BTC/USDT", "1h", 0, H)
    with pytest.raises(UnsupportedFeature):
        a.fetch_bars("BTC/USDT", "30m", 0, H)
    with pytest.raises(UnsupportedFeature):
        a.fetch_balances()
    caps = {c.key: c.supported for c in a.capabilities()}
    assert caps["public_market_data"] and caps["perpetual_market_data"] and not caps["live_trading"]
    assert not caps["withdraw"]
    with pytest.raises(ValueError):
        BybitAdapter(base_url="https://evil.example.com")


def test_public_adapter_factory_and_schemas():
    assert isinstance(public_adapter("bybit"), BybitAdapter)
    assert isinstance(public_adapter("okx"), OKXAdapter)
    assert isinstance(public_adapter("binance"), BinanceAdapter)
    assert PaperStartIn(slug="s", exchange="bybit").exchange == "bybit"
    assert DemoConnectIn(exchange="bybit", api_key="k" * 8, api_secret="s" * 8).exchange == "bybit"


def test_parse_bybit_ws_messages():
    assert S.bybit_url("BTC/USDT") == "wss://stream.bybit.com/v5/public/spot"
    assert S.bybit_url("BTC/USDT:PERP") == "wss://stream.bybit.com/v5/public/linear"
    assert S.bybit_subscribe("ETH/USDT:PERP", "4h") == {"op": "subscribe", "args": ["kline.240.ETHUSDT"]}
    msg = {
        "topic": "kline.60.BTCUSDT",
        "type": "snapshot",
        "data": [
            {
                "start": 0,
                "end": H - 1,
                "interval": "60",
                "open": "1",
                "high": "2",
                "low": "0.5",
                "close": "1.5",
                "volume": "10",
                "confirm": True,
            },
            {
                "start": H,
                "end": 2 * H - 1,
                "interval": "60",
                "open": "1.5",
                "high": "2",
                "low": "1",
                "close": "1.7",
                "volume": "3",
                "confirm": False,
            },
        ],
    }
    bars = S.parse_bybit(msg, "BTC/USDT", "1h")
    assert [b.closed for b in bars] == [True, False] and bars[0].close_time == H and bars[1].close == 1.7
    assert bars[0].source == "bybit-ws"
    assert S.parse_bybit({"op": "pong", "success": True}, "BTC/USDT", "1h") == []


def _ws(t: int, close: float, confirm: bool) -> str:
    return json.dumps(
        {
            "topic": "kline.60.BTCUSDT",
            "type": "snapshot",
            "data": [
                {
                    "start": t,
                    "end": t + H - 1,
                    "interval": "60",
                    "open": "1",
                    "high": "2",
                    "low": "0.5",
                    "close": str(close),
                    "volume": "1",
                    "confirm": confirm,
                }
            ],
        }
    )


def test_bybit_stream_receives_bars():
    srv = FakeServer([[json.dumps({"success": True, "op": "subscribe"}), _ws(0, 101, True), _ws(H, 105, False)]])
    st = S.BarStream("bybit", "BTC/USDT", "1h", url=f"ws://127.0.0.1:{srv.port}")
    st.start()
    try:
        assert _wait(lambda: st.last_price() == 105)
        assert [b.open_time for b in st.closed_since(-1)] == [0]
        assert st.stats.connected
    finally:
        st.stop()
        srv.close()


def test_bybit_stream_subscribe_failure_is_reported():
    srv = FakeServer(
        [
            [json.dumps({"success": False, "ret_msg": "Invalid topic", "op": "subscribe"})],
            [json.dumps({"success": False, "ret_msg": "Invalid topic", "op": "subscribe"})],
        ]
    )
    st = S.BarStream("bybit", "BTC/USDT", "1h", url=f"ws://127.0.0.1:{srv.port}")
    st.start()
    try:
        assert _wait(lambda: "Invalid topic" in st.stats.error)
    finally:
        st.stop()
        srv.close()
