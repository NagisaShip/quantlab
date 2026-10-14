"""微实盘：用真实资金、很小的金额跑已经毕业的策略。

层层把关（任一层不过就不会向交易所实盘下单）：

1. 服务器开关：环境变量 ``QAGENT_MICROLIVE=1`` 才允许。默认关闭，部署的人显式打开。
2. 工作区开关：管理员读完风险告知、输入「我已了解实盘风险」后打开；告知内容改版后要重新确认。
3. 一键停止：工作区级的急停。按下后所有微实盘实验「平仓并停止」，并拒绝新的启动和新的开仓，
   直到管理员重新解除。服务器上放一个 ``microlive.KILL`` 文件也能全局急停（不用登录）。
4. 策略：只有晋级关卡里「毕业」的策略能上；只做现货多头，不开杠杆、不做永续。
5. 资金上限：每个策略从最低一档开始（默认 100 / 300 / 1000 USDT），在当前一档跑满规定天数、
   没有衰减、回撤不超限，才可以由人确认升一档。上限是硬约束：下单前按交易所实际余额核对
   「本实验已经花掉的计价币 + 这一笔」不超过上限，和策略自己的账本无关。
6. 交易所止损必选：每个持仓都要在交易所挂保护止损（策略没设止损时用启动时填的默认止损）。
   交易所不接受止损单时立刻暂停开新仓，不允许裸仓。
7. API Key 自检：必须能交易、不能提现 / 划转、必须绑定 IP 白名单，三项都满足才保存；每次启动前再查一遍。
8. 每次启动都要人确认：先生成一份启动摘要和 6 位确认码（5 分钟有效），输入确认码才真正启动。

只支持 Binance、OKX 现货（这两家的 Key 权限能可靠查到）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as _secrets
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from ..platform.errors import AppError, ErrorCode
from . import demo_broker as DB
from .demo_accounts import DemoConnectionService
from .oms import DemoOMS, OmsLimits, OmsState

SERVER_FLAG = "QAGENT_MICROLIVE"
KILL_FILE = "microlive.KILL"
LIVE_HOSTS = {"binance": "https://api.binance.com", "okx": "https://www.okx.com"}
EXCHANGE_LABEL = {"binance": "Binance 现货（实盘）", "okx": "OKX 现货（实盘）"}
HARD_CEILING = 10_000.0  # 任何设置都不能超过的单策略上限（计价币）
DEFAULT_TIERS = [100.0, 300.0, 1000.0]
TIER_DAYS = 14
TIER_MAX_DD = 0.10
CHALLENGE_TTL_MS = 5 * 60_000
ACK_PHRASE = "我已了解实盘风险"
START_PHRASE = "确认实盘"

CONFIG = "microlive_config"
TIERS = "microlive_tiers"
CONNS = "microlive_connections"
CHALLENGES = "microlive_challenges"

RISK_NOTICE = """\
**微实盘使用真实资金，可能全部亏损。**

