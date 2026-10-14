"""ccxt 行情适配器（可选）：装了 ccxt 就能从它支持的上百家交易所拉公共 K 线，只读、不下单。

交易所名写成 ``ccxt:<id>``，比如 ``ccxt:kraken``、``ccxt:gate``。没装 ccxt 时给出明确提示，
不影响内置的 Binance / OKX / Bybit。
"""

from __future__ import annotations

import time
from typing import Any

from ..data.quality import TIMEFRAME_MS
from .base import Bar, BaseAdapter, Instrument, UnsupportedFeature

PAGE = 500


def available() -> bool:
    try:
        import ccxt  # noqa: F401
    except ImportError:
        return False
    return True


class CcxtAdapter(BaseAdapter):
    supports_trading = False

    def __init__(self, exchange_id: str, *, client: Any = None) -> None:
        super().__init__()
        self.exchange_id = exchange_id
        self.name = f"ccxt:{exchange_id}"
        if client is None:
            try:
                import ccxt
            except ImportError as exc:
                raise UnsupportedFeature("没装 ccxt：在服务器上 pip install ccxt 后才能用 ccxt:<交易所> 行情") from exc
            cls = getattr(ccxt, exchange_id, None)
            if cls is None:
                raise UnsupportedFeature(f"ccxt 没有交易所 {exchange_id}")
            client = cls({"enableRateLimit": True, "timeout": 15000})
        self.client = client

    def ping(self) -> bool:
        try:
            self.client.fetch_time()
            return True
        except Exception:
            return False

    def get_instruments(self) -> list[Instrument]:
        out = []
        for m in (self.client.load_markets() or {}).values():
            if not m.get("spot") or m.get("active") is False:
                continue
            prec, lim = m.get("precision") or {}, m.get("limits") or {}
            out.append(
                Instrument(
                    exchange=self.name,
                    symbol=m["symbol"],
                    market_type="spot",
                    base=m["base"],
                    quote=m["quote"],
                    tick_size=str(prec.get("price") or "0"),
                    lot_size=str(prec.get("amount") or "0"),
                    min_notional=str((lim.get("cost") or {}).get("min") or "0"),
                )
            )
        return out

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        tfs = getattr(self.client, "timeframes", None) or {}
        if timeframe not in TIMEFRAME_MS or (tfs and timeframe not in tfs):
            raise UnsupportedFeature(f"{self.name} 不支持周期 {timeframe}")
        step = TIMEFRAME_MS[timeframe]
        now = int(time.time() * 1000)
        rows: dict[int, list[Any]] = {}
        since = start
        for _ in range(2000):  # 从旧往新翻页
            page = self.client.fetch_ohlcv(symbol, timeframe, since=since, limit=PAGE) or []
            fresh = [r for r in page if int(r[0]) not in rows]
            for r in page:
                rows[int(r[0])] = r
            if not fresh:
                break
            newest = max(int(r[0]) for r in page)
            if newest >= end or len(page) < PAGE:
                break
            since = newest + step
        bars = []
        for ts in sorted(rows):
            if ts < start or ts > end:
                continue
            r = rows[ts]
            bars.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=ts,
                    close_time=ts + step,
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5] or 0),
                    closed=ts + step <= now,
                    source=self.name,
                    raw_ref=f"{self.name}:ohlcv?symbol={symbol}&tf={timeframe}",
                )
            )
        return bars

    def fetch_ticker(self, symbol: str) -> dict:
        t = self.client.fetch_ticker(symbol)
        return {"price": t.get("last"), "bid": t.get("bid"), "ask": t.get("ask")}
