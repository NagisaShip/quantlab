"""OKX 适配器。

要点（来自 plan.md 第 3 章）：

- 模拟交易必须使用专门的模拟盘密钥，并携带请求头 `x-simulated-trading: 1`。
  少了这个头，请求会打到实盘环境——这是最容易出事的地方，所以它由 `simulated`
  统一控制，调用方无法忘记。
- API 域名取决于账户注册地区（www.okx.com / app.okx.com / us.okx.com / eea.okx.com）。
  这里只允许官方域名，避免把请求发到不明站点。
- 签名规则：`OK-ACCESS-SIGN = base64(HMAC-SHA256(timestamp + method + path + body))`，
  其中 timestamp 必须是 ISO8601 毫秒格式。
"""

from __future__ import annotations

import json
from typing import Any

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
from .signing import decimal_str, hmac_sha256_b64, iso_timestamp_ms

LIVE_BASE = "https://openapi.okx.com"  # 实验三验证可用的公共域名；也可配置为 www.okx.com

# 只允许官方域名：区域账户用对应域名，但绝不接受任意主机
ALLOWED_HOSTS = ("openapi.okx.com", "www.okx.com", "app.okx.com", "us.okx.com", "eea.okx.com")

TIMEFRAME_STEP_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
# 日线用 UTC 开盘口径（1Dutc），与平台统一的 UTC 日界一致
_OKX_BAR_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}


