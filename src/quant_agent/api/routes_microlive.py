"""微实盘 API：开关与风险告知、实盘 Key（三项自检）、资金档位、启动摘要 + 确认码、一键停止。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..live import micro as MI
from ..platform import context
from ..platform.errors import AppError, ErrorCode
from ..research import graduation as GR
from .deps import get_services

router = APIRouter(prefix="/api/microlive", tags=["微实盘"])


class EnableIn(BaseModel):
    phrase: str
    notice_version: str
    tiers: list[float] | None = None


class PhraseIn(BaseModel):
    phrase: str = ""


class ConnectIn(BaseModel):
    exchange: str
    label: str = Field("", max_length=60)
    api_key: str = Field(..., min_length=8, max_length=256)
    api_secret: str = Field(..., min_length=8, max_length=256)
    passphrase: str = Field("", max_length=128)


class PrepareIn(BaseModel):
    slug: str
    connection_id: str
    symbol: str = "BTC/USDT"
    initial_cash: float = Field(..., gt=0)
    stop_loss: float = Field(0.05, ge=0.005, le=0.2)
    max_daily_loss: float = Field(0.05, ge=0.01, le=0.2)


class StartIn(BaseModel):
    challenge_id: str
    code: str
    phrase: str


def _ctx():
    return context.current()


def _admin() -> None:
    if not _ctx().is_ws_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员能操作微实盘")


def _store():
    return get_services().store


def _sessions() -> list[dict[str, Any]]:
    from .routes_lifecycle import running_sessions

    return [s for s in running_sessions(get_services(), context.current_workspace()) if s.get("execution") == "live"]


@router.get("", summary="微实盘状态：服务器与工作区开关、风险告知、急停、资金档位、运行中的实验")
def overview() -> dict:
    s = get_services()
    st = MI.status(s.store, s.home)
    rows = []
    for slug, name in sorted({(x.meta.slug, x.meta.name) for x in _graduated()}):
        rows.append({"slug": slug, "name": name, **MI.tier_check(s.store, slug)})
    return {
        "status": st,
        "connections": MI.connections(s).list(),
        "exchanges": MI.LiveConnectionService.environments(),
        "strategies": rows,
        "sessions": [
            {
                k: x.get(k)
                for k in (
                    "id",
                    "strategy_name",
                    "strategy_id",
                    "status",
                    "run_state",
                    "initial_cash",
                    "demo",
                    "equity_points",
                )
            }
            for x in _sessions()
        ],
    }


def _graduated() -> list[Any]:
    from ..strategies.repo import StrategyRepository

    repo = StrategyRepository(get_services().store.root / "strategies")
    return [
        rec for rec in repo.list() if (GR.record(get_services().store, rec.meta.slug) or {}).get("stage") == "graduated"
    ]


@router.post("/enable", summary="打开微实盘（管理员读完风险告知后确认）")
def enable(payload: EnableIn) -> dict:
    _admin()
    s = get_services()
    c = MI.enable(
        s.store,
        user=_ctx().user_id or "",
        phrase=payload.phrase,
        notice_version=payload.notice_version,
        tiers=payload.tiers,
    )
    s.audit.record(
        "microlive.enable",
        object_type="workspace",
        object_id=context.current_workspace(),
        details={"notice": MI.NOTICE_VERSION, "tiers": c["tiers"]},
    )
    return {"status": MI.status(s.store, s.home)}


@router.post("/disable", summary="关闭微实盘（不影响已在运行的实验，要停它们请用一键停止）")
def disable() -> dict:
    _admin()
    s = get_services()
    MI.disable(s.store, user=_ctx().user_id or "")
    s.audit.record("microlive.disable", object_type="workspace", object_id=context.current_workspace())
    return {"status": MI.status(s.store, s.home)}


@router.post("/kill", summary="一键停止：所有微实盘实验平仓并停止，之后拒绝新的启动")
def kill() -> dict:
    if not _ctx().can_write:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只读成员不能操作")
    from .routes_studio import liquidate_paper

    s = get_services()
    MI.set_killed(s.store, True, user=_ctx().user_id or "")
    done, failed = [], []
    for x in _sessions():
        try:
            liquidate_paper(x["id"])
            done.append(x["id"])
        except Exception as exc:
            failed.append({"id": x["id"], "error": str(exc)[:200]})
    s.audit.record(
        "microlive.kill",
        object_type="workspace",
        object_id=context.current_workspace(),
        details={"stopped": done, "failed": failed},
    )
    return {"stopped": done, "failed": failed, "status": MI.status(s.store, s.home)}


@router.post("/rearm", summary="解除急停（管理员）")
def rearm(payload: PhraseIn) -> dict:
    _admin()
    s = get_services()
    MI.set_killed(s.store, False, user=_ctx().user_id or "", phrase=payload.phrase)
    s.audit.record("microlive.rearm", object_type="workspace", object_id=context.current_workspace())
    return {"status": MI.status(s.store, s.home)}


# ---------------------------------------------------------------- Key
@router.post("/connections", summary="接入实盘 Key：必须能交易、不能提现划转、绑定 IP 白名单")
def connect(payload: ConnectIn) -> dict:
    _admin()
    s = get_services()
    conn = MI.connections(s).connect(
        exchange=payload.exchange,
        label=payload.label,
        user=_ctx().user_id or "",
        creds={"api_key": payload.api_key, "api_secret": payload.api_secret, "passphrase": payload.passphrase},
    )
    s.audit.record(
        "microlive.connect",
        object_type="microlive_connection",
        object_id=conn["id"],
        details={"exchange": conn["exchange"], "key": conn["key_mask"]},
    )
    return {"connection": conn}


@router.post("/connections/{cid}/test", summary="重新自检一个实盘 Key")
def test_connection(cid: str) -> dict:
    _admin()
    return {"connection": MI.connections(get_services()).test(cid)}


@router.delete("/connections/{cid}", summary="删除实盘 Key（平台保存的密文擦除）")
def revoke(cid: str) -> dict:
    _admin()
    s = get_services()
    conn = MI.connections(s).revoke(cid)
    s.audit.record("microlive.revoke", object_type="microlive_connection", object_id=cid)
    return {"connection": conn, "note": "平台保存的密钥已擦除。交易所那边的 Key 请到交易所后台删除。"}


# ---------------------------------------------------------------- 档位
@router.post("/tiers/{slug}/raise", summary="资金上限升一档（条件满足后由管理员确认）")
def raise_tier(slug: str) -> dict:
    _admin()
    s = get_services()
    t = MI.raise_tier(s.store, slug, user=_ctx().user_id or "")
    s.audit.record("microlive.tier_up", object_type="strategy", object_id=slug, details={"cap": t["cap"]})
    return {"tier": t}


@router.post("/tiers/{slug}/lower", summary="资金上限降一档")
def lower_tier(slug: str) -> dict:
    _admin()
    s = get_services()
    t = MI.lower_tier(s.store, slug, user=_ctx().user_id or "")
    s.audit.record("microlive.tier_down", object_type="strategy", object_id=slug, details={"cap": t["cap"]})
    return {"tier": t}


# ---------------------------------------------------------------- 启动
def _preflight(p: PrepareIn) -> dict[str, Any]:
    from .routes_research import _record_or_404

    s = get_services()
    st = MI.status(s.store, s.home)
    if not st["ready"]:
        raise AppError(ErrorCode.PERMISSION_DENIED, "微实盘不能启动：" + "；".join(st["blockers"]))
    rec = _record_or_404(p.slug)
    if (GR.record(s.store, p.slug) or {}).get("stage") != "graduated":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有晋级关卡里「毕业」的策略能上微实盘")
    if not rec.meta.human_approved:
        raise AppError(ErrorCode.PERMISSION_DENIED, "当前代码版本还没批准")
    sym = p.symbol.strip().upper()
    if ":" in sym or "/" not in sym:
        raise AppError(ErrorCode.INVALID_PARAMETER, "微实盘只做现货，交易对写成 BTC/USDT 这样")
    t = MI.tier(s.store, p.slug)
    if p.initial_cash > t["cap"]:
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            f"这个策略当前的资金上限是 {t['cap']:.0f}，本次资金 {p.initial_cash:.0f} 超出了",
        )
    if any(x.get("strategy_id") == p.slug for x in _sessions()):
        raise AppError(ErrorCode.CONFLICT, "这个策略已经有一个微实盘实验在跑")
    svc = MI.connections(s)
    conn = svc._out(svc.get(p.connection_id))
    if conn.get("status") != "active":
        raise AppError(ErrorCode.INVALID_PARAMETER, "这个实盘 Key 状态不正常，请先重新自检")
    fresh = svc.test(p.connection_id)  # 每次启动前再查一遍三项权限和余额
    if fresh.get("status") != "active":
        raise AppError(
            ErrorCode.PERMISSION_DENIED, "启动前自检没通过：" + str((fresh.get("last_test") or {}).get("error") or "")
        )
    quote = sym.split("/")[1]
    have = float(((fresh.get("last_test") or {}).get("balances") or {}).get(quote) or 0)
    if have < p.initial_cash:
        raise AppError(
            ErrorCode.INVALID_PARAMETER, f"交易所账户 {quote} 余额 {have:.2f}，不够本次 {p.initial_cash:.2f}"
        )
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
        "cap": t["cap"],
        "stop_loss": p.stop_loss,
        "max_daily_loss": p.max_daily_loss,
        "max_order_notional": t["cap"],
        "balance": have,
        "quote": quote,
    }


@router.post("/prepare", summary="生成启动摘要与 6 位确认码（5 分钟有效）")
def prepare(payload: PrepareIn) -> dict:
    _admin()
    summary = _preflight(payload)
    return MI.prepare(_store(), user=_ctx().user_id or "", summary=summary)


@router.post("/start", summary="输入确认码启动微实盘")
def start(payload: StartIn) -> dict:
    _admin()
    from .routes_studio import start_session
    from .schemas import PaperStartIn, RiskIn

    s = get_services()
    summ = MI.consume(
        s.store, payload.challenge_id, code=payload.code, phrase=payload.phrase, user=_ctx().user_id or ""
    )
    # 摘要生成后状态可能变了（急停、降档、Key 被撤），这里按摘要重新检查一遍
    again = _preflight(
        PrepareIn(
            slug=summ["slug"],
            connection_id=summ["connection_id"],
            symbol=summ["symbol"],
            initial_cash=summ["initial_cash"],
            stop_loss=summ["stop_loss"],
            max_daily_loss=summ["max_daily_loss"],
        )
    )
    if again["version"] != summ["version"] or again["cap"] < summ["initial_cash"]:
        raise AppError(ErrorCode.INVALID_PARAMETER, "确认之后策略版本或资金上限变了，请重新生成启动摘要")
    conn = MI.connections(s)._out(MI.connections(s).get(summ["connection_id"]))
    body = PaperStartIn(
        slug=summ["slug"],
        securities=[summ["symbol"]],
        initial_cash=summ["initial_cash"],
        source="exchange",
        exchange=summ["exchange"],
        execution="paper",
        risk=RiskIn(stop_loss=summ["stop_loss"]),
        max_order_notional=summ["max_order_notional"],
        max_orders_per_minute=6,
        max_daily_loss=summ["max_daily_loss"],
    )
    micro = {
        "cap": summ["cap"],
        "stop_loss": summ["stop_loss"],
        "notice": MI.NOTICE_VERSION,
        "confirmed_by": _ctx().user_id,
        "challenge_id": payload.challenge_id,
    }
    out = start_session(body, live={"connection": conn, "micro": micro})
    s.audit.record(
        "microlive.start",
        object_type="run",
        object_id=out["run_id"],
        details={k: summ[k] for k in ("slug", "symbol", "exchange", "initial_cash", "cap", "stop_loss")},
    )
    return out


@router.get("/notice", summary="风险告知原文与版本")
def notice() -> dict:
    return {"notice": MI.RISK_NOTICE, "version": MI.NOTICE_VERSION, "ack_phrase": MI.ACK_PHRASE}
