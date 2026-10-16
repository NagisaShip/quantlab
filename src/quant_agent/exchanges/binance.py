"""Binance 适配器。

**行情域名与交易域名必须分开**——这是踩过坑的地方：

- 行情永远走公共数据域名 `data-api.binance.vision`（无需鉴权）。
  它是完整历史，BTCUSDT 1h 能拉到几百上千根。
- 测试环境 `testnet.binance.vision` 只用于订单/账户联调。
  它的历史 K 线非常浅（实测 1h 只有百余根），
  拿它当行情源会得到一段只有几天的"回测"，却让人以为跑了一个月。

所以 `simulated` 只影响交易与账户接口，不影响行情。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import (
    Balance,
    Bar,
    BaseAdapter,
    Capability,
    Fill,
    FundingRate,
    Instrument,
    OrderIntent,
    UnsupportedFeature,
)
from .signing import decimal_str, hmac_sha256_hex, signed_query

PUBLIC_BASE = "https://data-api.binance.vision"
# U 本位永续的公共行情（K 线、资金费率、标记价）。data-api.binance.vision 只有现货。
FUTURES_BASE = "https://fapi.binance.com"
TESTNET_BASE = "https://testnet.binance.vision"

_INTERVAL_MAP = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
#: Binance K 线接口原生支持的周期（1s 只有现货有）
NATIVE_INTERVALS = {
    "1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w",
}
#: 逐笔成交聚合最多扫多少页（每页 1000 笔）：防止一次请求把 100ms 线拉成几十万次调用
MAX_TRADE_PAGES = 200


class BinanceAdapter(BaseAdapter):
    name = "binance"

    def __init__(
        self,
        *,
        simulated: bool = False,
        api_key: str | None = None,
        api_secret: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(simulated=simulated)
        self.api_key = api_key
        self.api_secret = api_secret
        # 行情客户端：始终指向公共数据域名
        # 行情请求会被多个后台任务并发调用：连接池放大、等待连接的超时单独放宽，避免并发时报 PoolTimeout
        timeout = httpx.Timeout(15.0, connect=8.0, pool=30.0)
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=16)
        self._client = httpx.Client(base_url=PUBLIC_BASE, timeout=timeout, limits=limits, transport=transport)
        self._fclient = httpx.Client(base_url=FUTURES_BASE, timeout=timeout, limits=limits, transport=transport)
        # 交易客户端：模拟盘走 testnet，仅用于账户与订单接口
        self.trade_base = TESTNET_BASE if simulated else PUBLIC_BASE
        self._trade_client = httpx.Client(base_url=self.trade_base, timeout=15.0, transport=transport)

    @property
    def base(self) -> str:
        """行情域名。保留这个属性名，便于测试与展示。"""
        return PUBLIC_BASE

    # ---------- 公共行情 ----------
    def ping(self) -> bool:
        return self._client.get("/api/v3/ping").status_code == 200

    def server_time(self) -> int:
        return self._client.get("/api/v3/time").json()["serverTime"]

    def get_instruments(self) -> list[Instrument]:
        data = self._client.get("/api/v3/exchangeInfo").json()
        out: list[Instrument] = []
        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            filters = {f["filterType"]: f for f in s.get("filters", [])}
            tick = filters.get("PRICE_FILTER", {}).get("tickSize", "0")
            lot = filters.get("LOT_SIZE", {}).get("stepSize", "0")
            min_notional = filters.get("NOTIONAL", {}).get("minNotional", "0")
            out.append(
                Instrument(
                    exchange=self.name,
                    symbol=f"{s['baseAsset']}/{s['quoteAsset']}",
                    market_type="spot",
                    base=s["baseAsset"],
                    quote=s["quoteAsset"],
                    tick_size=tick,
                    lot_size=lot,
                    min_notional=min_notional,
                )
            )
        return out

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """K 线。非原生周期在本地聚合：整秒周期用 1s K 线合成，亚秒周期用逐笔成交（aggTrades）合成。"""
        from ..timeframes import parse_timeframe_ms

        perp = symbol.upper().endswith(":PERP")
        step = parse_timeframe_ms(timeframe)
        if step is None:
            raise UnsupportedFeature(f"不支持的周期 {timeframe}")
        if timeframe not in NATIVE_INTERVALS or (perp and timeframe == "1s"):
            if perp:
                raise UnsupportedFeature(f"永续合约没有 {timeframe} 数据：秒级 / 毫秒级只支持现货")
            if step % 1000 == 0:
                base = self._klines(symbol, "1s", start, end)
                return _resample(base, symbol, timeframe, step, start, end)
            return self._trade_bars(symbol, timeframe, step, start, end)
        return self._klines(symbol, timeframe, start, end)

    def _trade_bars(self, symbol: str, timeframe: str, step: int, start: int, end: int) -> list[Bar]:
        """逐笔成交聚合成亚秒级 K 线；没有成交的区间用上一根收盘价补一根 0 成交量的 bar，时间轴保持连续。"""
        raw_symbol = symbol.split(":")[0].replace("/", "")
        start = start - start % step
        trades: list[tuple[int, float, float]] = []
        cursor = start
        for _ in range(MAX_TRADE_PAGES):
            if cursor >= end:
                break
            # aggTrades 一次的时间窗不能超过 1 小时
            resp = self._client.get(
                "/api/v3/aggTrades",
                params={"symbol": raw_symbol, "startTime": cursor, "endTime": min(end, cursor + 3_600_000) - 1, "limit": 1000},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                cursor = min(end, cursor + 3_600_000)
                continue
            trades.extend((int(r["T"]), float(r["p"]), float(r["q"])) for r in rows)
            last = int(rows[-1]["T"])
            cursor = last + 1 if len(rows) >= 1000 else min(end, cursor + 3_600_000)
        bars: list[Bar] = []
        i = 0
        prev_close: float | None = None
        t = start
        while t + step <= end + step and t < end:
            o = h = lo = c = None
            vol = 0.0
            while i < len(trades) and trades[i][0] < t + step:
                _, px, qty = trades[i]
                if o is None:
                    o = h = lo = px
                h, lo, c = max(h, px), min(lo, px), px  # type: ignore[type-var]
                vol += qty
                i += 1
            if o is None:
                if prev_close is None:
                    t += step
                    continue
                o = h = lo = c = prev_close
            prev_close = c
            close_time = t + step - 1
            bars.append(
                Bar(symbol=symbol, timeframe=timeframe, open_time=t, close_time=close_time, open=o, high=h, low=lo,  # type: ignore[arg-type]
                    close=c, volume=vol, closed=close_time < end)  # type: ignore[arg-type]
            )
            t += step
        return bars

    def _klines(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        perp = symbol.upper().endswith(":PERP")
        raw_symbol = symbol.split(":")[0].replace("/", "")
        client, path = (self._fclient, "/fapi/v1/klines") if perp else (self._client, "/api/v3/klines")
        limit = 1000 if not perp else 1500
        bars: list[Bar] = []
        cursor = start
        while cursor < end:
            resp = client.get(
                path,
                params={"symbol": raw_symbol, "interval": timeframe, "startTime": cursor, "limit": limit},
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break
            for r in rows:
                close_time = r[6]
                bars.append(
                    Bar(
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=r[0],
                        close_time=close_time,
                        open=float(r[1]),
                        high=float(r[2]),
                        low=float(r[3]),
                        close=float(r[4]),
                        volume=float(r[5]),
                        closed=close_time < end,  # 未完成 K 线标记
                    )
                )
            last_close = rows[-1][6]
            # 已经追上区间末端，或服务端返回不足一页（说明到数据尽头）才停止
            if last_close >= end:
                break
            if last_close + 1 <= cursor:
                break
            cursor = last_close + 1
        return bars

    def fetch_funding_rates(self, symbol: str, start: int, end: int) -> list[FundingRate]:
        """U 本位永续的历史资金费率（公共接口 /fapi/v1/fundingRate，每页最多 1000 条）。"""
        if not symbol.upper().endswith(":PERP"):
            raise UnsupportedFeature(f"{symbol} 是现货，没有资金费率（永续写成 BTC/USDT:PERP）")
        raw = symbol.split(":")[0].replace("/", "")
        out: list[FundingRate] = []
        cursor = start
        while cursor < end:
            resp = self._fclient.get(
                "/fapi/v1/fundingRate", params={"symbol": raw, "startTime": cursor, "endTime": end, "limit": 1000}
            )
            rows = _json_or_error(resp)
            if not rows:
                break
            for r in rows:
                out.append(
                    FundingRate(
                        symbol=symbol,
                        ts=int(r["fundingTime"]),
                        rate=str(r["fundingRate"]),
                        mark_price=str(r.get("markPrice") or "0"),
                        source="binance:fapi",
                        instrument_id=f"binance:{symbol}",
                        raw_ref=f"binance:fundingRate?symbol={raw}&startTime={cursor}",
                    )
                )
            last = int(rows[-1]["fundingTime"])
            if len(rows) < 1000 or last + 1 <= cursor:
                break
            cursor = last + 1
        return out

    def fetch_ticker(self, symbol: str) -> dict:
        if symbol.upper().endswith(":PERP"):
            raw = symbol.split(":")[0].replace("/", "")
            book = _json_or_error(self._fclient.get("/fapi/v1/ticker/bookTicker", params={"symbol": raw}))
            prem = _json_or_error(self._fclient.get("/fapi/v1/premiumIndex", params={"symbol": raw}))
            return {
                "price": prem.get("markPrice"),
                "bid": book["bidPrice"],
                "ask": book["askPrice"],
                "mark": prem.get("markPrice"),
                "funding_rate": prem.get("lastFundingRate"),
                "next_funding_time": prem.get("nextFundingTime"),
            }
        raw_symbol = symbol.replace("/", "")
        price = self._client.get("/api/v3/ticker/price", params={"symbol": raw_symbol}).json()
        book = self._client.get("/api/v3/ticker/bookTicker", params={"symbol": raw_symbol}).json()
        return {"price": price["price"], "bid": book["bidPrice"], "ask": book["askPrice"]}

    def fetch_order_book(self, symbol: str, depth: int = 20) -> dict:
        """公开盘口快照（执行研究的盘口录制用）。"""
        limit = min((x for x in (5, 10, 20, 50, 100) if x >= depth), default=100)
        d = self._client.get("/api/v3/depth", params={"symbol": symbol.replace("/", ""), "limit": limit}).json()
        return {
            "ts": int(time.time() * 1000),
            "bids": [[float(p), float(q)] for p, q in d["bids"][:depth]],
            "asks": [[float(p), float(q)] for p, q in d["asks"][:depth]],
        }

    def fetch_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """最近的逐笔成交（归集）。side 为主动方向：买方是挂单方 → 主动卖。"""
        rows = self._client.get("/api/v3/aggTrades", params={"symbol": symbol.replace("/", ""), "limit": limit}).json()
        return [
            {
                "id": r["a"],
                "t": int(r["T"]),
                "p": float(r["p"]),
                "q": float(r["q"]),
                "side": "sell" if r.get("m") else "buy",
            }
            for r in rows
        ]

    def ws_url(self, symbol: str, channel: str = "kline_1m") -> str:
        if symbol.upper().endswith(":PERP"):
            return f"wss://fstream.binance.com/ws/{symbol.split(':')[0].replace('/', '').lower()}@{channel}"
        stream = f"{symbol.replace('/', '').lower()}@{channel}"
        return f"wss://data-stream.binance.vision:443/ws/{stream}"

    # ---------- 账户（只读 / 模拟）----------
    def _signed_headers(self) -> dict[str, str]:
        if not self.api_key:
            raise UnsupportedFeature("需要 API Key 才能访问账户接口")
        return {"X-MBX-APIKEY": self.api_key}

    def fetch_balances(self) -> list[Balance]:
        if not self.simulated:
            raise UnsupportedFeature("公共行情域名不提供账户数据，请使用模拟盘或只读账户 key")
        resp = self._trade_client.get("/api/v3/account", headers=self._signed_headers())
        resp.raise_for_status()
        return [Balance(asset=b["asset"], free=b["free"], locked=b["locked"]) for b in resp.json().get("balances", [])]

    def fetch_fills(self, symbol: str | None = None, start: int | None = None) -> list[Fill]:
        """从模拟盘/只读账户拉成交记录，用于实盘对照。"""
        if not self.api_key:
            raise UnsupportedFeature("需要 API Key 才能读取成交记录")
        params: dict[str, object] = {}
        if symbol:
            params["symbol"] = symbol.replace("/", "")
        if start:
            params["startTime"] = int(start)
        resp = self._trade_client.get("/api/v3/myTrades", params=params, headers=self._signed_headers())
        resp.raise_for_status()
        out: list[Fill] = []
        for row in resp.json():
            out.append(
                Fill(
                    symbol=symbol or row.get("symbol", ""),
                    side="buy" if row.get("isBuyer") else "sell",
                    price=row["price"],
                    qty=row["qty"],
                    fee=row.get("commission", "0"),
                    fee_asset=row.get("commissionAsset", "USDT"),
                    fill_id=str(row.get("id", "")),
                    ts=int(row.get("time", 0)),
                )
            )
        return out

    # ---------- 交易（模拟盘 / 实盘，按 simulated 切换域名）----------
    supports_trading = True

    def place_order(self, intent: OrderIntent) -> dict[str, Any]:
        """下单。只允许市价单与限价单，且必须带 client_order_id 便于对账。

        校验顺序刻意是"编程错误 → 能力错误 → 业务错误"：
        缺 client_order_id 是调用方写错了，先报这个，别让缺密钥的消息把真正的问题盖住。

        红线：平台不提供提现能力，这里也没有任何提现接口。
        """
        if not intent.client_order_id:
            raise ValueError("下单必须携带 client_order_id：没有它就无法与实盘成交逐笔对账")
        if intent.order_type not in ("market", "limit"):
            raise UnsupportedFeature(f"Binance 适配器暂不支持 {intent.order_type} 订单")
        if not self.api_key or not self.api_secret:
            raise UnsupportedFeature("需要 API Key 与 Secret 才能下单")

        self.cache_instruments()
        qty = self.round_qty(intent.symbol, str(intent.qty))
        if qty <= 0:
            raise ValueError(f"{intent.symbol} 的数量 {intent.qty} 低于最小步长，无法下单")

        params: dict[str, object] = {
            "symbol": intent.symbol.replace("/", ""),
            "side": intent.side.upper(),
            "type": intent.order_type.upper(),
            "quantity": decimal_str(qty),
            "newClientOrderId": intent.client_order_id,
            "timestamp": int(time.time() * 1000),
        }
        price = intent.meta.get("price")
        if intent.order_type == "limit":
            if price is None:
                raise ValueError("限价单必须提供 price")
            params["price"] = decimal_str(self.round_price(intent.symbol, str(price)))
            params["timeInForce"] = intent.meta.get("time_in_force", "GTC")

        resp = self._trade_client.post(
            "/api/v3/order",
            content=signed_query(params, self.api_secret),
            headers={**self._signed_headers(), "Content-Type": "application/x-www-form-urlencoded"},
        )
        body = _json_or_error(resp)
        return {
            "ok": True,
            "exchange": self.name,
            "mode": "simulated" if self.simulated else "live",
            "order_id": str(body.get("orderId", "")),
            "client_order_id": body.get("clientOrderId", intent.client_order_id),
            "status": body.get("status", ""),
            "raw": body,
        }

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise UnsupportedFeature("需要 API Key 与 Secret 才能撤单")
        params: dict[str, object] = {
            "symbol": symbol.replace("/", ""),
            "orderId": order_id,
            "timestamp": int(time.time() * 1000),
        }
        resp = self._trade_client.delete(
            "/api/v3/order",
            params=params,
            headers=self._signed_headers(),
        )
        # DELETE 也要签名，httpx 的 params 不参与签名，所以手工拼
        if resp.status_code == 400:
            params["signature"] = hmac_sha256_hex(self.api_secret, urlencode(sorted(params.items())))
            resp = self._trade_client.delete("/api/v3/order", params=params, headers=self._signed_headers())
        body = _json_or_error(resp)
        return {
            "ok": True,
            "exchange": self.name,
            "order_id": str(body.get("orderId", order_id)),
            "status": body.get("status", "CANCELED"),
            "raw": body,
        }

    def query_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        """查单。状态未知时**必须**先查清楚再决定是否重发，不能因超时就新建订单。"""
        if not self.api_key or not self.api_secret:
            raise UnsupportedFeature("需要 API Key 与 Secret 才能查单")
        params: dict[str, object] = {
            "symbol": symbol.replace("/", ""),
            "orderId": order_id,
            "timestamp": int(time.time() * 1000),
        }
        resp = self._trade_client.get(
            "/api/v3/order",
            params={**params, "signature": hmac_sha256_hex(self.api_secret, urlencode(sorted(params.items())))},
            headers=self._signed_headers(),
        )
        body = _json_or_error(resp)
        return {
            "ok": True,
            "exchange": self.name,
            "order_id": str(body.get("orderId", order_id)),
            "status": body.get("status", ""),
            "executed_qty": body.get("executedQty", "0"),
            "cummulative_quote_qty": body.get("cummulativeQuoteQty", "0"),
            "raw": body,
        }

    def capabilities(self) -> list[Capability]:
        return [
            Capability("public_market_data", True, "data-api.binance.vision，无需鉴权"),
            Capability("readonly_account", True, "只读密钥可拉余额与成交"),
            Capability("simulated_trading", True, "Spot Testnet（testnet.binance.vision）"),
            Capability("live_trading", True, "需实盘密钥；平台默认不启用"),
            Capability("market_order", True, ""),
            Capability("limit_order", True, "支持 GTC"),
            Capability("websocket_stream", True, "data-stream.binance.vision"),
            Capability("perpetual_market_data", True, "U 本位永续 K 线与资金费率：fapi.binance.com（公共接口）"),
            Capability("withdraw", False, "平台不提供提现能力"),
        ]


def _json_or_error(resp: httpx.Response) -> dict[str, Any]:
    """把交易所的错误响应变成可读异常，而不是让 KeyError 冒到上层。"""
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text[:200]
        raise UnsupportedFeature(f"交易所返回错误 {resp.status_code}：{detail}")
    try:
        return resp.json()
    except ValueError as exc:
        raise UnsupportedFeature(f"交易所返回了非 JSON 响应：{resp.text[:200]}") from exc


def _resample(base: list[Bar], symbol: str, timeframe: str, step: int, start: int, end: int) -> list[Bar]:
    """把细周期 K 线（例如 1s）按 step 毫秒聚合成粗周期；只有凑齐的完整区间才算已收盘。"""
    out: list[Bar] = []
    bucket: list[Bar] = []
    cur = None
    for b in base:
        k = b.open_time - b.open_time % step
        if cur is not None and k != cur and bucket:
            out.append(_merge(bucket, symbol, timeframe, cur, step, end))
            bucket = []
        cur = k
        bucket.append(b)
    if bucket and cur is not None:
        out.append(_merge(bucket, symbol, timeframe, cur, step, end))
    return out


def _merge(bucket: list[Bar], symbol: str, timeframe: str, t: int, step: int, end: int) -> Bar:
    close_time = t + step - 1
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=t,
        close_time=close_time,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
        closed=close_time < end and all(b.closed for b in bucket),
    )
