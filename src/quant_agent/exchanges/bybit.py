"""Bybit 公共行情适配器（V5）。

只做**公共行情**：K 线（现货 category=spot、USDT 永续 category=linear）、行情快照、资金费率历史、交易规则。
不带任何密钥、不签名、没有下单接口——Bybit 的模拟盘下单在 live/demo_broker.py 里，
只连 api-demo / api-testnet 两个模拟域名。这里的 api.bybit.com 只用来读公开行情。

口径：
- 永续写成 `BTC/USDT:PERP` → linear / BTCUSDT；现货 `BTC/USDT` → spot / BTCUSDT。
- Bybit K 线只给开盘时间，收盘时间 = 开盘 + 周期（与 OKX 适配器一致）；日线按 UTC 0 点切分。
- 接口返回新→旧，这里统一整理成旧→新、去重。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..timeframes import TimeframeTable
from .base import (
    Balance,
    Bar,
    BaseAdapter,
    Capability,
    Fill,
    FundingRate,
    Instrument,
    UnsupportedFeature,
)

PUBLIC_BASE = "https://api.bybit.com"
# 只允许官方公共行情域名
ALLOWED_HOSTS = ("api.bybit.com", "api.bytick.com")

TIMEFRAME_STEP_MS = TimeframeTable(
    {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}, lambda ms: ms
)
INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
KLINE_LIMIT = 1000
FUNDING_LIMIT = 200


def category_of(symbol: str) -> str:
    from .contracts import contract_type_of

    kind = contract_type_of(symbol)
    if kind == "inverse":
        return "inverse"
    if kind in ("linear", "delivery"):
        return "linear"
    return "spot"


def symbol_of(symbol: str) -> str:
    """BTC/USDT → BTCUSDT；BTC/USDT:PERP → BTCUSDT。"""
    return symbol.split(":")[0].replace("/", "").upper()


class BybitAdapter(BaseAdapter):
    name = "bybit"
    supports_trading = False

    def __init__(
        self, *, simulated: bool = False, base_url: str = PUBLIC_BASE, transport: httpx.BaseTransport | None = None
    ) -> None:
        super().__init__(simulated=simulated)
        host = base_url.rstrip("/").split("://")[-1].split("/")[0]
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"Bybit 行情只允许官方域名：{'、'.join(ALLOWED_HOSTS)}；收到 {host}")
        self.base = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base, timeout=15.0, transport=transport)

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return _bybit_json(self._client.get(path, params=params))

    # ---------- 公共行情 ----------
    def ping(self) -> bool:
        return self._client.get("/v5/market/time").status_code == 200

    def get_instruments(self) -> list[Instrument]:
        out: list[Instrument] = []
        cursor = ""
        for _ in range(20):
            params: dict[str, Any] = {"category": "spot", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = self._get("/v5/market/instruments-info", params)
            for s in res.get("list") or []:
                if s.get("status") not in (None, "Trading"):
                    continue
                lot = s.get("lotSizeFilter") or {}
                out.append(
                    Instrument(
                        exchange=self.name,
                        symbol=f"{s['baseCoin']}/{s['quoteCoin']}",
                        market_type="spot",
                        base=s["baseCoin"],
                        quote=s["quoteCoin"],
                        tick_size=(s.get("priceFilter") or {}).get("tickSize", "0"),
                        lot_size=lot.get("basePrecision", "0"),
                        min_notional=lot.get("minOrderAmt", "0"),
                    )
                )
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return out

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        interval = INTERVAL.get(timeframe)
        if interval is None:
            raise UnsupportedFeature(f"Bybit 不支持周期 {timeframe}")
        step = TIMEFRAME_STEP_MS[timeframe]
        cat, raw = category_of(symbol), symbol_of(symbol)
        now = int(time.time() * 1000)
        rows: dict[int, list[str]] = {}
        cursor = end
        for _ in range(500):  # 按 1000 根一页从新往旧翻
            res = self._get(
                "/v5/market/kline",
                {
                    "category": cat,
                    "symbol": raw,
                    "interval": interval,
                    "start": start,
                    "end": cursor,
                    "limit": KLINE_LIMIT,
                },
            )
            page = res.get("list") or []
            if not page:
                break
            for r in page:
                rows[int(r[0])] = r
            oldest = min(int(r[0]) for r in page)
            if oldest <= start or len(page) < KLINE_LIMIT or oldest - 1 >= cursor:
                break
            cursor = oldest - 1
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
                    volume=float(r[5]),  # 现货与 USDT 永续都按标的币计
                    closed=ts + step <= now,  # 正在形成的那根标记为未收盘
                )
            )
        return bars

    def fetch_funding_rates(self, symbol: str, start: int, end: int) -> list[FundingRate]:
        """USDT 永续资金费率历史（/v5/market/funding/history，每页 200 条，新→旧翻页）。"""
        from .contracts import contract_type_of

        if contract_type_of(symbol) not in ("linear", "inverse"):
            raise UnsupportedFeature(f"{symbol} 没有资金费率（永续写成 :PERP 或 :INVERSE）")
        raw = symbol_of(symbol)
        got: dict[int, FundingRate] = {}
        cursor = end
        for _ in range(500):
            res = self._get(
                "/v5/market/funding/history",
                {"category": "linear", "symbol": raw, "startTime": start, "endTime": cursor, "limit": FUNDING_LIMIT},
            )
            page = res.get("list") or []
            if not page:
                break
            for r in page:
                ts = int(r["fundingRateTimestamp"])
                if start <= ts <= end:
                    got[ts] = FundingRate(
                        symbol=symbol,
                        ts=ts,
                        rate=str(r["fundingRate"]),
                        source="bybit:public",
                        instrument_id=f"bybit:{symbol}",
                        raw_ref=f"bybit:funding/history?symbol={raw}&endTime={cursor}",
                    )
            oldest = min(int(r["fundingRateTimestamp"]) for r in page)
            if oldest <= start or len(page) < FUNDING_LIMIT or oldest - 1 >= cursor:
                break
            cursor = oldest - 1
        return [got[t] for t in sorted(got)]

    def fetch_ticker(self, symbol: str) -> dict:
        res = self._get("/v5/market/tickers", {"category": category_of(symbol), "symbol": symbol_of(symbol)})
        rows = res.get("list") or []
        if not rows:
            raise UnsupportedFeature(f"Bybit 没有 {symbol} 的行情")
        d = rows[0]
        out = {"price": d.get("lastPrice"), "bid": d.get("bid1Price"), "ask": d.get("ask1Price")}
        if category_of(symbol) == "linear":
            out.update(
                mark=d.get("markPrice"),
                funding_rate=d.get("fundingRate"),
                next_funding_time=int(d["nextFundingTime"]) if d.get("nextFundingTime") else None,
            )
        return out

    def ws_url(self, symbol: str, channel: str = "kline.1") -> str:
        return f"wss://stream.bybit.com/v5/public/{category_of(symbol)}"

    def ws_subscribe(self, symbol: str, channel: str = "kline.1") -> dict:
        return {"op": "subscribe", "args": [f"{channel}.{symbol_of(symbol)}"]}

    # ---------- 账户：不提供 ----------
    def fetch_balances(self) -> list[Balance]:
        raise UnsupportedFeature("Bybit 适配器只提供公共行情；模拟盘账户请在「交易所模拟盘」里接入")

    def fetch_fills(self, symbol: str | None = None, start: int | None = None) -> list[Fill]:
        raise UnsupportedFeature("Bybit 适配器只提供公共行情；模拟盘账户请在「交易所模拟盘」里接入")

    def capabilities(self) -> list[Capability]:
        return [
            Capability("public_market_data", True, "V5 公共行情（现货 + USDT 永续），无需鉴权"),
            Capability("readonly_account", False, "暂未接入只读账户"),
            Capability("simulated_trading", True, "通过「交易所模拟盘」接入 Demo Trading（api-demo.bybit.com）"),
            Capability("live_trading", False, "平台不提供 Bybit 实盘下单"),
            Capability("market_order", True, "仅模拟盘：现货 marketUnit=baseCoin，永续单向持仓"),
            Capability("limit_order", False, "模拟盘只下市价单"),
            Capability("websocket_stream", True, "wss://stream.bybit.com/v5/public/spot|linear"),
            Capability("perpetual_market_data", True, "USDT 永续（linear）K 线与资金费率历史"),
            Capability("withdraw", False, "平台不提供提现能力"),
        ]


def _bybit_json(resp: httpx.Response) -> dict[str, Any]:
    """Bybit 用 retCode 表示业务结果，HTTP 200 也可能是失败。返回 result。"""
    if resp.status_code >= 400:
        raise UnsupportedFeature(f"Bybit 返回 HTTP {resp.status_code}：{resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise UnsupportedFeature(f"Bybit 返回了非 JSON 响应：{resp.text[:200]}") from exc
    if int(body.get("retCode") or 0) != 0:
        raise UnsupportedFeature(f"Bybit 返回错误：{body.get('retCode')} {body.get('retMsg')}")
    return body.get("result") or {}