1. 平台是工具，不构成投资建议。回测、纸面模拟和模拟盘的结果都不代表未来收益；策略可能因为行情变化、交易所故障、网络中断、滑点、手续费或程序错误而亏损。
2. 你需要自己确认所在地的法律法规允许你使用这家交易所、交易这些币种，并承担相应的税务责任。
3. API Key 只能开「交易」权限，必须关掉提现和划转，并绑定 IP 白名单。平台会检查这三项，但无法替你保管交易所账户。
4. 资金上限、交易所止损和一键停止能限制损失，但不能保证：极端行情下止损单可能以远差于触发价的价格成交，交易所也可能拒单或宕机。
5. 平台不会替你做任何超出你确认范围的事：不会自动升档，不会自动加资金，不会在你未确认时启动。
6. 你可以随时一键停止：平掉微实盘的全部持仓并停止，之后需要管理员重新解除才能再启动。
"""  # noqa: E501 风险告知原文，不折行
NOTICE_VERSION = hashlib.sha256(RISK_NOTICE.encode()).hexdigest()[:12]


def _now() -> int:
    return int(time.time() * 1000)


def server_enabled() -> bool:
    return os.environ.get(SERVER_FLAG, "0") == "1"


def server_killed(home: Path | str) -> bool:
    return (Path(home) / KILL_FILE).exists()


# ---------------------------------------------------------------------- 实盘下单客户端（只现货）
class _SpotOnly:
    def prepare_perp(self, symbol: str, leverage: Decimal) -> None:
        raise DB.BrokerError("微实盘只做现货，不开杠杆、不做永续")

    def _no_perp(self, symbol: str) -> None:
        if DB.is_perp(symbol):
            raise DB.BrokerError("微实盘只做现货，不做永续")


class LiveBinanceBroker(_SpotOnly, DB.BinanceDemoBroker):
    def __init__(self, api_key: str, api_secret: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.env = "live"
        self.api_key, self.api_secret = api_key, api_secret
        self.base = LIVE_HOSTS["binance"]
        self.futures_base = ""
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._f = None
        self._rules: dict[str, DB.Rules] = {}

    @property
    def label(self) -> str:
        return EXCHANGE_LABEL["binance"]

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> DB.OrderReport:
        self._no_perp(symbol)
        return super().place_market(symbol, side, qty, client_order_id)

    def place_stop(
        self, symbol: str, side: str, qty: Decimal, trigger: Decimal, client_order_id: str
    ) -> DB.OrderReport:
        self._no_perp(symbol)
        return super().place_stop(symbol, side, qty, trigger, client_order_id)

    def close(self) -> None:
        self._c.close()


class LiveOKXBroker(_SpotOnly, DB.OKXDemoBroker):
    def __init__(
        self, api_key: str, api_secret: str, passphrase: str, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.env = "live"
        self.api_key, self.api_secret, self.passphrase = api_key, api_secret, passphrase
        self.base = LIVE_HOSTS["okx"]
        self._c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._rules: dict[str, DB.Rules] = {}
        self._ct_val: dict[str, Decimal] = {}

    @property
    def label(self) -> str:
        return EXCHANGE_LABEL["okx"]

    def _headers(self, method: str, path: str, body: str = "") -> dict[str, str]:
        h = super()._headers(method, path, body)
        h.pop("x-simulated-trading", None)  # 实盘：不带模拟盘请求头
        return h

    def place_market(self, symbol: str, side: str, qty: Decimal, client_order_id: str) -> DB.OrderReport:
        self._no_perp(symbol)
        return super().place_market(symbol, side, qty, client_order_id)

    def place_stop(
        self, symbol: str, side: str, qty: Decimal, trigger: Decimal, client_order_id: str
    ) -> DB.OrderReport:
        self._no_perp(symbol)
        return super().place_stop(symbol, side, qty, trigger, client_order_id)


def make_live_broker(
    exchange: str, creds: dict[str, str], transport: httpx.BaseTransport | None = None
) -> DB.DemoBroker:
    if exchange == "binance":
        return LiveBinanceBroker(creds["api_key"], creds["api_secret"], transport=transport)
    if exchange == "okx":
        return LiveOKXBroker(creds["api_key"], creds["api_secret"], creds.get("passphrase") or "", transport=transport)
    raise AppError(ErrorCode.UNSUPPORTED_CAPABILITY, f"微实盘暂不支持 {exchange}（只支持 Binance、OKX 现货）")


#: 测试可替换
broker_factory = make_live_broker


def _default_permissions(exchange: str, creds: dict[str, str]) -> dict[str, Any]:
    from .accounts import default_client_factory

    rep = default_client_factory(exchange, creds, "live").permissions()
    return {
        "can_trade": rep.can_trade,
        "can_withdraw": rep.can_withdraw,
        "ip_restricted": rep.ip_restricted,
        "account_id": rep.account_id,
    }


#: 测试可替换：permission_probe(exchange, creds) -> {can_trade, can_withdraw, ip_restricted, account_id}
permission_probe = _default_permissions


def key_check(exchange: str, creds: dict[str, str]) -> dict[str, Any]:
    """API Key 三项自检：能交易、不能提现 / 划转、绑定了 IP 白名单。"""
    p = permission_probe(exchange, creds)
    checks = [
        {
            "name": "交易权限",
            "ok": bool(p.get("can_trade")),
            "detail": "已开启" if p.get("can_trade") else "没开交易权限，没法下单",
        },
        {
            "name": "提现 / 划转",
            "ok": not p.get("can_withdraw"),
            "detail": "已关闭"
            if not p.get("can_withdraw")
            else "开着提现或划转权限：Key 泄露时资金可能被转走，请关掉后重试",
        },
        {
            "name": "IP 白名单",
            "ok": p.get("ip_restricted") is True,
            "detail": "已绑定" if p.get("ip_restricted") else "没绑定 IP 白名单：请在交易所后台限定为运行节点的出口 IP",
        },
    ]
    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "account_id": p.get("account_id") or "",
        "reasons": [c["detail"] for c in checks if not c["ok"]],
        "at": _now(),
    }


class LiveConnectionService(DemoConnectionService):
    """实盘 Key：和模拟盘 Key 分开存（集合与密钥目录都不同），保存前必须过三项自检。"""

    def __init__(self, store: Any, secrets: Any, home: Path) -> None:
        self.store = store
        self.secrets = secrets
        self.dir = Path(home) / "microlive-secrets"
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _out(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            **{k: v for k, v in doc.items() if k != "secret_ref"},
            "env_label": EXCHANGE_LABEL.get(doc.get("exchange", ""), ""),
            "realistic": True,
            "env_note": "真实资金",
        }

    @staticmethod
    def environments() -> list[dict[str, Any]]:
        return [{"exchange": ex, "label": EXCHANGE_LABEL[ex], "host": host} for ex, host in LIVE_HOSTS.items()]

    def list(self) -> list[dict[str, Any]]:
        return [self._out(d) for d in sorted(self.store.list(CONNS), key=lambda d: -(d.get("created_at") or 0))]

    def get(self, cid: str) -> dict[str, Any]:
        doc = self.store.get(CONNS, cid)
        if doc is None:
            raise AppError(ErrorCode.NOT_FOUND, "实盘连接不存在")
        return doc

    def _probe_live(self, exchange: str, creds: dict[str, str]) -> dict[str, Any]:
        chk = key_check(exchange, creds)
        if not chk["ok"]:
            raise AppError(
                ErrorCode.PERMISSION_DENIED,
                "API Key 自检没通过，未保存：" + "；".join(chk["reasons"]),
                details={"checks": chk["checks"]},
            )
        broker = broker_factory(exchange, creds)
        try:
            bal = broker.balances()
        except DB.BrokerError as exc:
            raise AppError(ErrorCode.PERMISSION_DENIED, f"查余额失败：{exc}") from exc
        except DB.BrokerUnknown as exc:
            raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, f"连不上交易所：{exc}") from exc
        finally:
            broker.close()
        top = sorted(((k, v) for k, v in bal.items() if v > 0), key=lambda kv: -float(kv[1]))[:8]
        return {**chk, "balances": {k: str(v) for k, v in top}}

    def connect(
        self, *, exchange: str, env: str = "live", label: str = "", creds: dict[str, str], user: str = ""
    ) -> dict[str, Any]:
        if exchange not in LIVE_HOSTS:
            raise AppError(ErrorCode.UNSUPPORTED_CAPABILITY, "微实盘只支持 Binance、OKX 现货")
        need = ["api_key", "api_secret"] + (["passphrase"] if exchange == "okx" else [])
        missing = [k for k in need if not (creds.get(k) or "").strip()]
        if missing:
            raise AppError(ErrorCode.INVALID_PARAMETER, "缺少：" + "、".join(missing))
        creds = {k: creds[k].strip() for k in need}
        test = self._probe_live(exchange, creds)
        key = creds["api_key"]
        doc = {
            "id": f"ml-{uuid.uuid4().hex[:10]}",
            "exchange": exchange,
            "env": "live",
            "label": (label or EXCHANGE_LABEL[exchange])[:60],
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
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "实盘连接已撤销")
        return self._out(doc), broker_factory(doc["exchange"], self._read_secret(doc["secret_ref"]))


def connections(services: Any) -> LiveConnectionService:
    return LiveConnectionService(services.store, services.secrets, services.home)


# ---------------------------------------------------------------------- 工作区开关与急停
def config(store: Any) -> dict[str, Any]:
    cur = store.get(CONFIG, "config") or {}
    tiers = [min(float(x), HARD_CEILING) for x in (cur.get("tiers") or DEFAULT_TIERS)]
    return {
        "id": "config",
        "enabled": False,
        "killed": False,
        "notice_version": "",
        **cur,
        "tiers": tiers,
        "tier_days": int(cur.get("tier_days") or TIER_DAYS),
    }


def status(store: Any, home: Path | str) -> dict[str, Any]:
    c = config(store)
    current = c.get("notice_version") == NOTICE_VERSION
    blockers = []
    if not server_enabled():
        blockers.append(f"服务器没有打开微实盘（部署时设置环境变量 {SERVER_FLAG}=1）")
    if server_killed(home):
        blockers.append(f"服务器上有急停文件 {KILL_FILE}：删掉它才能恢复")
    if not c["enabled"]:
        blockers.append("工作区还没打开微实盘：管理员读完风险告知后打开")
    elif not current:
        blockers.append("风险告知更新过，需要管理员重新确认")
    if c["killed"]:
        blockers.append("已一键停止：管理员解除后才能再启动")
    return {
        **c,
        "server_enabled": server_enabled(),
        "server_killed": server_killed(home),
        "notice": RISK_NOTICE,
        "notice_current": NOTICE_VERSION,
        "ack_phrase": ACK_PHRASE,
        "start_phrase": START_PHRASE,
        "hard_ceiling": HARD_CEILING,
        "ready": not blockers,
        "blockers": blockers,
    }


def enable(
    store: Any, *, user: str, phrase: str, notice_version: str, tiers: list[float] | None = None
) -> dict[str, Any]:
    if phrase.strip() != ACK_PHRASE:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"请输入「{ACK_PHRASE}」确认")
    if notice_version != NOTICE_VERSION:
        raise AppError(ErrorCode.INVALID_PARAMETER, "风险告知已经更新，请刷新后重新阅读")
    c = config(store)
    t = sorted(min(max(float(x), 10.0), HARD_CEILING) for x in (tiers or c["tiers"]))[:6]
    c.update(enabled=True, notice_version=NOTICE_VERSION, accepted_by=user, accepted_at=_now(), tiers=t)
    store.upsert(CONFIG, c)
    return c


def disable(store: Any, *, user: str) -> dict[str, Any]:
    c = config(store)
    c.update(enabled=False, disabled_by=user, disabled_at=_now())
    store.upsert(CONFIG, c)
    return c


def set_killed(store: Any, killed: bool, *, user: str, phrase: str = "") -> dict[str, Any]:
    c = config(store)
    if not killed and phrase.strip() != ACK_PHRASE:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"解除急停请输入「{ACK_PHRASE}」")
    c.update(
        killed=killed,
        **({"killed_by": user, "killed_at": _now()} if killed else {"rearmed_by": user, "rearmed_at": _now()}),
    )
    store.upsert(CONFIG, c)
    return c


def blocked(store: Any, home: Path | str) -> str | None:
    """worker 每笔开仓前问一次：返回不允许开仓的原因。"""
    if not server_enabled():
        return "服务器已关闭微实盘"
    if server_killed(home):
        return "服务器急停中"
    c = config(store)
    if c["killed"]:
        return "已一键停止"
    if not c["enabled"]:
        return "工作区已关闭微实盘"
    return None


# ---------------------------------------------------------------------- 资金档位
def tier(store: Any, slug: str) -> dict[str, Any]:
    c = config(store)
    rec = store.get(TIERS, slug) or {"id": slug, "slug": slug, "index": 0, "history": []}
    idx = max(0, min(int(rec.get("index") or 0), len(c["tiers"]) - 1))
    return {
        **rec,
        "index": idx,
        "cap": min(c["tiers"][idx], HARD_CEILING),
        "tiers": c["tiers"],
        "next_cap": c["tiers"][idx + 1] if idx + 1 < len(c["tiers"]) else None,
    }


def tier_check(store: Any, slug: str) -> dict[str, Any]:
    """能不能升一档：在当前档跑满规定天数、没有衰减、回撤不超限、没有未解除的熔断。"""
    from ..research import decay as D
    from ..store import Collections

    t = tier(store, slug)
    c = config(store)
    if t["next_cap"] is None:
        return {**t, "can_raise": False, "checks": [{"label": "已经是最高一档", "ok": False}]}
    since = int(t.get("since") or 0)
    sessions = [
        s
        for s in store.list(Collections.PAPER_SESSIONS)
        if s.get("strategy_id") == slug and s.get("execution") == "live" and (s.get("started_at") or 0) >= since
    ]
    days = max([((s.get("last_update") or 0) - (s.get("started_at") or 0)) / D.DAY for s in sessions], default=0.0)
    dd = max([D.live_stats(s.get("equity_points") or [], [])["max_drawdown"] or 0.0 for s in sessions], default=0.0)
    decays = [
        r
        for r in store.list(D.REPORTS)
        if r.get("strategy_id") == slug and r.get("execution") == "live" and r.get("status") == "decay"
    ]
    halted = any((s.get("demo") or {}).get("halted") for s in sessions)
    checks = [
        {"label": f"在当前一档实际运行满 {c['tier_days']} 天", "ok": days >= c["tier_days"], "value": f"{days:.1f} 天"},
        {
            "label": f"最大回撤不超过 {TIER_MAX_DD:.0%}",
            "ok": bool(sessions) and dd <= TIER_MAX_DD,
            "value": f"{dd:.1%}",
        },
        {"label": "衰减监测没有报警", "ok": not decays, "value": "正常" if not decays else "衰减"},
        {"label": "没有未解除的熔断", "ok": not halted, "value": "熔断中" if halted else "正常"},
    ]
    return {**t, "can_raise": all(x["ok"] for x in checks), "checks": checks}


def raise_tier(store: Any, slug: str, *, user: str) -> dict[str, Any]:
    chk = tier_check(store, slug)
    if not chk["can_raise"]:
        raise AppError(
            ErrorCode.INVALID_PARAMETER, "还不能升档：" + "；".join(x["label"] for x in chk["checks"] if not x["ok"])
        )
    rec = store.get(TIERS, slug) or {"id": slug, "slug": slug, "index": 0, "history": []}
    rec["history"] = [
        *(rec.get("history") or []),
        {"from": chk["cap"], "to": chk["next_cap"], "at": _now(), "by": user},
    ]
    rec.update(index=chk["index"] + 1, since=_now())
    store.upsert(TIERS, rec)
    return tier(store, slug)


def lower_tier(store: Any, slug: str, *, user: str, note: str = "") -> dict[str, Any]:
    rec = store.get(TIERS, slug) or {"id": slug, "slug": slug, "index": 0, "history": []}
    if int(rec.get("index") or 0) > 0:
        rec["history"] = [*(rec.get("history") or []), {"down": True, "at": _now(), "by": user, "note": note}]
        rec.update(index=int(rec["index"]) - 1, since=_now())
        store.upsert(TIERS, rec)
    return tier(store, slug)


# ---------------------------------------------------------------------- 启动确认码
def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def prepare(store: Any, *, user: str, summary: dict[str, Any]) -> dict[str, Any]:
    code = f"{_secrets.randbelow(1_000_000):06d}"
    ch = {
        "id": f"mc-{uuid.uuid4().hex[:10]}",
        "user": user,
        "code_hash": _code_hash(code),
        "summary": summary,
        "created_at": _now(),
        "expires_at": _now() + CHALLENGE_TTL_MS,
        "used": False,
    }
    store.upsert(CHALLENGES, ch)
    return {"challenge_id": ch["id"], "code": code, "expires_at": ch["expires_at"], "summary": summary}


def consume(store: Any, cid: str, *, code: str, phrase: str, user: str) -> dict[str, Any]:
    ch = store.get(CHALLENGES, cid)
    if ch is None or ch.get("used"):
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认码无效或已用过，请重新生成启动摘要")
    if ch["user"] != user:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只能由生成启动摘要的人确认")
    if _now() > ch["expires_at"]:
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认码已过期（5 分钟），请重新生成")
    if phrase.strip() != START_PHRASE:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"请输入「{START_PHRASE}」")
    if not _secrets.compare_digest(_code_hash(code.strip()), ch["code_hash"]):
        ch["attempts"] = int(ch.get("attempts") or 0) + 1
        if ch["attempts"] >= 3:
            ch["used"] = True
        store.upsert(CHALLENGES, ch)
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认码不对" + ("，已作废，请重新生成" if ch.get("used") else ""))
    ch.update(used=True, used_at=_now())
    store.upsert(CHALLENGES, ch)
    return ch["summary"]


# ---------------------------------------------------------------------- 实盘 OMS
class LiveOMS(DemoOMS):
    """在模拟盘 OMS 上多三道：急停、按交易所余额核对的资金上限、止损单挂不上就停止开仓。"""

    def __init__(
        self,
        broker: DB.DemoBroker,
        limits: OmsLimits,
        state: OmsState | None = None,
        *,
        cap: float,
        quote: str,
        blocked_fn: Any = None,
        **kw: Any,
    ) -> None:
        super().__init__(broker, limits, state, **kw)
        self.cap = float(cap)
        self.quote = quote
        self.blocked_fn = blocked_fn or (lambda: None)

    def spent(self) -> float:
        """本实验从启动到现在花掉的计价币（按交易所实际余额，不看策略账本）。"""
        base = float(self.state.baseline.get(self.quote) or 0)
        now = float(self.broker.balances().get(self.quote) or 0)
        return base - now

    def _pretrade(self, intent: dict[str, Any]) -> str | None:
        why = super()._pretrade(intent)
        if why:
            return why
        if intent.get("perp"):
            return "微实盘只做现货"
        opens = intent.get("opens", intent["side"] == "buy")
        if not opens:
            return None  # 减仓永远允许：急停时也要能平仓
        b = self.blocked_fn()
        if b:
            return f"{b}，不开新仓"
        notional = float(Decimal(intent["qty"]) * Decimal(intent["ref_price"]))
        try:
            spent = self.spent()
        except Exception as exc:
            return f"查交易所余额失败（{exc}），为保证资金上限，这一笔不下"
        if spent + notional > self.cap * 1.001:
            return f"资金上限 {self.cap:.2f} {self.quote}：已用 {max(0.0, spent):.2f}，这一笔 {notional:.2f} 会超出"
        return None

    def _protect_record(
        self, p: dict[str, Any], status: str, message: str = "", rep: DB.OrderReport | None = None
    ) -> None:
        super()._protect_record(p, status, message, rep)
        if status == "rejected" and not self.state.halted:
            self.halt(f"交易所没接受止损单（{message}）。微实盘不允许裸仓：已暂停开新仓，请检查后手动解除")


def manifest_hash(m: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(m, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
