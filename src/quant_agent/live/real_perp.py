"""Real-money perpetual execution with a separate safety boundary.

This module deliberately does **not** reuse the spot micro-live flag or its
connection store.  Real perpetual trading is a more dangerous capability: the
server flag, workspace acknowledgement, isolated credential store, futures
account check, exposure caps, margin-mode declaration, exchange-side stop and
emergency-close watchdog are all independent conditions.
"""

from __future__ import annotations

import hashlib
import os
import secrets as _secrets
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from ..exchanges.contracts import contract_type_of, spec_for
from ..platform.errors import AppError, ErrorCode
from . import demo_broker as DB
from . import micro as MI
from .demo_accounts import DemoConnectionService
from .oms import OmsLimits, OmsState

SERVER_FLAG = "QAGENT_REAL_PERP"
KILL_FILE = "real_perp.KILL"
CONFIG = "real_perp_config"
CONNS = "real_perp_connections"
CHALLENGES = "real_perp_challenges"
HARD_CEILING = 10_000.0
NOTICE_VERSION = "2026-10-18-real-perp-v1"
ACK_PHRASE = "我已了解真实永续风险"
ACK_PHRASE_EN = "I understand real perpetual risk"
START_PHRASE = "确认真实永续"
START_PHRASE_EN = "Confirm real perpetual"
SUPPORTED = {"binance", "okx"}
LIVE_HOSTS = {"binance": "https://fapi.binance.com", "okx": "https://www.okx.com"}


def _now() -> int:
    return int(time.time() * 1000)


def server_enabled() -> bool:
    return os.environ.get(SERVER_FLAG, "0") == "1"


def server_killed(home: Path | str) -> bool:
    return (Path(home) / KILL_FILE).exists()


def _quote_notional(intent: dict[str, Any], exchange: str, price: Decimal | None = None) -> Decimal:
    sym = intent["security"]
    spec = spec_for(exchange, sym)
    mult = Decimal(str(spec.contract_multiplier))
    px = price if price is not None else Decimal(str(intent.get("ref_price") or 0))
    if spec.contract_type == "inverse":
        return abs(Decimal(str(intent["qty"]))) * mult
    return abs(Decimal(str(intent["qty"]))) * mult * px


class RealBinancePerpBroker(DB.BinanceDemoBroker):
    """Binance USD-M real endpoint; only perpetual symbols are accepted."""

    def __init__(self, api_key: str, api_secret: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.env = "live"
        self.api_key, self.api_secret = api_key, api_secret
        self.base = "https://api.binance.com"
        self.futures_base = LIVE_HOSTS["binance"]
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._f = httpx.Client(base_url=self.futures_base, timeout=10.0, transport=transport)
        self._rules = {}
        self.use_futures = True
        self.position_side = ""
        self.margin_mode = "isolated"

    def prepare_perp(
        self, symbol: str, leverage: Decimal, *, margin_mode: str = "isolated", position_mode: str = "one-way"
    ) -> None:
        if contract_type_of(symbol) != "linear":
            raise DB.BrokerError("真实永续通道当前只接受 USDT-M 线性合约")
        dual = position_mode == "hedge"
        self._signed(
            "POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "true" if dual else "false"}, futures=True
        )
        try:
            self._signed(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": self._fsym(symbol), "marginType": "CROSSED" if margin_mode == "cross" else "ISOLATED"},
                futures=True,
            )
        except DB.BrokerError as exc:
            if "-4046" not in str(exc) and "No need to change" not in str(exc):
                raise
        self._signed(
            "POST", "/fapi/v1/leverage", {"symbol": self._fsym(symbol), "leverage": int(leverage)}, futures=True
        )

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> DB.OrderReport:
        if contract_type_of(symbol) not in ("linear", "inverse"):
            raise DB.BrokerError("真实永续通道只接受衍生品")
        if self.position_side and contract_type_of(symbol) == "linear":
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
                    "positionSide": self.position_side.upper(),
                },
                futures=True,
            )
            rep = self._freport(body, client_order_id)
            if rep.filled_qty > 0:
                rep.fee, rep.fee_currency = self._ffee(symbol, rep.exchange_order_id)
            return rep
        return super().place_market(symbol, side, qty, client_order_id)


