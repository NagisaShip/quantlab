"""交易所模拟盘下单客户端。

**这个模块里不存在实盘地址。** 能连的只有下面几个模拟环境，地址是常量，调用方传不进别的主机：

| 环境 | 地址 | 价格是否跟实盘 |
| --- | --- | --- |
| binance / demo | https://demo-api.binance.com | 接近实盘（官方：“贴近但不等于”） |
| binance / testnet | https://testnet.binance.vision | 不跟：价格和盘口独立，只用于接口联调 |
| okx / demo | https://www.okx.com + 请求头 x-simulated-trading: 1 | 接近实盘 |
| bybit / demo | https://api-demo.bybit.com | 接近实盘（Bybit 主站「模拟交易」） |
| bybit / testnet | https://api-testnet.bybit.com | 不跟：测试网独立撮合，只用于接口联调 |

OKX 模拟盘和实盘共用域名，区别只在请求头。这里的请求头是写死的；而且 OKX 的实盘 Key
带着这个头会被交易所拒绝（Key 与环境不匹配），所以填错 Key 的后果是“连不上”，不会变成实盘下单。

只提供市价单下单、按客户端订单号查单、查余额、查交易规则这几样。没有提现、没有划转。

永续合约（`BTC/USDT:PERP`）同样只走模拟环境：Binance U 本位合约的模拟交易 demo-fapi.binance.com
（Testnet 为 testnet.binancefuture.com），OKX 用同一个模拟盘账户的 -SWAP 合约，Bybit 用同一个统一账户的
USDT 线性永续（category=linear）。一律单向持仓；能设逐仓的都设逐仓。

Bybit 的客户端只认 BYBIT_DEMO_HOSTS 里的两个模拟域名，构造时再核对一遍：即使有人改了 ENVIRONMENTS，
填进 api.bybit.com 这类实盘域名也会直接报错。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from ..exchanges.signing import hmac_sha256_hex
from . import nodeguard

ENVIRONMENTS: dict[tuple[str, str], dict[str, Any]] = {
    ("binance", "demo"): {
        "base": "https://demo-api.binance.com",
        "futures_base": "https://demo-fapi.binance.com",
        "label": "Binance 模拟交易（Demo Mode）",
        "realistic": True,
        "note": "价格和盘口贴近实盘；余额可在 Binance 网页的模拟交易里随时重置",
        "key_help": "在 Binance 网页切到「模拟交易」，在 API 管理里创建 Key",
    },
    ("binance", "testnet"): {
        "base": "https://testnet.binance.vision",
        "futures_base": "https://testnet.binancefuture.com",
        "label": "Binance Spot Testnet",
        "realistic": False,
        "note": "价格和盘口与实盘无关，只用于接口联调，不能用来判断策略效果",
        "key_help": "在 testnet.binance.vision 用 GitHub 登录后生成 Key",
    },
    ("okx", "demo"): {
        "futures_base": "https://www.okx.com",
        "base": "https://www.okx.com",
        "label": "OKX 模拟盘",
        "realistic": True,
        "note": "价格跟随实盘；模拟盘 Key 只能在模拟交易模式下创建",
        "key_help": "在 OKX 切到「模拟交易」，在 API 页面创建 Key（需要 Passphrase）",
    },
    ("bybit", "demo"): {
        "base": "https://api-demo.bybit.com",
        "futures_base": "https://api-demo.bybit.com",
        "label": "Bybit 模拟交易（Demo Trading）",
        "realistic": True,
        "note": "价格跟随实盘；支持现货和 USDT 永续，资金在 Bybit 网页的模拟交易里可随时补充",
        "key_help": (
            "登录 Bybit 主站，切到「模拟交易（Demo Trading）」，在该模式下的 API 管理里创建 Key（需勾选读写/交易权限）"
        ),
    },
    ("bybit", "testnet"): {
        "base": "https://api-testnet.bybit.com",
        "futures_base": "https://api-testnet.bybit.com",
        "label": "Bybit Testnet",
        "realistic": False,
        "note": "测试网独立撮合，价格和盘口与实盘无关，只用于接口联调，不能用来判断策略效果",
        "key_help": "在 testnet.bybit.com 注册测试网账户后创建 API Key",
    },
}

#: Bybit 客户端只允许这两个模拟域名（实盘 api.bybit.com / api.bytick.com 等一律拒绝）
BYBIT_DEMO_HOSTS = frozenset({"api-demo.bybit.com", "api-testnet.bybit.com"})


class BrokerError(RuntimeError):
    """交易所明确拒绝（不会重试）。"""


class BrokerUnknown(RuntimeError):
    """网络超时等：订单状态未知，必须先查单再决定。"""


@dataclass
class Rules:
    step: Decimal  # 数量步长
    min_qty: Decimal
    min_notional: Decimal
    tick: Decimal = Decimal(0)  # 价格步长（挂止损单用；0 = 不取整）


def round_tick(price: Decimal, tick: Decimal, *, up: bool = False) -> Decimal:
    if tick <= 0:
        return price
    q = (price / tick).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN)
    return (q * tick).normalize()


@dataclass
class OrderReport:
    client_order_id: str
    status: str  # filled | partial | new | rejected | canceled | unknown
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    fee_currency: str = ""
    exchange_order_id: str = ""
    message: str = ""
    ts: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _d(v: Any) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, "") else Decimal(0)
    except Exception:
        return Decimal(0)


def floor_step(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def is_perp(symbol: str) -> bool:
    return symbol.upper().endswith(":PERP")


class DemoBroker:
    exchange = ""
    env = ""
    use_futures = False  # 会话里有永续合约时打开：余额要把合约账户也算进来

    def prepare_perp(self, symbol: str, leverage: Decimal) -> None:
        """下永续单之前：设成逐仓、设杠杆。"""

    def positions(self) -> dict[str, Decimal]:
        """永续持仓（带符号，按标的币计）。"""
        return {}

    def bills(self, symbol: str | None = None, start: int | None = None, end: int | None = None) -> list[dict]:
        raise BrokerError(f"{self.label} 不支持账单查询")

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> OrderReport: ...
    def query(self, symbol: str, client_order_id: str) -> OrderReport: ...

    # ---- 保护止损（交易所端条件单）：平台停了，止损还挂在交易所 ----
    def place_stop(self, symbol: str, side: str, qty: Decimal, trigger: Decimal, client_order_id: str) -> OrderReport:
        raise BrokerError(f"{self.exchange} 模拟盘暂不支持挂止损单")

    def cancel_stop(self, symbol: str, client_order_id: str) -> None:
        raise BrokerError(f"{self.exchange} 模拟盘暂不支持挂止损单")

    def query_stop(self, symbol: str, client_order_id: str) -> OrderReport:
        raise BrokerError(f"{self.exchange} 模拟盘暂不支持挂止损单")

    def balances(self) -> dict[str, Decimal]: ...
    def rules(self, symbol: str) -> Rules: ...
    def close(self) -> None: ...

    @property
    def label(self) -> str:
        return ENVIRONMENTS[(self.exchange, self.env)]["label"]


def _gate(exchange: str) -> None:
    """发请求前取限频令牌；本地等太久就不发（这笔没到交易所，按拒单处理是安全的）。"""
    try:
        nodeguard.bucket(exchange).acquire()
    except nodeguard.RateLimited as exc:
        raise BrokerError(str(exc)) from exc


def _throttled(exchange: str, resp: httpx.Response) -> None:
    if resp.status_code in (418, 429):
        wait = nodeguard.retry_after(resp)
        nodeguard.bucket(exchange).penalize(wait)
        raise BrokerError(f"交易所限频（HTTP {resp.status_code}），暂停 {wait:.0f} 秒；这笔没有下")


def _raise_for(resp: httpx.Response, body: Any) -> None:
    if resp.status_code >= 500:
        raise BrokerUnknown(f"交易所服务错误 HTTP {resp.status_code}")
    if resp.status_code >= 400:
        msg = body.get("msg") if isinstance(body, dict) else str(body)[:200]
        raise BrokerError(f"HTTP {resp.status_code}：{msg}")


class BinanceDemoBroker(DemoBroker):
    exchange = "binance"

    def __init__(
        self, api_key: str, api_secret: str, *, env: str = "demo", transport: httpx.BaseTransport | None = None
    ) -> None:
        if ("binance", env) not in ENVIRONMENTS:
            raise ValueError(f"Binance 只支持模拟环境 demo / testnet，收到 {env!r}")
        self.env = env
        self.api_key, self.api_secret = api_key, api_secret
        self.base = ENVIRONMENTS[("binance", env)]["base"]
        self.futures_base = ENVIRONMENTS[("binance", env)]["futures_base"]
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._f = httpx.Client(base_url=self.futures_base, timeout=10.0, transport=transport)
        self._rules: dict[str, Rules] = {}

    def _signed(self, method: str, path: str, params: dict[str, Any], *, futures: bool = False) -> Any:
        _gate("binance")
        q = {**params, "timestamp": nodeguard.CLOCK.now_ms("binance"), "recvWindow": 10_000}
        qs = urlencode(q)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        client = self._f if futures else self._c
        try:
            resp = client.request(method, f"{path}?{qs}&signature={sig}", headers={"X-MBX-APIKEY": self.api_key})
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrokerUnknown(f"{type(exc).__name__}: {exc}") from exc
        _throttled("binance", resp)
        try:
            body = resp.json()
        except ValueError:
            body = {"msg": resp.text[:200]}
        _raise_for(resp, body)
        return body

    def balances(self) -> dict[str, Decimal]:
        body = self._signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        out = {b["asset"]: _d(b["free"]) + _d(b["locked"]) for b in body.get("balances", [])}
        if self.use_futures:
            # 现货与 U 本位合约是两个钱包：对账按两者之和（合约钱包余额含保证金、不含浮动盈亏）
            for b in self._signed("GET", "/fapi/v2/balance", {}, futures=True) or []:
                out[b["asset"]] = out.get(b["asset"], Decimal(0)) + _d(b.get("balance"))
        return out

    def bills(self, symbol: str | None = None, start: int | None = None, end: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 1000}
        if symbol:
            params["symbol"] = self._fsym(symbol)
        if start:
            params["startTime"] = int(start)
        if end:
            params["endTime"] = int(end)
        return list(self._signed("GET", "/fapi/v1/income", params, futures=True) or [])

    @staticmethod
    def _fsym(symbol: str) -> str:
        return symbol.split(":")[0].replace("/", "")

    def prepare_perp(self, symbol: str, leverage: Decimal) -> None:
        try:
            self._signed(
                "POST", "/fapi/v1/marginType", {"symbol": self._fsym(symbol), "marginType": "ISOLATED"}, futures=True
            )
        except BrokerError as exc:
            if "-4046" not in str(exc) and "No need to change" not in str(exc):
                raise
        self._signed(
            "POST", "/fapi/v1/leverage", {"symbol": self._fsym(symbol), "leverage": int(leverage)}, futures=True
        )

    def positions(self) -> dict[str, Decimal]:
        rows = self._signed("GET", "/fapi/v2/positionRisk", {}, futures=True) or []
        out = {}
        for r in rows:
            amt = _d(r.get("positionAmt"))
            if amt:
                sym = r["symbol"]
                out[f"{sym[:-4]}/{sym[-4:]}:PERP" if sym.endswith("USDT") else sym] = amt
        return out

    def rules(self, symbol: str) -> Rules:
        if is_perp(symbol) and symbol not in self._rules:
            resp = self._f.get("/fapi/v1/exchangeInfo")
            body = resp.json()
            _raise_for(resp, body)
            row = next((x for x in body.get("symbols", []) if x.get("symbol") == self._fsym(symbol)), None)
            if row is None:
                raise BrokerError(f"模拟盘没有合约 {symbol}")
            f = {x["filterType"]: x for x in row["filters"]}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
            self._rules[symbol] = Rules(
                _d(lot.get("stepSize")),
                _d(lot.get("minQty")),
                _d((f.get("MIN_NOTIONAL") or {}).get("notional")),
                _d((f.get("PRICE_FILTER") or {}).get("tickSize")),
            )
        if symbol not in self._rules:
            resp = self._c.get("/api/v3/exchangeInfo", params={"symbol": symbol.replace("/", "")})
            body = resp.json()
            _raise_for(resp, body)
            f = {x["filterType"]: x for x in body["symbols"][0]["filters"]}
            lot = f.get("LOT_SIZE") or {}
            mn = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._rules[symbol] = Rules(
                _d(lot.get("stepSize")),
                _d(lot.get("minQty")),
                _d(mn.get("minNotional")),
                _d((f.get("PRICE_FILTER") or {}).get("tickSize")),
            )
        return self._rules[symbol]

    def _report(self, body: dict[str, Any], coid: str) -> OrderReport:
        status = {
            "FILLED": "filled",
            "PARTIALLY_FILLED": "partial",
            "NEW": "new",
            "CANCELED": "canceled",
            "REJECTED": "rejected",
            "EXPIRED": "canceled",
            "EXPIRED_IN_MATCH": "canceled",
        }.get(body.get("status", ""), "unknown")
        qty = _d(body.get("executedQty"))
        quote = _d(body.get("cummulativeQuoteQty"))
        fee, ccy = Decimal(0), ""
        fills = body.get("fills") or []
        if fills:
            ccy = fills[0].get("commissionAsset", "")
            fee = sum((_d(x.get("commission")) for x in fills if x.get("commissionAsset") == ccy), Decimal(0))
        return OrderReport(
            client_order_id=body.get("clientOrderId") or coid,
            status=status,
            filled_qty=qty,
            avg_price=(quote / qty) if qty > 0 else Decimal(0),
            fee=fee,
            fee_currency=ccy,
            exchange_order_id=str(body.get("orderId", "")),
            ts=int(body.get("transactTime") or body.get("updateTime") or time.time() * 1000),
            raw=body,
        )

    def _freport(self, body: dict[str, Any], coid: str) -> OrderReport:
        status = {
            "FILLED": "filled",
            "PARTIALLY_FILLED": "partial",
            "NEW": "new",
            "CANCELED": "canceled",
            "REJECTED": "rejected",
            "EXPIRED": "canceled",
        }.get(body.get("status", ""), "unknown")
        qty = _d(body.get("executedQty"))
        return OrderReport(
            client_order_id=body.get("clientOrderId") or coid,
            status=status,
            filled_qty=qty,
            avg_price=_d(body.get("avgPrice")),
            exchange_order_id=str(body.get("orderId", "")),
            ts=int(body.get("updateTime") or time.time() * 1000),
            raw=body,
        )

    def _ffee(self, symbol: str, order_id: str) -> tuple[Decimal, str]:
        try:
            rows = self._signed(
                "GET", "/fapi/v1/userTrades", {"symbol": self._fsym(symbol), "orderId": order_id}, futures=True
            )
        except Exception:
            return Decimal(0), ""
        if not rows:
            return Decimal(0), ""
        ccy = rows[0].get("commissionAsset", "")
        return sum((_d(r.get("commission")) for r in rows if r.get("commissionAsset") == ccy), Decimal(0)), ccy

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> OrderReport:
        if is_perp(symbol):
            body = self._signed(
                "POST",
                "/fapi/v1/order",
                {
                    "symbol": self._fsym(symbol),
                    "side": side.upper(),
                    "type": "MARKET",
                    "quantity": format(qty.normalize(), "f"),
                    "newClientOrderId": client_order_id,
                    "newOrderRespType": "RESULT",
                },
                futures=True,
            )
            rep = self._freport(body, client_order_id)
            if rep.filled_qty > 0:
                rep.fee, rep.fee_currency = self._ffee(symbol, rep.exchange_order_id)
            return rep
        body = self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol.replace("/", ""),
                "side": side.upper(),
                "type": "MARKET",
                "quantity": format(qty.normalize(), "f"),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
        )
        return self._report(body, client_order_id)

    def query(self, symbol: str, client_order_id: str) -> OrderReport:
        if is_perp(symbol):
            try:
                body = self._signed(
                    "GET",
                    "/fapi/v1/order",
                    {"symbol": self._fsym(symbol), "origClientOrderId": client_order_id},
                    futures=True,
                )
            except BrokerError as exc:
                if "-2013" in str(exc) or "does not exist" in str(exc):
                    return OrderReport(client_order_id, "rejected", message="交易所没有这笔订单（未送达）")
                raise
            rep = self._freport(body, client_order_id)
            if rep.filled_qty > 0:
                rep.fee, rep.fee_currency = self._ffee(symbol, rep.exchange_order_id)
            return rep
        try:
            body = self._signed(
                "GET", "/api/v3/order", {"symbol": symbol.replace("/", ""), "origClientOrderId": client_order_id}
            )
        except BrokerError as exc:
            if "-2013" in str(exc) or "does not exist" in str(exc):
                return OrderReport(client_order_id, "rejected", message="交易所没有这笔订单（未送达）")
            raise
        rep = self._report(body, client_order_id)
        if rep.filled_qty > 0 and rep.fee == 0:
            rep.fee, rep.fee_currency = self._fee_from_trades(symbol, rep.exchange_order_id)
        return rep

    # ---- 保护止损 ----
    STOP_LIMIT_BUFFER = Decimal("0.01")  # 现货止损限价单：限价比触发价再让 1%，基本等同市价

    def place_stop(self, symbol: str, side: str, qty: Decimal, trigger: Decimal, client_order_id: str) -> OrderReport:
        tick = self.rules(symbol).tick
        if is_perp(symbol):
            # U 本位合约的条件单走 Algo 服务（/fapi/v1/algoOrder），只减仓，按标记价格触发
            body = self._signed(
                "POST",
                "/fapi/v1/algoOrder",
                {
                    "algoType": "CONDITIONAL",
                    "symbol": self._fsym(symbol),
                    "side": side.upper(),
                    "type": "STOP_MARKET",
                    "triggerPrice": format(round_tick(trigger, tick, up=side == "buy"), "f"),
                    "quantity": format(qty.normalize(), "f"),
                    "reduceOnly": "true",
                    "workingType": "MARK_PRICE",
                    "clientAlgoId": client_order_id,
                },
                futures=True,
            )
            return OrderReport(client_order_id, "new", exchange_order_id=str(body.get("algoId", "")), raw=body)
        sell = side == "sell"
        limit = trigger * (1 - self.STOP_LIMIT_BUFFER) if sell else trigger * (1 + self.STOP_LIMIT_BUFFER)
        body = self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol.replace("/", ""),
                "side": side.upper(),
                "type": "STOP_LOSS_LIMIT",
                "timeInForce": "GTC",
                "quantity": format(qty.normalize(), "f"),
                "stopPrice": format(round_tick(trigger, tick, up=not sell), "f"),
                "price": format(round_tick(limit, tick, up=not sell), "f"),
                "newClientOrderId": client_order_id,
            },
        )
        return self._report(body, client_order_id)

    def cancel_stop(self, symbol: str, client_order_id: str) -> None:
        try:
            if is_perp(symbol):
                self._signed("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id}, futures=True)
            else:
                self._signed(
                    "DELETE", "/api/v3/order", {"symbol": symbol.replace("/", ""), "origClientOrderId": client_order_id}
                )
        except BrokerError as exc:
            if "-2011" not in str(exc) and "-2013" not in str(exc) and "Unknown order" not in str(exc):
                raise

    def query_stop(self, symbol: str, client_order_id: str) -> OrderReport:
        if not is_perp(symbol):
            return self.query(symbol, client_order_id)
        body = self._signed("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id}, futures=True)
        st = str(body.get("algoStatus") or "").upper()
        if st in ("NEW", "TRIGGERING"):
            return OrderReport(client_order_id, "new", raw=body)
        if st in ("CANCELED", "EXPIRED", "REJECTED"):
            return OrderReport(client_order_id, "canceled", message=st, raw=body)
        oid = body.get("actualOrderId") or body.get("orderId")
        if oid:
            o = self._signed("GET", "/fapi/v1/order", {"symbol": self._fsym(symbol), "orderId": oid}, futures=True)
            rep = self._freport(o, client_order_id)
            rep.client_order_id = client_order_id
            if rep.filled_qty > 0:
                rep.fee, rep.fee_currency = self._ffee(symbol, rep.exchange_order_id)
            return rep
        # 触发了但查不到实际成交单：按触发价估算，注明
        return OrderReport(
            client_order_id,
            "filled",
            _d(body.get("quantity")),
            _d(body.get("triggerPrice")),
            message="交易所未返回实际成交单，按触发价估算",
            raw=body,
        )

    def _fee_from_trades(self, symbol: str, order_id: str) -> tuple[Decimal, str]:
        try:
            rows = self._signed("GET", "/api/v3/myTrades", {"symbol": symbol.replace("/", ""), "orderId": order_id})
        except Exception:
            return Decimal(0), ""
        if not rows:
            return Decimal(0), ""
        ccy = rows[0].get("commissionAsset", "")
        return sum((_d(r.get("commission")) for r in rows if r.get("commissionAsset") == ccy), Decimal(0)), ccy

    def close(self) -> None:
        self._c.close()
        self._f.close()


class OKXDemoBroker(DemoBroker):
    exchange = "okx"
    env = "demo"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        *,
        env: str = "demo",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if env != "demo":
            raise ValueError("OKX 只支持模拟盘（demo）")
        self.api_key, self.api_secret, self.passphrase = api_key, api_secret, passphrase
        self.base = ENVIRONMENTS[("okx", "demo")]["base"]
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._rules: dict[str, Rules] = {}
        self._ct_val: dict[str, Decimal] = {}  # 永续每张合约 = 多少标的币

    @staticmethod
    def _inst(symbol: str) -> str:
        base = symbol.split(":")[0].replace("/", "-")
        return f"{base}-SWAP" if is_perp(symbol) else base

    def _headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        now = datetime.fromtimestamp(nodeguard.CLOCK.now_ms("okx") / 1000, UTC)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        mac = hmac.new(self.api_secret.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256)
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": base64.b64encode(mac.digest()).decode(),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "x-simulated-trading": "1",  # 写死：这个客户端只会访问模拟盘
        }

    def _call(
        self, method: str, path: str, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        full = path + (("?" + urlencode(params)) if params else "")
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        _gate("okx")
        try:
            resp = self._c.request(method, full, content=body or None, headers=self._headers(method, full, body))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrokerUnknown(f"{type(exc).__name__}: {exc}") from exc
        _throttled("okx", resp)
        try:
            data = resp.json()
        except ValueError:
            data = {"msg": resp.text[:200]}
        _raise_for(resp, data)
        if str(data.get("code")) not in ("0", ""):
            rows = data.get("data") or [{}]
            detail = rows[0].get("sMsg") if rows and isinstance(rows[0], dict) else ""
            raise BrokerError(f"OKX {data.get('code')}：{detail or data.get('msg')}")
        return data.get("data") or []

    def balances(self) -> dict[str, Decimal]:
        rows = self._call("GET", "/api/v5/account/balance")
        details = (rows[0] if rows else {}).get("details") or []
        return {d["ccy"]: _d(d.get("cashBal") or d.get("eq")) for d in details}

    def bills(self, symbol: str | None = None, start: int | None = None, end: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"instType": "SWAP", "limit": "100"}
        if symbol:
            params["instId"] = self._inst(symbol)
        if start:
            params["begin"] = int(start)
        if end:
            params["end"] = int(end)
        return list(self._call("GET", "/api/v5/account/bills", params) or [])

    def rules(self, symbol: str) -> Rules:
        if symbol not in self._rules:
            perp = is_perp(symbol)
            rows = self._call(
                "GET",
                "/api/v5/public/instruments",
                {"instType": "SWAP" if perp else "SPOT", "instId": self._inst(symbol)},
            )
            r = rows[0]
            if perp:
                # 合约按张下单；对外统一按标的币数量：步长 = 每张标的币数 × 张数步长
                ct = _d(r.get("ctVal")) or Decimal(1)
                self._ct_val[symbol] = ct
                self._rules[symbol] = Rules(
                    _d(r.get("lotSz")) * ct, _d(r.get("minSz")) * ct, Decimal(0), _d(r.get("tickSz"))
                )
            else:
                self._rules[symbol] = Rules(_d(r.get("lotSz")), _d(r.get("minSz")), Decimal(0), _d(r.get("tickSz")))
        return self._rules[symbol]

    def _ct(self, symbol: str) -> Decimal:
        if symbol not in self._ct_val:
            self.rules(symbol)
        return self._ct_val.get(symbol, Decimal(1))

    def prepare_perp(self, symbol: str, leverage: Decimal) -> None:
        cfg = self._call("GET", "/api/v5/account/config")
        if cfg and cfg[0].get("posMode") not in (None, "net_mode"):
            try:
                self._call("POST", "/api/v5/account/set-position-mode", payload={"posMode": "net_mode"})
            except BrokerError as exc:
                raise BrokerError(f"OKX 模拟盘需要单向持仓模式（net_mode），自动切换失败：{exc}") from exc
        self._call(
            "POST",
            "/api/v5/account/set-leverage",
            payload={"instId": self._inst(symbol), "lever": str(int(leverage)), "mgnMode": "isolated"},
        )

    def positions(self) -> dict[str, Decimal]:
        rows = self._call("GET", "/api/v5/account/positions", {"instType": "SWAP"})
        out: dict[str, Decimal] = {}
        for r in rows:
            inst = r.get("instId", "")
            if not inst.endswith("-SWAP") or not _d(r.get("pos")):
                continue
            sym = inst[: -len("-SWAP")].replace("-", "/") + ":PERP"
            out[sym] = _d(r.get("pos")) * self._ct(sym)
        return out

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> OrderReport:
        if is_perp(symbol):
            payload = {
                "instId": self._inst(symbol),
                "tdMode": "isolated",
                "side": side,
                "ordType": "market",
                "sz": format((qty / self._ct(symbol)).normalize(), "f"),  # 张数
                "clOrdId": client_order_id,
            }
        else:
            payload = {
                "instId": self._inst(symbol),
                "tdMode": "cash",
                "side": side,
                "ordType": "market",
                "sz": format(qty.normalize(), "f"),
                "tgtCcy": "base_ccy",  # 数量按标的币计，买卖口径一致
                "clOrdId": client_order_id,
            }
        rows = self._call("POST", "/api/v5/trade/order", payload=payload)
        first = rows[0] if rows else {}
        if first.get("sCode") not in (None, "0"):
            raise BrokerError(f"OKX 拒单：{first.get('sCode')} {first.get('sMsg')}")
        # OKX 下单只返回订单号，成交结果要查
        return OrderReport(
            client_order_id, "new", exchange_order_id=str(first.get("ordId", "")), ts=int(time.time() * 1000)
        )

    def query(self, symbol: str, client_order_id: str) -> OrderReport:
        try:
            rows = self._call("GET", "/api/v5/trade/order", {"instId": self._inst(symbol), "clOrdId": client_order_id})
        except BrokerError as exc:
            if "51603" in str(exc):  # Order does not exist
                return OrderReport(client_order_id, "rejected", message="交易所没有这笔订单（未送达）")
            raise
        r = rows[0] if rows else {}
        status = {
            "filled": "filled",
            "partially_filled": "partial",
            "live": "new",
            "canceled": "canceled",
            "mmp_canceled": "canceled",
        }.get(r.get("state", ""), "unknown")
        return OrderReport(
            client_order_id=client_order_id,
            status=status,
            filled_qty=_d(r.get("accFillSz")) * (self._ct(symbol) if is_perp(symbol) else 1),
            avg_price=_d(r.get("avgPx")),
            fee=abs(_d(r.get("fee"))),  # OKX 的手续费是负数
            fee_currency=r.get("feeCcy", ""),
            exchange_order_id=str(r.get("ordId", "")),
            ts=int(r.get("uTime") or time.time() * 1000),
            raw=r,
        )

    # ---- 保护止损：OKX 策略委托（conditional，触发后按市价） ----
    def place_stop(self, symbol: str, side: str, qty: Decimal, trigger: Decimal, client_order_id: str) -> OrderReport:
        perp = is_perp(symbol)
        tick = self.rules(symbol).tick
        payload: dict[str, Any] = {
            "instId": self._inst(symbol),
            "tdMode": "isolated" if perp else "cash",
            "side": side,
            "ordType": "conditional",
            "sz": format(((qty / self._ct(symbol)) if perp else qty).normalize(), "f"),
            "slTriggerPx": format(round_tick(trigger, tick, up=side == "buy"), "f"),
            "slOrdPx": "-1",
            "algoClOrdId": client_order_id,
        }
        if perp:
            payload["reduceOnly"] = True
        else:
            payload["tgtCcy"] = "base_ccy"
        rows = self._call("POST", "/api/v5/trade/order-algo", payload=payload)
        first = rows[0] if rows else {}
        if first.get("sCode") not in (None, "0"):
            raise BrokerError(f"OKX 拒绝止损单：{first.get('sCode')} {first.get('sMsg')}")
        return OrderReport(client_order_id, "new", exchange_order_id=str(first.get("algoId", "")))

    def _algo(self, client_order_id: str) -> dict[str, Any] | None:
        for path in ("/api/v5/trade/order-algo",):
            try:
                rows = self._call("GET", path, {"algoClOrdId": client_order_id})
            except BrokerError as exc:
                if "51603" in str(exc) or "51000" in str(exc):
                    return None
                raise
            if rows:
                return rows[0]
        return None

    def cancel_stop(self, symbol: str, client_order_id: str) -> None:
        a = self._algo(client_order_id)
        if a is None or a.get("state") not in ("live", "pause"):
            return
        self._call(
            "POST", "/api/v5/trade/cancel-algos", payload=[{"instId": self._inst(symbol), "algoId": a.get("algoId")}]
        )  # type: ignore[arg-type]

    def query_stop(self, symbol: str, client_order_id: str) -> OrderReport:
        a = self._algo(client_order_id)
        if a is None:
            return OrderReport(client_order_id, "canceled", message="交易所没有这张止损单")
        st = a.get("state")
        if st in ("live", "pause", "partially_effective"):
            return OrderReport(client_order_id, "new", raw=a)
        if st != "effective":
            return OrderReport(client_order_id, "canceled", message=str(st), raw=a)
        oid = a.get("ordId") or (a.get("ordIdList") or [""])[0]
        rows = self._call("GET", "/api/v5/trade/order", {"instId": self._inst(symbol), "ordId": oid}) if oid else []
        r = rows[0] if rows else {}
        return OrderReport(
            client_order_id,
            "filled" if r.get("state") == "filled" else "partial",
            filled_qty=_d(r.get("accFillSz")) * (self._ct(symbol) if is_perp(symbol) else 1),
            avg_price=_d(r.get("avgPx")),
            fee=abs(_d(r.get("fee"))),
            fee_currency=r.get("feeCcy", ""),
            exchange_order_id=str(oid),
            ts=int(r.get("uTime") or time.time() * 1000),
            raw=r,
        )

    def close(self) -> None:
        self._c.close()


class BybitDemoBroker(DemoBroker):
    """Bybit V5：现货（category=spot）+ USDT 线性永续（category=linear），统一账户（UTA）。"""

    exchange = "bybit"
    RECV_WINDOW = "10000"
    LINK_ID_MAX = 36  # Bybit orderLinkId 最长 36 位

    def __init__(
        self, api_key: str, api_secret: str, *, env: str = "demo", transport: httpx.BaseTransport | None = None
    ) -> None:
        if ("bybit", env) not in ENVIRONMENTS:
            raise ValueError(f"Bybit 只支持模拟环境 demo / testnet，收到 {env!r}")
        base = ENVIRONMENTS[("bybit", env)]["base"]
        host = httpx.URL(base).host
        if host not in BYBIT_DEMO_HOSTS:
            raise ValueError(f"Bybit 只允许模拟域名 {'、'.join(sorted(BYBIT_DEMO_HOSTS))}，拒绝 {host}")
        self.env = env
        self.api_key, self.api_secret = api_key, api_secret
        self.base = base
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._rules: dict[str, Rules] = {}

    # ---- 基础 ----
    @staticmethod
    def _cat(symbol: str) -> str:
        return "linear" if is_perp(symbol) else "spot"

    @staticmethod
    def _sym(symbol: str) -> str:
        return symbol.split(":")[0].replace("/", "").upper()

    @classmethod
    def link_id(cls, client_order_id: str) -> str:
        """客户端订单号 → orderLinkId。平台的号（q + 31 位十六进制）原样使用；
        超长或带非法字符的号确定性地哈希成 36 位以内，保证下单和查单用的是同一个号。"""
        ok = all(ch.isalnum() or ch in "-_" for ch in client_order_id)
        if client_order_id and ok and len(client_order_id) <= cls.LINK_ID_MAX:
            return client_order_id
        return "h" + hashlib.sha256(client_order_id.encode()).hexdigest()[: cls.LINK_ID_MAX - 1]

    def _headers(self, ts: str, payload: str) -> dict[str, str]:
        sign = hmac_sha256_hex(self.api_secret, f"{ts}{self.api_key}{self.RECV_WINDOW}{payload}")
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self.RECV_WINDOW,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        *,
        signed: bool = True,
    ) -> dict[str, Any]:
        _gate("bybit")
        ts = str(nodeguard.CLOCK.now_ms("bybit"))
        try:
            if method == "GET":
                qs = urlencode(params or {})
                headers = self._headers(ts, qs) if signed else {}
                resp = self._c.get(path + (f"?{qs}" if qs else ""), headers=headers)
            else:
                body = json.dumps(payload or {}, separators=(",", ":"))
                resp = self._c.request(method, path, content=body, headers=self._headers(ts, body))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise BrokerUnknown(f"{type(exc).__name__}: {exc}") from exc
        _throttled("bybit", resp)
        try:
            data = resp.json()
        except ValueError:
            data = {"retMsg": resp.text[:200]}
        if resp.status_code >= 500:
            raise BrokerUnknown(f"交易所服务错误 HTTP {resp.status_code}")
        if resp.status_code >= 400:
            msg = data.get("retMsg") if isinstance(data, dict) else str(data)[:200]
            raise BrokerError(f"HTTP {resp.status_code}：{msg}")
        if not isinstance(data, dict):
            raise BrokerError(f"Bybit 返回格式异常：{str(data)[:200]}")
        if int(data.get("retCode") or 0) != 0:
            raise BrokerError(f"Bybit {data.get('retCode')}：{data.get('retMsg')}")
        return data.get("result") or {}

    # ---- 账户 ----
    def balances(self) -> dict[str, Decimal]:
        # 统一账户：现货和 USDT 永续共用一个钱包，walletBalance 不含未实现盈亏
        res = self._call("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        out: dict[str, Decimal] = {}
        for acct in res.get("list") or []:
            for c in acct.get("coin") or []:
                out[c["coin"]] = out.get(c["coin"], Decimal(0)) + _d(c.get("walletBalance"))
        return out

    def bills(self, symbol: str | None = None, start: int | None = None, end: int | None = None) -> list[dict]:
        params: dict[str, Any] = {"accountType": "UNIFIED", "category": "linear", "limit": 100}
        if symbol:
            params["symbol"] = self._sym(symbol)
        if start:
            params["startTime"] = int(start)
        if end:
            params["endTime"] = int(end)
        return list(self._call("GET", "/v5/account/transaction-log", params) or [])

    def rules(self, symbol: str) -> Rules:
        if symbol not in self._rules:
            cat = self._cat(symbol)
            res = self._call(
                "GET", "/v5/market/instruments-info", {"category": cat, "symbol": self._sym(symbol)}, signed=False
            )
            rows = res.get("list") or []
            if not rows:
                raise BrokerError(f"模拟盘没有{'合约' if cat == 'linear' else '交易对'} {symbol}")
            lot = rows[0].get("lotSizeFilter") or {}
            tick = _d((rows[0].get("priceFilter") or {}).get("tickSize"))
            if cat == "linear":
                self._rules[symbol] = Rules(
                    _d(lot.get("qtyStep")), _d(lot.get("minOrderQty")), _d(lot.get("minNotionalValue")), tick
                )
            else:
                self._rules[symbol] = Rules(
                    _d(lot.get("basePrecision")), _d(lot.get("minOrderQty")), _d(lot.get("minOrderAmt")), tick
                )
        return self._rules[symbol]

    # ---- 永续 ----
    def prepare_perp(self, symbol: str, leverage: Decimal) -> None:
        sym, lev = self._sym(symbol), str(int(leverage))
        try:
            self._call("POST", "/v5/position/switch-mode", payload={"category": "linear", "coin": "USDT", "mode": 0})
        except BrokerError as exc:
            if "110025" not in str(exc) and "not modified" not in str(exc).lower():
                raise BrokerError(f"Bybit 模拟盘需要单向持仓模式，自动切换失败：{exc}") from exc
        try:
            self._call(
                "POST",
                "/v5/position/switch-isolated",
                payload={"category": "linear", "symbol": sym, "tradeMode": 1, "buyLeverage": lev, "sellLeverage": lev},
            )
        except BrokerError as exc:
            text = str(exc).lower()
            # 110026 已经是逐仓；统一账户（UTA）不支持逐笔切换（保证金模式在账户级设置），只能靠下面设杠杆
            if not (
                "110026" in text
                or "100028" in text
                or "unified" in text
                or "not modified" in text
                or "not support" in text
            ):
                raise
        try:
            self._call(
                "POST",
                "/v5/position/set-leverage",
                payload={"category": "linear", "symbol": sym, "buyLeverage": lev, "sellLeverage": lev},
            )
        except BrokerError as exc:
            if "110043" not in str(exc):  # leverage not modified
                raise

    def positions(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        cursor = ""
        for _ in range(20):  # 分页，最多 20 页
            params: dict[str, Any] = {"category": "linear", "settleCoin": "USDT", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            res = self._call("GET", "/v5/position/list", params)
            for r in res.get("list") or []:
                size = _d(r.get("size"))
                sym = r.get("symbol", "")
                if not size or not sym.endswith("USDT"):
                    continue
                key = f"{sym[:-4]}/USDT:PERP"
                out[key] = out.get(key, Decimal(0)) + (-size if r.get("side") == "Sell" else size)
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
        return {k: v for k, v in out.items() if v}

    # ---- 下单 / 查单 ----
    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> OrderReport:
        payload: dict[str, Any] = {
            "category": self._cat(symbol),
            "symbol": self._sym(symbol),
            "side": "Buy" if side.lower() == "buy" else "Sell",
            "orderType": "Market",
            "qty": format(qty.normalize(), "f"),
            "orderLinkId": self.link_id(client_order_id),
        }
        if is_perp(symbol):
            payload["positionIdx"] = 0  # 单向持仓
        else:
            payload["marketUnit"] = "baseCoin"  # 现货市价买默认按计价币下单；统一按标的币数量
        res = self._call("POST", "/v5/order/create", payload=payload)
        # Bybit 下单只返回订单号，成交结果要查
        return OrderReport(
            client_order_id, "new", exchange_order_id=str(res.get("orderId", "")), ts=int(time.time() * 1000)
        )

    # ---- 保护止损：条件市价单（triggerDirection 1 = 涨到触发、2 = 跌到触发） ----
    def place_stop(self, symbol: str, side: str, qty: Decimal, trigger: Decimal, client_order_id: str) -> OrderReport:
        tick = self.rules(symbol).tick
        buy = side.lower() == "buy"
        payload: dict[str, Any] = {
            "category": self._cat(symbol),
            "symbol": self._sym(symbol),
            "side": "Buy" if buy else "Sell",
            "orderType": "Market",
            "qty": format(qty.normalize(), "f"),
            "triggerPrice": format(round_tick(trigger, tick, up=buy), "f"),
            "triggerDirection": 1 if buy else 2,
            "orderLinkId": self.link_id(client_order_id),
        }
        if is_perp(symbol):
            payload.update({"positionIdx": 0, "reduceOnly": True, "triggerBy": "MarkPrice"})
        else:
            payload.update({"orderFilter": "StopOrder", "marketUnit": "baseCoin"})
        res = self._call("POST", "/v5/order/create", payload=payload)
        return OrderReport(client_order_id, "new", exchange_order_id=str(res.get("orderId", "")))

    def cancel_stop(self, symbol: str, client_order_id: str) -> None:
        payload: dict[str, Any] = {
            "category": self._cat(symbol),
            "symbol": self._sym(symbol),
            "orderLinkId": self.link_id(client_order_id),
        }
        if not is_perp(symbol):
            payload["orderFilter"] = "StopOrder"
        try:
            self._call("POST", "/v5/order/cancel", payload=payload)
        except BrokerError as exc:
            if "110001" not in str(exc) and "170213" not in str(exc):  # 订单不存在（已触发或已撤）
                raise

    def query_stop(self, symbol: str, client_order_id: str) -> OrderReport:
        rep = self.query(symbol, client_order_id)
        if rep.status == "rejected" and "未送达" in rep.message:
            rep.status = "canceled"
        return rep

    _STATUS = {
        "Filled": "filled",
        "PartiallyFilled": "partial",
        "New": "new",
        "Created": "new",
        "Untriggered": "new",
        "Triggered": "new",
        "Cancelled": "canceled",
        "PartiallyFilledCanceled": "canceled",
        "Deactivated": "canceled",
        "Rejected": "rejected",
    }

    def _find(self, symbol: str, link: str) -> dict[str, Any] | None:
        params = {"category": self._cat(symbol), "symbol": self._sym(symbol), "orderLinkId": link}
        for path in ("/v5/order/realtime", "/v5/order/history"):
            rows = self._call("GET", path, params).get("list") or []
            if rows:
                return rows[0]
        return None

    def _fees(self, symbol: str, order_id: str, side: str) -> tuple[Decimal, str] | None:
        try:
            res = self._call(
                "GET",
                "/v5/execution/list",
                {"category": self._cat(symbol), "symbol": self._sym(symbol), "orderId": order_id},
            )
        except Exception:
            return None
        rows = res.get("list") or []
        if not rows:
            return None
        ccy = rows[0].get("feeCurrency") or self._fee_ccy(symbol, side)
        fee = sum((_d(r.get("execFee")) for r in rows if (r.get("feeCurrency") or ccy) == ccy), Decimal(0))
        return abs(fee), ccy

    @staticmethod
    def _fee_ccy(symbol: str, side: str) -> str:
        """成交明细没给币种时的推断：永续按 USDT；现货买入收标的币、卖出收计价币。"""
        if is_perp(symbol):
            return "USDT"
        base, _, quote = symbol.split(":")[0].partition("/")
        return base if side == "Buy" else quote

    def query(self, symbol: str, client_order_id: str) -> OrderReport:
        try:
            r = self._find(symbol, self.link_id(client_order_id))
        except BrokerError as exc:
            if "110001" in str(exc):  # order not exists
                r = None
            else:
                raise
        if r is None:
            return OrderReport(client_order_id, "rejected", message="交易所没有这笔订单（未送达）")
        status = self._STATUS.get(r.get("orderStatus", ""), "unknown")
        qty = _d(r.get("cumExecQty"))
        avg = _d(r.get("avgPrice"))
        if not avg and qty > 0:
            avg = _d(r.get("cumExecValue")) / qty
        side = r.get("side", "")
        fee, ccy = Decimal(0), ""
        if qty > 0:
            got = self._fees(symbol, str(r.get("orderId", "")), side)
            fee, ccy = got if got else (abs(_d(r.get("cumExecFee"))), self._fee_ccy(symbol, side))
        return OrderReport(
            client_order_id=client_order_id,
            status=status,
            filled_qty=qty,
            avg_price=avg,
            fee=fee,
            fee_currency=ccy,
            exchange_order_id=str(r.get("orderId", "")),
            message=str(r.get("rejectReason") or "")
            if status in ("rejected", "canceled") and r.get("rejectReason") not in (None, "", "EC_NoError")
            else "",
            ts=int(r.get("updatedTime") or time.time() * 1000),
            raw=r,
        )

    def close(self) -> None:
        self._c.close()


def make_broker(
    exchange: str, env: str, creds: dict[str, str], transport: httpx.BaseTransport | None = None
) -> DemoBroker:
    if (exchange, env) not in ENVIRONMENTS:
        raise ValueError(f"不支持的模拟环境：{exchange}/{env}")
    if exchange == "binance":
        return BinanceDemoBroker(creds["api_key"], creds["api_secret"], env=env, transport=transport)
    if exchange == "bybit":
        return BybitDemoBroker(creds["api_key"], creds["api_secret"], env=env, transport=transport)
    return OKXDemoBroker(
        creds["api_key"], creds["api_secret"], creds.get("passphrase") or "", env=env, transport=transport
    )
