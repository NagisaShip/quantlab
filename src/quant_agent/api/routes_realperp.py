"""API for real-money perpetual execution (separate from spot micro-live)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..exchanges.contracts import contract_type_of
from ..live import real_perp as RP
from ..platform import context
from ..platform.errors import AppError, ErrorCode
from ..research import graduation as GR
from .deps import get_services

router = APIRouter(prefix="/api/realperp", tags=["真实永续"])


class EnableIn(BaseModel):
    phrase: str
    notice_version: str


class PhraseIn(BaseModel):
    phrase: str = ""


class ConnectIn(BaseModel):
    exchange: Literal["binance", "okx"]
    label: str = Field("", max_length=60)
    api_key: str = Field(..., min_length=8, max_length=256)
    api_secret: str = Field(..., min_length=8, max_length=256)
    passphrase: str = Field("", max_length=128)


class PrepareIn(BaseModel):
    slug: str
    connection_id: str
    symbol: str = "BTC/USDT:PERP"
    initial_cash: float = Field(..., gt=0, le=RP.HARD_CEILING)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    position_mode: Literal["one-way", "hedge"] = "one-way"
    leverage: float = Field(5, ge=1, le=50)
    stop_loss: float = Field(0.05, ge=0.005, le=0.2)
    max_daily_loss: float = Field(0.03, ge=0.005, le=0.2)
    max_order_notional: float = Field(100, gt=0, le=RP.HARD_CEILING)


class StartIn(BaseModel):
    challenge_id: str
    code: str
    phrase: str


def _ctx():
    return context.current()


def _admin() -> None:
    if not _ctx().is_ws_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员能操作真实永续")


def _sessions() -> list[dict[str, Any]]:
    from .routes_lifecycle import running_sessions

    return [
        s
        for s in running_sessions(get_services(), context.current_workspace())
        if s.get("execution") == "live_realperp"
    ]


@router.get("", summary="真实永续状态、连接和运行中的实验")
def overview() -> dict:
    s = get_services()
    return {
        "status": RP.status(s.store, s.home),
        "connections": RP.connections(s).list(),
        "environments": RP.RealPerpConnectionService.environments(),
        "sessions": [
            {
                k: x.get(k)
                for k in ("id", "strategy_name", "strategy_id", "status", "run_state", "initial_cash", "equity_points")
            }
            for x in _sessions()
        ],
    }


@router.post("/enable", summary="确认真实永续风险并打开工作区开关")
def enable(payload: EnableIn) -> dict:
    _admin()
    s = get_services()
    RP.enable(s.store, user=_ctx().user_id or "", phrase=payload.phrase, notice_version=payload.notice_version)
    s.audit.record(
        "realperp.enable",
        object_type="workspace",
        object_id=context.current_workspace(),
        details={"notice": RP.NOTICE_VERSION},
    )
    return {"status": RP.status(s.store, s.home)}


@router.post("/disable", summary="关闭真实永续工作区开关")
def disable() -> dict:
    _admin()
    s = get_services()
    RP.disable(s.store, user=_ctx().user_id or "")
    s.audit.record("realperp.disable", object_type="workspace", object_id=context.current_workspace())
    return {"status": RP.status(s.store, s.home)}


@router.post("/kill", summary="一键平掉所有真实永续持仓并急停")
def kill() -> dict:
    if not _ctx().can_write:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只读成员不能操作")
    from .routes_studio import liquidate_paper

    s = get_services()
    RP.set_killed(s.store, True, user=_ctx().user_id or "")
    stopped, failed = [], []
    # First ask remote OMS sessions to close; then the normal paper liquidation
    # path is retained as a second, local reconciliation action.
    for x in _sessions():
        try:
            liquidate_paper(x["id"])
            stopped.append(x["id"])
        except Exception as exc:
            failed.append({"id": x["id"], "error": str(exc)[:200]})
    s.audit.record(
        "realperp.kill",
        object_type="workspace",
        object_id=context.current_workspace(),
        details={"stopped": stopped, "failed": failed},
    )
    return {"stopped": stopped, "failed": failed, "status": RP.status(s.store, s.home)}


@router.post("/rearm", summary="解除真实永续急停")
def rearm(payload: PhraseIn) -> dict:
    _admin()
    s = get_services()
    RP.set_killed(s.store, False, user=_ctx().user_id or "", phrase=payload.phrase)
    s.audit.record("realperp.rearm", object_type="workspace", object_id=context.current_workspace())
    return {"status": RP.status(s.store, s.home)}


@router.post("/connections", summary="接入真实永续专用 Key")
def connect(payload: ConnectIn) -> dict:
    _admin()
    s = get_services()
    conn = RP.connections(s).connect(
        exchange=payload.exchange,
        label=payload.label,
        user=_ctx().user_id or "",
        creds={"api_key": payload.api_key, "api_secret": payload.api_secret, "passphrase": payload.passphrase},
    )
    s.audit.record(
        "realperp.connect",
        object_type="realperp_connection",
        object_id=conn["id"],
        details={"exchange": conn["exchange"], "key": conn["key_mask"]},
    )
    return {"connection": conn}


@router.post("/connections/{cid}/test", summary="重新检查真实永续 Key 与期货账户")
def test_connection(cid: str) -> dict:
    _admin()
    return {"connection": RP.connections(get_services()).test(cid)}


@router.delete("/connections/{cid}", summary="擦除真实永续 Key")
def revoke(cid: str) -> dict:
    _admin()
    s = get_services()
    conn = RP.connections(s).revoke(cid)
    s.audit.record("realperp.revoke", object_type="realperp_connection", object_id=cid)
    return {"connection": conn}


def _preflight(p: PrepareIn) -> dict[str, Any]:
    from .routes_research import _record_or_404

    s = get_services()
    st = RP.status(s.store, s.home)
    if not st["ready"]:
        raise AppError(ErrorCode.PERMISSION_DENIED, "真实永续不能启动：" + "；".join(st["blockers"]))
    rec = _record_or_404(p.slug)
    if (GR.record(s.store, p.slug) or {}).get("stage") != "graduated":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有已毕业策略能进入真实永续")
    if not rec.meta.human_approved:
        raise AppError(ErrorCode.PERMISSION_DENIED, "当前策略版本尚未人工批准")
    sym = p.symbol.strip().upper()
    if contract_type_of(sym) != "linear":
        raise AppError(ErrorCode.INVALID_PARAMETER, "真实永续 v1 只接受线性 USDT-M 合约，例如 BTC/USDT:PERP")
    if p.initial_cash > RP.HARD_CEILING or p.max_order_notional > p.initial_cash:
        raise AppError(ErrorCode.INVALID_PARAMETER, "初始资金或单笔名义超过真实永续上限")
    if any(x.get("strategy_id") == p.slug for x in _sessions()):
        raise AppError(ErrorCode.CONFLICT, "这个策略已有真实永续实验在跑")
    svc = RP.connections(s)
    conn = svc._out(svc.get(p.connection_id))
    if conn.get("status") != "active":
        raise AppError(ErrorCode.PERMISSION_DENIED, "真实永续连接状态不正常")
    fresh = svc.test(p.connection_id)
    if fresh.get("status") != "active":
        raise AppError(ErrorCode.PERMISSION_DENIED, "启动前 Key/期货账户自检失败")
    quote = "USDT"
    have = float(((fresh.get("last_test") or {}).get("balances") or {}).get(quote) or 0)
    if have < p.initial_cash:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"期货钱包 USDT 余额 {have:.2f}，不够本次 {p.initial_cash:.2f}")
    return {
        "slug": p.slug,
        "name": rec.meta.name,
        "version": rec.meta.version,
        "timeframe": rec.meta.timeframe,
        "symbol": sym,
        "exchange": conn["exchange"],
        "connection_id": conn["id"],
        "key_mask": conn["key_mask"],
        "initial_cash": p.initial_cash,
        "margin_mode": p.margin_mode,
        "position_mode": p.position_mode,
        "leverage": p.leverage,
        "stop_loss": p.stop_loss,
        "max_daily_loss": p.max_daily_loss,
        "max_order_notional": p.max_order_notional,
        "quote": quote,
        "balance": have,
    }


@router.post("/prepare", summary="生成真实永续启动摘要与确认码")
def prepare(payload: PrepareIn) -> dict:
    _admin()
    return RP.prepare(get_services().store, user=_ctx().user_id or "", summary=_preflight(payload))


@router.post("/start", summary="输入确认码启动真实永续")
def start(payload: StartIn) -> dict:
    _admin()
    from ..platform import features
    from .routes_studio import start_session
    from .schemas import PaperStartIn, RiskIn

    features.require("execution.real_perp")
    s = get_services()
    summ = RP.consume(
        s.store, payload.challenge_id, code=payload.code, phrase=payload.phrase, user=_ctx().user_id or ""
    )
    again = _preflight(
        PrepareIn(
            **{
                k: summ[k]
                for k in (
                    "slug",
                    "connection_id",
                    "symbol",
                    "initial_cash",
                    "margin_mode",
                    "position_mode",
                    "leverage",
                    "stop_loss",
                    "max_daily_loss",
                    "max_order_notional",
                )
            }
        )
    )
    if again["version"] != summ["version"]:
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认后策略版本发生变化，请重新生成摘要")
    conn = RP.connections(s)._out(RP.connections(s).get(summ["connection_id"]))
    body = PaperStartIn(
        slug=summ["slug"],
        securities=[summ["symbol"]],
        initial_cash=summ["initial_cash"],
        initial_currency="USDT",
        source="exchange",
        exchange=summ["exchange"],
        execution="paper",
        risk=RiskIn(stop_loss=summ["stop_loss"]),
        max_order_notional=summ["max_order_notional"],
        max_orders_per_minute=6,
        max_daily_loss=summ["max_daily_loss"],
        margin_mode=summ["margin_mode"],
        position_mode=summ["position_mode"],
        contract_type="linear",
        ledger_v2=True,
    )
    live = {
        "connection": conn,
        "micro": {"stop_loss": summ["stop_loss"]},
        "real_perp": {
            "cap": summ["initial_cash"],
            "max_order_notional": summ["max_order_notional"],
            "max_leverage": summ["leverage"],
            "margin_mode": summ["margin_mode"],
            "position_mode": summ["position_mode"],
            "max_daily_loss": summ["max_daily_loss"],
            "confirmed_by": _ctx().user_id,
            "challenge_id": payload.challenge_id,
        },
    }
    out = start_session(body, live=live)
    s.audit.record("realperp.start", object_type="run", object_id=out["run_id"], details=summ)
    return out


@router.get("/notice", summary="真实永续风险告知")
def notice() -> dict:
    return {
        "notice": RP.RISK_NOTICE,
        "notice_en": RP.RISK_NOTICE_EN,
        "version": RP.NOTICE_VERSION,
        "ack_phrase": RP.ACK_PHRASE,
        "ack_phrase_en": RP.ACK_PHRASE_EN,
        "start_phrase": RP.START_PHRASE,
        "start_phrase_en": RP.START_PHRASE_EN,
    }