class RealOKXPerpBroker(DB.OKXDemoBroker):
    def __init__(
        self, api_key: str, api_secret: str, passphrase: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.api_key, self.api_secret, self.passphrase = api_key, api_secret, passphrase
        self.base = LIVE_HOSTS["okx"]
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._rules = {}
        self._ct_val = {}
        self.position_side = ""

    def _headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        h = super()._headers(method, path, body)
        h.pop("x-simulated-trading", None)
        return h

    def prepare_perp(
        self, symbol: str, leverage: Decimal, *, margin_mode: str = "isolated", position_mode: str = "one-way"
    ) -> None:
        if contract_type_of(symbol) != "linear":
            raise DB.BrokerError("真实永续通道当前只接受线性合约")
        self._call(
            "POST",
            "/api/v5/account/set-position-mode",
            payload={"posMode": "long_short_mode" if position_mode == "hedge" else "net_mode"},
        )
        self._call(
            "POST",
            "/api/v5/account/set-leverage",
            payload={
                "instId": self._inst(symbol),
                "lever": str(int(leverage)),
                "mgnMode": "cross" if margin_mode == "cross" else "isolated",
            },
        )

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> DB.OrderReport:
        if contract_type_of(symbol) not in ("linear", "inverse"):
            raise DB.BrokerError("真实永续通道只接受衍生品")
        if contract_type_of(symbol) == "linear":
            payload = {
                "instId": self._inst(symbol),
                "tdMode": "cross",
                "side": side,
                "ordType": "market",
                "sz": format((qty / self._ct(symbol)).normalize(), "f"),
                "clOrdId": client_order_id,
            }
            if self.position_side:
                payload["posSide"] = self.position_side
            rows = self._call("POST", "/api/v5/trade/order", payload=payload)
            first = rows[0] if rows else {}
            if first.get("sCode") not in (None, "0"):
                raise DB.BrokerError(f"OKX 拒单：{first.get('sCode')} {first.get('sMsg')}")
            return DB.OrderReport(
                client_order_id, "new", exchange_order_id=str(first.get("ordId", "")), ts=int(time.time() * 1000)
            )
        return super().place_market(symbol, side, qty, client_order_id)


def make_real_perp_broker(
    exchange: str, creds: dict[str, str], transport: httpx.BaseTransport | None = None
) -> DB.DemoBroker:
    if exchange == "binance":
        return RealBinancePerpBroker(creds["api_key"], creds["api_secret"], transport=transport)
    if exchange == "okx":
        return RealOKXPerpBroker(
            creds["api_key"], creds["api_secret"], creds.get("passphrase") or "", transport=transport
        )
    raise AppError(ErrorCode.UNSUPPORTED_CAPABILITY, f"真实永续暂不支持 {exchange}")


broker_factory = make_real_perp_broker

RISK_NOTICE = """\
真实永续使用真实资金。永续有杠杆、资金费、保证金率与强平，损失可能超过最初保证金，
极端行情下也可能穿仓或无法及时平仓。

本通道只允许 Binance/OKX 的 USDT-M 线性永续研究账户；Key 必须仅开交易权限、关闭提现/划转、
绑定 IP 白名单，并经过管理员确认码。平台会在启动前检查期货账户、保证金模式、杠杆、
单笔/总名义、日亏损和交易所止损，任一条件不满足就拒绝启动或自动急停。
"""
RISK_NOTICE_EN = """\
Real perpetual trading uses real money. Perpetuals involve leverage, funding,
maintenance margin and liquidation. Losses can exceed the initial margin, and
extreme markets may make liquidation or risk reduction impossible in time.

This channel only accepts Binance/OKX USDT-M linear perpetual research accounts.
Keys must be trade-only, have withdrawals/transfers disabled and an IP allowlist,
and an admin must confirm a start challenge. The platform checks the futures
account, margin mode, leverage, per-order/total notional, daily loss and
exchange-side stops before every start or new position.
"""


class RealPerpConnectionService(DemoConnectionService):
    def __init__(self, store: Any, secrets: Any, home: Path) -> None:
        self.store = store
        self.secrets = secrets
        self.dir = Path(home) / "realperp-secrets"
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _out(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            **{k: v for k, v in doc.items() if k != "secret_ref"},
            "env_label": "真实永续（Binance/OKX USDT-M）",
            "realistic": True,
            "env_note": "真实资金 · 仅线性 USDT-M",
        }

    @staticmethod
    def environments() -> list[dict[str, Any]]:
        return [{"exchange": ex, "label": "真实永续", "host": LIVE_HOSTS[ex]} for ex in sorted(SUPPORTED)]

    def list(self) -> list[dict[str, Any]]:
        return [self._out(d) for d in sorted(self.store.list(CONNS), key=lambda d: -(d.get("created_at") or 0))]

    def get(self, cid: str) -> dict[str, Any]:
        doc = self.store.get(CONNS, cid)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "真实永续连接不存在")
        return doc

    def _probe_live(self, exchange: str, creds: dict[str, str]) -> dict[str, Any]:
        chk = MI.key_check(exchange, creds)
        if not chk["ok"]:
            raise AppError(
                ErrorCode.PERMISSION_DENIED,
                "API Key 自检没通过：" + "；".join(chk["reasons"]),
                details={"checks": chk["checks"]},
            )
        broker = broker_factory(exchange, creds)
        try:
            bal = broker.balances()
            positions = broker.positions() if hasattr(broker, "positions") else {}
        except DB.BrokerError as exc:
            raise AppError(ErrorCode.PERMISSION_DENIED, f"期货账户自检失败：{exc}") from exc
        except DB.BrokerUnknown as exc:
            raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, f"连不上交易所：{exc}") from exc
        finally:
            broker.close()
        return {
            **chk,
            "balances": {k: str(v) for k, v in bal.items() if v},
            "positions": {k: str(v) for k, v in positions.items()},
            "futures_account": True,
        }

    def connect(self, *, exchange: str, label: str = "", creds: dict[str, str], user: str = "") -> dict[str, Any]:
        if exchange not in SUPPORTED:
            raise AppError(ErrorCode.UNSUPPORTED_CAPABILITY, "真实永续只支持 Binance/OKX")
        need = ["api_key", "api_secret"] + (["passphrase"] if exchange == "okx" else [])
        missing = [k for k in need if not (creds.get(k) or "").strip()]
        if missing:
            raise AppError(ErrorCode.INVALID_PARAMETER, "缺少：" + "、".join(missing))
        creds = {k: creds[k].strip() for k in need}
        test = self._probe_live(exchange, creds)
        key = creds["api_key"]
        doc = {
            "id": f"rp-{uuid.uuid4().hex[:10]}",
            "exchange": exchange,
            "env": "live",
            "label": (label or "真实永续账户")[:60],
            "key_mask": f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "****",
            "status": "active",
            "last_test": test,
            "created_at": _now(),
            "created_by": user,
            "secret_ref": None,
        }
        self.store.upsert(CONNS, doc)
        try:
            doc["secret_ref"] = self._write_secret(creds)
            self.store.upsert(CONNS, doc)
        except Exception:
            self.store.delete(CONNS, doc["id"])
            raise
        return self._out(doc)

    def test(self, cid: str) -> dict[str, Any]:
        doc = self.get(cid)
        try:
            doc["last_test"] = self._probe_live(doc["exchange"], self._read_secret(doc.get("secret_ref") or ""))
            doc["status"] = "active"
        except AppError as exc:
            doc["last_test"] = {"at": _now(), "ok": False, "error": exc.message, **(exc.details or {})}
            doc["status"] = "error" if doc.get("secret_ref") else "revoked"
        self.store.upsert(CONNS, doc)
        return self._out(doc)

    def revoke(self, cid: str) -> dict[str, Any]:
        doc = self.get(cid)
        self._wipe(doc.get("secret_ref"))
        doc.update(secret_ref=None, status="revoked", revoked_at=_now())
        self.store.upsert(CONNS, doc)
        return self._out(doc)

    def broker(self, cid: str) -> tuple[dict[str, Any], DB.DemoBroker]:
        doc = self.get(cid)
        if doc.get("status") == "revoked" or not doc.get("secret_ref"):
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "真实永续连接已撤销")
        return self._out(doc), broker_factory(doc["exchange"], self._read_secret(doc["secret_ref"]))