class OKXAdapter(BaseAdapter):
    name = "okx"
    supports_trading = True

    def __init__(
        self,
        *,
        simulated: bool = False,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        base_url: str = LIVE_BASE,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(simulated=simulated)
        host = base_url.rstrip("/").split("://")[-1].split("/")[0]
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"OKX 只允许官方域名：{'、'.join(ALLOWED_HOSTS)}；收到 {host}")
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._client = httpx.Client(base_url=self.base, timeout=15.0, transport=transport)

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.simulated:
            h["x-simulated-trading"] = "1"  # OKX 模拟盘专用头
        if self.api_key:
            h["OK-ACCESS-KEY"] = self.api_key
        return h

    # ---------- 公共行情 ----------
    def ping(self) -> bool:
        return self._client.get("/api/v5/public/time").status_code == 200

    def get_instruments(self) -> list[Instrument]:
        resp = self._client.get("/api/v5/public/instruments", params={"instType": "SPOT"})
        resp.raise_for_status()
        out: list[Instrument] = []
        for s in resp.json().get("data", []):
            base, quote = s["instId"].split("-")[0], s["instId"].split("-")[1]
            out.append(
                Instrument(
                    exchange=self.name,
                    symbol=f"{base}/{quote}",
                    market_type="spot",
                    base=base,
                    quote=quote,
                    tick_size=s.get("tickSz", "0"),
                    lot_size=s.get("lotSz", "0"),
                    min_notional=s.get("minSz", "0"),
                )
            )
        return out

    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        perp = symbol.upper().endswith(":PERP")
        inst_id = inst_id_of(symbol)
        bar_param = _OKX_BAR_MAP.get(timeframe)
        if bar_param is None:
            raise UnsupportedFeature(f"OKX 不支持周期 {timeframe}")
        bars: list[Bar] = []
        after = end  # OKX 用 after 返回更早的数据
        while True:
            resp = self._client.get(
                "/api/v5/market/history-candles",
                params={"instId": inst_id, "bar": bar_param, "after": after, "limit": 100},
            )
            resp.raise_for_status()
            rows = resp.json().get("data", [])
            if not rows:
                break
            for r in rows:
                ts = int(r[0])
                if ts < start:
                    return list(reversed(bars))
                confirm = r[8] == "1" if len(r) > 8 else True
                bars.append(
                    Bar(
                        symbol=symbol,
                        timeframe=timeframe,
                        open_time=ts,
                        # OKX 只返回开盘时间；收盘时间 = 开盘 + 周期。若写成 ts，
                        # 这根 bar 会在开盘时刻就"已收盘"，策略等于提前看到整根 K 线。
                        close_time=ts + TIMEFRAME_STEP_MS[timeframe],
                        open=float(r[1]),
                        high=float(r[2]),
                        low=float(r[3]),
                        close=float(r[4]),
                        # 永续的 r[5] 是张数，r[6] 才是标的币数量；统一按标的币计
                        volume=float(r[6] if perp and len(r) > 6 else r[5]),
                        closed=confirm,
                    )
                )
            after = int(rows[-1][0])
        return list(reversed(bars))

    def fetch_funding_rates(self, symbol: str, start: int, end: int) -> list[FundingRate]:
        """永续资金费率历史（/api/v5/public/funding-rate-history，每页 100 条，交易所只保留约 3 个月）。"""
        if not symbol.upper().endswith(":PERP"):
            raise UnsupportedFeature(f"{symbol} 是现货，没有资金费率（永续写成 BTC/USDT:PERP）")
        inst = inst_id_of(symbol)
        out: list[FundingRate] = []
        after = end
        for _ in range(200):
            resp = self._client.get(
                "/api/v5/public/funding-rate-history", params={"instId": inst, "after": after, "limit": 100}
            )
            rows = _okx_json(resp).get("data") or []
            if not rows:
                break
            for r in rows:
                ts = int(r["fundingTime"])
                if ts < start:
                    return sorted(out, key=lambda f: f.ts)
                out.append(
                    FundingRate(
                        symbol=symbol,
                        ts=ts,
                        rate=str(r.get("realizedRate") or r["fundingRate"]),
                        source="okx:public",
                        instrument_id=f"okx:{symbol}",
                        raw_ref=f"okx:funding-rate-history?instId={inst}&after={after}",
                    )
                )
            after = int(rows[-1]["fundingTime"])
        return sorted(out, key=lambda f: f.ts)

    def fetch_ticker(self, symbol: str) -> dict:
        inst_id = inst_id_of(symbol)
        resp = self._client.get("/api/v5/market/ticker", params={"instId": inst_id})
        resp.raise_for_status()
        d = resp.json()["data"][0]
        return {"price": d["last"], "bid": d["bidPx"], "ask": d["askPx"]}

    def fetch_order_book(self, symbol: str, depth: int = 20) -> dict:
        """公开盘口快照（执行研究的盘口录制用）。"""
        resp = self._client.get("/api/v5/market/books", params={"instId": symbol.replace("/", "-"), "sz": depth})
        resp.raise_for_status()
        d = resp.json()["data"][0]
        return {
            "ts": int(d["ts"]),
            "bids": [[float(x[0]), float(x[1])] for x in d["bids"][:depth]],
            "asks": [[float(x[0]), float(x[1])] for x in d["asks"][:depth]],
        }

    def fetch_trades(self, symbol: str, limit: int = 100) -> list[dict]:
        resp = self._client.get("/api/v5/market/trades", params={"instId": symbol.replace("/", "-"), "limit": limit})
        resp.raise_for_status()
        return [
            {"id": r["tradeId"], "t": int(r["ts"]), "p": float(r["px"]), "q": float(r["sz"]), "side": r["side"]}
            for r in resp.json()["data"]
        ]

    def ws_url(self, symbol: str, channel: str = "candle1m") -> str:
        return "wss://ws.okx.com:8443/ws/v5/public"

    def ws_subscribe(self, symbol: str, channel: str = "candle1m") -> dict:
        return {"op": "subscribe", "args": [{"channel": channel, "instId": inst_id_of(symbol)}]}

    # ---------- 账户 ----------
    def fetch_balances(self) -> list[Balance]:
        if not self.api_key:
            raise UnsupportedFeature("需要模拟盘/只读 API Key 才能访问账户接口")
        path = "/api/v5/account/balance"
        resp = self._client.get(path, headers=self._signed_headers("GET", path, ""))
        body = _okx_json(resp)
        out: list[Balance] = []
        for d in body.get("data", []):
            for detail in d.get("details", []):
                out.append(Balance(asset=detail["ccy"], free=detail["availBal"], locked=detail["frozenBal"]))
        return out

    def fetch_fills(self, symbol: str | None = None, start: int | None = None) -> list[Fill]:
        """拉成交明细（近 3 天用 fills-history，更早用 fills）。"""
        if not self.api_key:
            raise UnsupportedFeature("需要模拟盘/只读 API Key 才能读取成交记录")
        path = "/api/v5/trade/fills-history" if start else "/api/v5/trade/fills"
        params: dict[str, str] = {"instType": "SPOT", "limit": "100"}
        if symbol:
            params["instId"] = symbol.replace("/", "-")
        resp = self._client.get(path, params=params, headers=self._signed_headers("GET", path, ""))
        body = _okx_json(resp)
        out: list[Fill] = []
        for row in body.get("data", []):
            out.append(
                Fill(
                    symbol=symbol or row.get("instId", "").replace("-", "/"),
                    side=row.get("side", ""),
                    price=row.get("fillPx", "0"),
                    qty=row.get("fillSz", "0"),
                    fee=row.get("fee", "0"),
                    fee_asset=row.get("feeCcy", "USDT"),
                    fill_id=str(row.get("tradeId") or row.get("billId") or ""),
                    ts=int(row.get("ts") or 0),
                )
            )
        return out

    # ---------- 签名 ----------
    def _signed_headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """构造 OKX 签名头。

        `x-simulated-trading: 1` 由 `simulated` 统一决定——这是模拟盘与实盘之间
        唯一的分界线，绝不允许调用方临时改。
        """
        if not (self.api_key and self.api_secret and self.passphrase):
            raise UnsupportedFeature("需要 API Key、Secret 与 Passphrase 才能访问账户接口")
        ts = iso_timestamp_ms()
        message = f"{ts}{method.upper()}{path}{body}"
        headers = {
            **self._headers(),
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": hmac_sha256_b64(self.api_secret, message),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        return headers

    # ---------- 交易 ----------
    def place_order(self, intent: OrderIntent) -> dict[str, Any]:
        """下单。市价单用 tgtCcy=base_ccy 明确数量口径，避免"多少币/多少钱"歧义。"""
        if not intent.client_order_id:
            raise ValueError("下单必须携带 client_order_id：没有它就无法与实盘成交逐笔对账")
        if intent.order_type not in ("market", "limit"):
            raise UnsupportedFeature(f"OKX 适配器暂不支持 {intent.order_type} 订单")
        if not (self.api_key and self.api_secret and self.passphrase):
            raise UnsupportedFeature("需要 API Key、Secret 与 Passphrase 才能下单")

        self.cache_instruments()
        qty = self.round_qty(intent.symbol, str(intent.qty))
        if qty <= 0:
            raise ValueError(f"{intent.symbol} 的数量 {intent.qty} 低于最小步长，无法下单")

        payload: dict[str, Any] = {
            "instId": intent.symbol.replace("/", "-"),
            "tdMode": "cash",  # 现货默认现金模式，不加杠杆
            "side": intent.side,
            "ordType": intent.order_type,
            "sz": decimal_str(qty),
            "clOrdId": intent.client_order_id,
        }
        if intent.order_type == "market":
            payload["tgtCcy"] = "base_ccy"
        else:
            price = intent.meta.get("price")
            if price is None:
                raise ValueError("限价单必须提供 price")
            payload["px"] = decimal_str(self.round_price(intent.symbol, str(price)))

        body = json.dumps(payload, separators=(",", ":"))
        path = "/api/v5/trade/order"
        resp = self._client.post(path, content=body, headers=self._signed_headers("POST", path, body))
        data = _okx_json(resp)
        first = (data.get("data") or [{}])[0]
        if first.get("sCode") not in (None, "0"):
            raise UnsupportedFeature(f"OKX 拒单：{first.get('sCode')} {first.get('sMsg')}")
        return {
            "ok": True,
            "exchange": self.name,
            "mode": "simulated" if self.simulated else "live",
            "order_id": str(first.get("ordId", "")),
            "client_order_id": str(first.get("clOrdId", intent.client_order_id)),
            "status": "submitted",
            "raw": data,
        }

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        payload = {"instId": symbol.replace("/", "-"), "ordId": order_id}
        body = json.dumps(payload, separators=(",", ":"))
        path = "/api/v5/trade/cancel-order"
        resp = self._client.post(path, content=body, headers=self._signed_headers("POST", path, body))
        data = _okx_json(resp)
        return {
            "ok": True,
            "exchange": self.name,
            "order_id": order_id,
            "status": "canceled",
            "raw": data,
        }

    def query_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        path = "/api/v5/trade/order"
        params = {"instId": symbol.replace("/", "-"), "ordId": order_id}
        query = f"{path}?instId={params['instId']}&ordId={order_id}"
        resp = self._client.get(path, params=params, headers=self._signed_headers("GET", query, ""))
        data = _okx_json(resp)
        first = (data.get("data") or [{}])[0]
        return {
            "ok": True,
            "exchange": self.name,
            "order_id": order_id,
            "status": first.get("state", ""),
            "executed_qty": first.get("accFillSz", "0"),
            "avg_price": first.get("avgPx", "0"),
            "raw": data,
        }

    def capabilities(self) -> list[Capability]:
        return [
            Capability("public_market_data", True, "公共行情接口，无需鉴权"),
            Capability("readonly_account", True, "只读密钥可拉余额与成交"),
            Capability("simulated_trading", True, "模拟盘密钥 + x-simulated-trading: 1"),
            Capability("live_trading", True, "需实盘密钥；平台默认不启用"),
            Capability("market_order", True, "tgtCcy=base_ccy"),
            Capability("limit_order", True, ""),
            Capability("websocket_stream", True, "wss://ws.okx.com:8443/ws/v5/public"),
            Capability("perpetual_market_data", True, "U 本位永续（-SWAP）K 线与资金费率历史（约 3 个月）"),
            Capability("regional_domain", True, "www / app / us / eea，只允许官方域名"),
            Capability("withdraw", False, "平台不提供提现能力"),
        ]


def inst_id_of(symbol: str) -> str:
    """BTC/USDT → BTC-USDT；BTC/USDT:PERP → BTC-USDT-SWAP。"""
    base = symbol.split(":")[0].replace("/", "-")
    return f"{base}-SWAP" if symbol.upper().endswith(":PERP") else base


def _okx_json(resp: httpx.Response) -> dict[str, Any]:
    """OKX 用 code 字段表示业务结果，HTTP 200 也可能是失败。"""
    if resp.status_code >= 400:
        raise UnsupportedFeature(f"OKX 返回 HTTP {resp.status_code}：{resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise UnsupportedFeature(f"OKX 返回了非 JSON 响应：{resp.text[:200]}") from exc
    if str(body.get("code", "0")) != "0":
        raise UnsupportedFeature(f"OKX 返回错误：{body.get('code')} {body.get('msg')}")
    return body
