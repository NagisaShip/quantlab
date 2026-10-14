"""自适应与衰减：策略衰减监测（降级建议）、模型定期重训、按市场状态的调度权重。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import context
from ..research import decay as D
from ..research import graduation as GR
from ..research import regime as RG
from ..research import retrain as RT
from .deps import get_services

router = APIRouter(prefix="/api", tags=["自适应与衰减"])


class PlanIn(BaseModel):
    enabled: bool | None = None
    every_days: int | None = Field(None, ge=1, le=90)
    min_ratio: float | None = Field(None, ge=0, le=2)


class RetrainIn(BaseModel):
    asof: int | None = None
    source: str = "auto"
    bars_limit: int = Field(1500, ge=300, le=20_000)


def _user() -> str:
    return context.current().user_id


def running_sessions(services: Any, ws: str) -> list[dict[str, Any]]:
    from ..notify import bot as B

    return B.BotService(services)._sessions(ws)


def check_all(services: Any, ws: str) -> list[dict[str, Any]]:
    """对工作区里每个运行中的实验做一次衰减判定；新变成「衰减」的发告警。"""
    from ..notify import alerts

    store = services.store.for_workspace(ws)
    out = []
    for s in running_sessions(services, ws):
        if not s.get("strategy_id"):
            continue
        rep = D.check_session(store, s)
        out.append(rep)
        if rep.get("changed"):
            alerts.raise_alert_safe(
                services.db,
                ws,
                alerts.EVENT_STRATEGY_DECAY,
                subject=rep.get("strategy_name") or s["id"],
                context_data={
                    "session_id": s["id"],
                    "reasons": rep.get("reasons", [])[:3],
                    "suggest_demote_to": rep.get("suggest_demote_to"),
                },
            )
    return out


def current_regime(services: Any, symbol: str = "BTC/USDT") -> dict[str, Any]:
    try:
        ds = services.market.dataset(symbol, "1d", limit=400, source="auto")
        closes = [float(b.close) for b in ds.bars]
        labels = RG.classify(closes)
        lab = next((x for x in reversed(labels) if x), None)
        return {
            "regime": lab,
            "label": RG.REGIMES.get(lab or "", "数据不足"),
            "symbol": symbol,
            "as_of": ds.bars[-1].close_time if ds.bars else None,
            "source": ds.summary().get("source"),
        }
    except Exception as exc:
        return {"regime": None, "label": "数据不足", "symbol": symbol, "error": str(exc)[:200]}


def schedule_weights(services: Any, ws: str, regime: str | None) -> list[dict[str, Any]]:
    store = services.store.for_workspace(ws)
    members: dict[str, dict[str, Any]] = {}
    info: dict[str, dict[str, Any]] = {}
    for s in running_sessions(services, ws):
        slug = s.get("strategy_id")
        if not slug:
            continue
        bt = D.latest_backtest(store, slug)
        mult = RG.from_backtest(bt) if bt else {}
        live = D.live_stats(s.get("equity_points") or [], s.get("fills") or [])
        members[s["id"]] = {"base": 1.0, "returns": live.get("daily") or [], "regimes": mult}
        info[s["id"]] = {"session_id": s["id"], "strategy": s.get("strategy_name"), "multipliers": mult}
    w = RG.weights(members, regime) if members else {}
    return [{**info[k], "weight": round(v, 4)} for k, v in sorted(w.items(), key=lambda kv: -kv[1])]


@router.get("/lifecycle", summary="自适应与衰减总览：衰减判定、模型重训计划、市场状态与建议权重")
def overview() -> dict:
    from ..research.model_lib import ModelLibrary

    s = get_services()
    ws = context.current_workspace()
    lib = ModelLibrary(s.store)
    models = []
    for m in lib.list():
        log = m.get("retrain_log") or []
        models.append(
            {
                "id": m["id"],
                "name": m["name"],
                "status": m.get("status"),
                "plan": RT.plan(m),
                "due": RT.due(m),
                "last_retrain": RT.last_retrain(m),
                "last": log[-1] if log else None,
                "versions": len(m.get("versions") or []),
                "frozen": bool(m.get("frozen")),
                "health": (m.get("health") or [None])[-1],
            }
        )
    reg = current_regime(s)
    return {
        "decay": D.reports(s.store),
        "models": models,
        "regime": reg,
        "weights": schedule_weights(s, ws, reg.get("regime")),
        "regimes": RG.REGIMES,
    }


@router.post("/lifecycle/check", summary="立刻对运行中的实验做一次衰减判定")
def check_now() -> dict:
    s = get_services()
    return {"reports": check_all(s, context.current_workspace())}


@router.post("/lifecycle/{session_id}/demote", summary="按衰减监测的建议把策略退回上一阶段（需要人确认）")
def demote(session_id: str) -> dict:
    s = get_services()
    rep = s.store.get(D.REPORTS, session_id)
    if rep is None:
        raise HTTPException(404, "没有这个实验的衰减判定")
    to = rep.get("suggest_demote_to")
    if rep.get("status") != "decay" or not to:
        raise HTTPException(400, "当前判定不建议降级")
    try:
        out = GR.demote(
            s.store,
            rep["strategy_id"],
            to=to,
            user=_user(),
            note="衰减监测建议：" + "；".join(rep.get("reasons") or [])[:300],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s.audit.record("lifecycle.demote", object_type="strategy", object_id=rep["strategy_id"], details={"to": to})
    s.store.upsert(D.REPORTS, {**rep, "suggest_demote_to": None, "demoted_at": int(time.time() * 1000), "stage": to})
    return {"graduation": out}


@router.post("/models/{mid}/retrain-plan", summary="设置模型的定期重训计划")
def retrain_plan(mid: str, payload: PlanIn) -> dict:
    from ..research.model_lib import ModelLibrary

    rec = RT.set_plan(ModelLibrary(get_services().store), mid, **payload.model_dump())
    if rec is None:
        raise HTTPException(404, "模型不存在")
    return {"model": rec, "plan": RT.plan(rec)}


@router.post("/models/{mid}/retrain", summary="立刻重训一次（只用截至重训时点的数据）")
def retrain_now(mid: str, payload: RetrainIn) -> dict:
    from ..research import learners as L
    from ..research import models as M
    from ..research.model_lib import ModelLibrary
    from ..research.panel import PanelError
    from .routes_models import _panel

    lib = ModelLibrary(get_services().store)
    rec = lib.get(mid)
    if rec is None:
        raise HTTPException(404, "模型不存在")
    spec = {
        **rec["spec"],
        "bars_limit": payload.bars_limit,
        "start": None,
        "end": payload.asof,
        "source": payload.source,
    }
    try:
        return RT.retrain(lib, mid, _panel(spec), asof_ms=payload.asof, reason="manual")
    except (PanelError, M.ModelError, L.LearnerError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