def connections(services: Any) -> RealPerpConnectionService:
    return RealPerpConnectionService(services.store, services.secrets, services.home)


def config(store: Any) -> dict[str, Any]:
    cur = store.get(CONFIG, "config") or {}
    return {
        "id": "config",
        "enabled": False,
        "killed": False,
        "notice_version": "",
        **cur,
        "hard_ceiling": HARD_CEILING,
    }


def status(store: Any, home: Path | str) -> dict[str, Any]:
    c = config(store)
    blockers: list[str] = []
    if not server_enabled():
        blockers.append(f"服务器未打开真实永续（{SERVER_FLAG}=1）")
    if server_killed(home):
        blockers.append(f"服务器急停文件 {KILL_FILE} 存在")
    if not c["enabled"]:
        blockers.append("工作区未确认真实永续风险")
    elif c.get("notice_version") != NOTICE_VERSION:
        blockers.append("真实永续风险告知版本已更新，需要管理员重新确认")
    if c["killed"]:
        blockers.append("工作区真实永续急停中")
    return {
        **c,
        "server_enabled": server_enabled(),
        "server_killed": server_killed(home),
        "notice": RISK_NOTICE,
        "notice_en": RISK_NOTICE_EN,
        "notice_current": NOTICE_VERSION,
        "ack_phrase": ACK_PHRASE,
        "ack_phrase_en": ACK_PHRASE_EN,
        "start_phrase": START_PHRASE,
        "start_phrase_en": START_PHRASE_EN,
        "ready": not blockers,
        "blockers": blockers,
        "supported_exchanges": sorted(SUPPORTED),
    }


