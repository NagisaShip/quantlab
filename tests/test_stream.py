"""实时行情网关：消息解析、已收盘判定、断线重连后 REST 补数（用本机假 WebSocket 服务测试）。"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from quant_agent.data import stream as S
from quant_agent.exchanges.base import Bar

H = 3_600_000


def _kline(t: int, close: float, closed: bool) -> str:
    return json.dumps(
        {
            "stream": "btcusdt@kline_1h",
            "data": {
                "e": "kline",
                "E": t,
                "s": "BTCUSDT",
                "k": {
                    "t": t,
                    "T": t + H - 1,
                    "i": "1h",
                    "o": "100",
                    "h": "110",
                    "l": "90",
                    "c": str(close),
                    "v": "5",
                    "x": closed,
                },
            },
        }
    )


def test_parse_binance_and_okx():
    b = S.parse_binance(json.loads(_kline(0, 105.5, True)), "BTC/USDT", "1h")
    assert b.closed and b.close == 105.5 and b.close_time == H - 1 and b.source == "binance-ws"
    assert S.parse_binance({"data": {"e": "trade"}}, "BTC/USDT", "1h") is None
    msg = {
        "arg": {"channel": "candle1H", "instId": "BTC-USDT"},
        "data": [
            [str(H), "1", "2", "0.5", "1.5", "10", "0", "0", "0"],
            [str(0), "1", "2", "0.5", "1.2", "10", "0", "0", "1"],
        ],
    }
    bars = S.parse_okx(msg, "BTC/USDT", "1h")
    assert [x.closed for x in bars] == [False, True] and bars[0].close_time == 2 * H
    assert S.parse_okx({"event": "subscribe"}, "BTC/USDT", "1h") == []
    assert S.okx_subscribe("ETH/USDT", "4h")["args"][0] == {"channel": "candle4H", "instId": "ETH-USDT"}


class FakeServer:
    """本机 WebSocket 服务：每个连接先发一批消息，然后按 drop 决定是否主动断开。"""

    def __init__(self, batches: list[list[str]]):
        self.batches = batches
        self.conns = 0
        self.port = 0
        self._ready = threading.Event()
        self._stop: asyncio.Future | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        from websockets.asyncio.server import serve

        async def handler(ws):
            i = self.conns
            self.conns += 1
            for m in self.batches[min(i, len(self.batches) - 1)]:
                await ws.send(m)
                await asyncio.sleep(0.02)
            if i < len(self.batches) - 1:
                await ws.close()  # 模拟断线
                return
            await asyncio.sleep(30)

        async def main():
            self._stop = asyncio.get_running_loop().create_future()
            async with serve(handler, "127.0.0.1", 0) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self._ready.set()
                await self._stop

        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(main())

    def close(self):
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set_result, None)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


def _wait(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_stream_closed_bars_reconnect_and_backfill():
    # 第一次连接：bar0 收盘、bar1 形成中；然后断线。第二次连接：直接推 bar3 形成中（bar1、bar2 靠补数）
    srv = FakeServer(
        [
            [_kline(0, 101, False), _kline(0, 102, True), _kline(H, 103, False)],
            [_kline(3 * H, 120, False)],
        ]
    )
    calls = []

    def backfill(sym, tf, start, end):
        calls.append(start)
        return [
            Bar(sym, tf, H, 2 * H - 1, 1, 1, 1, 104, 1, True),
            Bar(sym, tf, 2 * H, 3 * H - 1, 1, 1, 1, 110, 1, True),
        ]

    st = S.BarStream("binance", "BTC/USDT", "1h", backfill=backfill, url=f"ws://127.0.0.1:{srv.port}")
    st.start()
    try:
        assert _wait(lambda: st.stats.reconnects >= 1 and st.stats.backfilled == 2)
        assert [b.open_time for b in st.closed_since(-1)] == [0, H, 2 * H]
        assert calls == [H]  # 从最后一根已收盘 bar 的下一根开始补
        assert _wait(lambda: st.last_price() == 120)
        snap = st.snapshot()
        assert snap["forming"]["c"] == 120 and snap["backfilled"] == 2
        assert any("补齐" in e["message"] for e in snap["events"])
        assert st.wait_closed(2 * H, 0.1) == []
    finally:
        st.stop()
        srv.close()


def test_gateway_refcount():
    gw = S.MarketGateway()
    gw._streams[("binance", "BTC/USDT", "1h")] = st = S.BarStream("binance", "BTC/USDT", "1h")
    gw._refs[("binance", "BTC/USDT", "1h")] = 1
    assert gw.subscribe("binance", "BTC/USDT", "1h") is st
    gw.release("binance", "BTC/USDT", "1h")
    assert gw.get("binance", "BTC/USDT", "1h") is st
    gw.release("binance", "BTC/USDT", "1h")
    assert gw.get("binance", "BTC/USDT", "1h") is None