def enable(store: Any, *, user: str, phrase: str, notice_version: str) -> dict[str, Any]:
    if phrase.strip() not in (ACK_PHRASE, ACK_PHRASE_EN):
        raise AppError(ErrorCode.INVALID_PARAMETER, f"请输入「{ACK_PHRASE}」或「{ACK_PHRASE_EN}」确认")
    if notice_version != NOTICE_VERSION:
        raise AppError(ErrorCode.INVALID_PARAMETER, "风险告知版本不匹配")
    c = config(store)
    c.update(enabled=True, notice_version=NOTICE_VERSION, accepted_by=user, accepted_at=_now())
    store.upsert(CONFIG, c)
    return c


def disable(store: Any, *, user: str) -> dict[str, Any]:
    c = config(store)
    c.update(enabled=False, disabled_by=user, disabled_at=_now())
    store.upsert(CONFIG, c)
    return c


def set_killed(store: Any, killed: bool, *, user: str, phrase: str = "") -> dict[str, Any]:
    c = config(store)
    if not killed and phrase.strip() not in (ACK_PHRASE, ACK_PHRASE_EN):
        raise AppError(ErrorCode.INVALID_PARAMETER, f"解除急停请输入「{ACK_PHRASE}」或「{ACK_PHRASE_EN}」")
    c.update(
        killed=killed,
        **({"killed_by": user, "killed_at": _now()} if killed else {"rearmed_by": user, "rearmed_at": _now()}),
    )
    store.upsert(CONFIG, c)
    return c


def blocked(store: Any, home: Path | str) -> str | None:
    if not server_enabled():
        return "服务器未打开真实永续"
    if server_killed(home):
        return "服务器真实永续急停中"
    c = config(store)
    if c.get("killed"):
        return "工作区真实永续急停中"
    if not c.get("enabled"):
        return "工作区未确认真实永续风险"
    if c.get("notice_version") != NOTICE_VERSION:
        return "真实永续风险告知需要重新确认"
    return None


def prepare(store: Any, *, user: str, summary: dict[str, Any]) -> dict[str, Any]:
    code = f"{_secrets.randbelow(1_000_000):06d}"
    ch = {
        "id": f"rpc-{uuid.uuid4().hex[:10]}",
        "user": user,
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
        "summary": summary,
        "created_at": _now(),
        "expires_at": _now() + MI.CHALLENGE_TTL_MS,
        "used": False,
    }
    store.upsert(CHALLENGES, ch)
    return {"challenge_id": ch["id"], "code": code, "expires_at": ch["expires_at"], "summary": summary}


def consume(store: Any, cid: str, *, code: str, phrase: str, user: str) -> dict[str, Any]:
    ch = store.get(CHALLENGES, cid)
    if ch is None or ch.get("used"):
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认码无效或已用过，请重新生成")
    if ch["user"] != user:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只能由生成摘要的人确认")
    if _now() > ch["expires_at"]:
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认码已过期（5 分钟）")
    if phrase.strip() not in (START_PHRASE, START_PHRASE_EN):
        raise AppError(ErrorCode.INVALID_PARAMETER, f"请输入「{START_PHRASE}」或「{START_PHRASE_EN}」")
    if not _secrets.compare_digest(hashlib.sha256(code.strip().encode()).hexdigest(), ch["code_hash"]):
        ch["attempts"] = int(ch.get("attempts") or 0) + 1
        if ch["attempts"] >= 3:
            ch["used"] = True
        store.upsert(CHALLENGES, ch)
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认码不对")
    ch.update(used=True, used_at=_now())
    store.upsert(CHALLENGES, ch)
    return ch["summary"]


class RealPerpOMS(MI.LiveOMS):
    """Perpetual OMS with an account-level exposure cap and emergency close."""

    def __init__(
        self,
        broker: DB.DemoBroker,
        limits: OmsLimits,
        state: OmsState | None = None,
        *,
        cap: float,
        quote: str,
        blocked_fn: Any = None,
        max_leverage: float = 10.0,
        margin_mode: str = "isolated",
        position_mode: str = "one-way",
        **kw: Any,
    ) -> None:
        super().__init__(broker, limits, state, cap=cap, quote=quote, blocked_fn=blocked_fn, **kw)
        self.max_leverage = float(max_leverage)
        self.margin_mode = margin_mode
        self.position_mode = position_mode
        self.emergency_reason = ""
        self.last_prices: dict[str, Decimal] = {}

    def _notional(self, intent: dict[str, Any], price: Decimal | None = None) -> Decimal:
        spec = spec_for(self.broker.exchange, intent["security"])
        mult = Decimal(str(spec.contract_multiplier))
        px = price if price is not None else Decimal(str(intent.get("ref_price") or 0))
        return abs(Decimal(str(intent["qty"]))) * mult * (Decimal(1) if spec.contract_type == "inverse" else px)

    def _pretrade(self, intent: dict[str, Any]) -> str | None:
        sym = intent.get("security", "")
        if contract_type_of(sym) not in ("linear", "inverse"):
            return "真实永续通道拒绝现货标的"
        opens = intent.get("opens", intent.get("side") == "buy")
        if opens:
            b = self.blocked_fn()
            if b:
                return f"{b}，不开新仓"
            if self.state.halted:
                return f"风控熔断中（{self.state.halt_reason}），不开新仓"
            lev = float(intent.get("leverage") or 1)
            if lev > self.max_leverage:
                return f"杠杆 {lev:g} 超过真实永续上限 {self.max_leverage:g}"
            if intent.get("margin_mode") and intent["margin_mode"] != self.margin_mode:
                return "保证金模式与启动摘要不一致"
            if intent.get("position_mode") and intent["position_mode"] != self.position_mode:
                return "持仓模式与启动摘要不一致"
            n = self._notional(intent)
            if n > Decimal(str(self.limits.max_order_notional)) * Decimal("1.001"):
                return f"单笔名义 {n:.2f} 超过上限 {self.limits.max_order_notional:.2f}"
            total = self.total_notional() + n
            if total > Decimal(str(self.cap)) * Decimal("1.001"):
                return f"账户总名义 {total:.2f} 超过上限 {self.cap:.2f}"
        now = self._now()
        self.state.sent_ts = [t for t in self.state.sent_ts if now - t < 60_000]
        if self.limits.max_orders_per_minute and len(self.state.sent_ts) >= self.limits.max_orders_per_minute:
            return "一分钟下单数达到上限"
        return None

    def total_notional(self) -> Decimal:
        try:
            total = Decimal(0)
            for sym, qty in self.broker.positions().items():
                spec = spec_for(self.broker.exchange, sym)
                px = self.last_prices.get(sym, Decimal(0))
                mult = Decimal(str(spec.contract_multiplier))
                total += abs(Decimal(str(qty))) * mult * (Decimal(1) if spec.contract_type == "inverse" else px)
            return total
        except Exception:
            return Decimal(0)

    def submit(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        try:
            self.last_prices[intent["security"]] = Decimal(str(intent.get("ref_price") or 0))
        except Exception:
            pass
        if hasattr(self.broker, "position_side"):
            self.broker.position_side = intent.get("position_side") or ""
        return super().submit(intent)

    def emergency_close(self, reason: str = "") -> list[dict[str, Any]]:
        """Best-effort market close of every exchange position; no new orders accepted."""
        self.emergency_reason = reason
        self.halt(reason or "真实永续急停")
        out: list[dict[str, Any]] = []
        try:
            positions = self.broker.positions()
        except Exception as exc:
            return [{"ok": False, "error": str(exc)}]
        for sym, qty in positions.items():
            if not qty:
                continue
            side = "sell" if Decimal(str(qty)) > 0 else "buy"
            ref = f"remote-close-{uuid.uuid4().hex[:10]}"
            try:
                rep = self.broker.place_market(sym, side, abs(Decimal(str(qty))), ref)
                out.append({"security": sym, "side": side, "qty": str(abs(Decimal(str(qty)))), "status": rep.status})
            except Exception as exc:
                out.append({"security": sym, "side": side, "error": str(exc)})
        return out

    def watchdog(self) -> str | None:
        why = self.blocked_fn()
        if why and not self.state.halted:
            self.halt(why)
            self.emergency_close(why)
            return why
        return None
