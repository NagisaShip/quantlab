"""组合与风控 API：方案评估（滚动组合回测）、风险体检、试验记录、方案库。

评估是同步的（10 个标的 1500 根 K 线通常一两秒内完成）；每次评估都记一笔试验。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import backtest_retention as BR
from ..platform import context
from ..research import goals as RG
from ..research import portfolio as PF
from ..research.datahub import DataHub
from ..research.factor_expr import fields_used, parse
from ..research.factor_lib import FactorLibrary
from ..research.model_lib import ModelLibrary
from ..research.models import fields_for
from ..research.panel import UNIVERSES, PanelError, build_panel
from ..research.portfolio_lib import STATUS_LABELS, PortfolioLibrary, signal_for
from .deps import get_services

router = APIRouter(prefix="/api/portfolio", tags=["组合与风控"])

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
Source = Literal["auto", "exchange", "synthetic"]


class EvalIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    universe: str = "major10"
    symbols: list[str] = Field(default_factory=list)
    timeframe: Timeframe = "4h"
    bars_limit: int = Field(1500, ge=300, le=20_000)
    start: int | None = None
    end: int | None = None
    source: Source = "auto"
    cost_bps: float = Field(10.0, ge=0, le=200)
    note: str = Field("", max_length=200)


class RiskIn(BaseModel):
    weights: dict[str, float]
    timeframe: Timeframe = "4h"
    bars_limit: int = Field(1000, ge=200, le=20_000)
    lookback: int = Field(180, ge=30, le=5000)
    source: Source = "auto"


class SaveIn(BaseModel):
    run_id: str
    name: str = Field("", max_length=40)
    status: Literal["candidate", "active", "retired"] = "candidate"
    note: str = Field("", max_length=400)
    scheme_id: str | None = None


class PatchIn(BaseModel):
    name: str | None = Field(None, max_length=40)
    status: Literal["candidate", "active", "retired"] | None = None
    note: str | None = Field(None, max_length=400)


def _lib() -> PortfolioLibrary:
    return PortfolioLibrary(get_services().store)


def _hub() -> DataHub:
    s = get_services()
    return DataHub(s.store, secrets_fn=s.read_secrets)


def _symbols(universe: str, symbols: list[str]) -> list[str]:
    out = [x.strip().upper() for x in symbols if x.strip()]
    if out:
        return out
    if universe not in UNIVERSES:
        raise HTTPException(400, f"未知标的池 {universe}")
    return list(UNIVERSES[universe]["symbols"])


def _fields(cfg: dict[str, Any]) -> list[str]:
    src = cfg.get("expected") or "none"
    store = get_services().store
    if src.startswith("model:"):
        rec = ModelLibrary(store).by_name(src[6:])
        return fields_for(rec["spec"]) if rec else []
    if src.startswith("factor:"):
        rec = FactorLibrary(store).by_name(src[7:])
        try:
            return sorted(fields_used(parse(rec["expression"] if rec else src[7:])))
        except Exception:
            return []
    return []


@router.get("/meta", summary="组合与风控元信息：优化器、风险模型、预期收益来源、压力情景、目标指标")
def portfolio_meta() -> dict:
    store = get_services().store
    return {
        "optimizers": [{"id": k, **v} for k, v in PF.OPTIMIZERS.items()],
        "risk_models": [{"id": k, **v} for k, v in PF.RISK_MODELS.items()],
        "expected": [{"id": k, "label": v} for k, v in PF.EXPECTED_SOURCES.items()],
        "models": [
            {"name": m["name"], "status": m["status"], "label": m["spec"]["label"]}
            for m in ModelLibrary(store).list()
            if m.get("status") != "retired"
        ],
        "factors": [
            {"name": f["name"], "status": f["status"]}
            for f in FactorLibrary(store).list()
            if f.get("status") != "retired"
        ],
        "scenarios": [{"id": s["id"], "name": s["name"], "note": s.get("note", "")} for s in PF.SCENARIOS],
        "defaults": PF.DEFAULT_CONFIG,
        "universes": [{"id": k, "name": v["name"], "symbols": v["symbols"]} for k, v in UNIVERSES.items()],
        "statuses": STATUS_LABELS,
        "goal": RG.PORTFOLIO.meta(),
        "trials": _lib().trial_count(),
    }


@router.post("/evaluate", summary="评估一份配置方案：滚动组合回测 + 风险分解 + 压力测试 + 有效前沿")
def evaluate(payload: EvalIn) -> dict:
    services = get_services()
    ctx = context.current()
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        ctx.user_id or "",
        ctx.role,
        "portfolio_run",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), ctx.user_id or "", ctx.role
        ),
    )
    syms = _symbols(payload.universe, payload.symbols)
    try:
        cfg = PF.normalize_config(payload.config)
        panel = build_panel(
            get_services().market,
            _hub(),
            syms,
            payload.timeframe,
            fields=_fields(cfg),
            start=payload.start,
            end=payload.end,
            bars_limit=payload.bars_limit,
            source=payload.source,
        )
        signal, ic, warns = signal_for(cfg, panel, get_services().store)
        report = PF.evaluate_scheme(cfg, panel, cost_bps=payload.cost_bps, signal=signal, signal_ic=ic)
    except (PF.PortfolioError, PanelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    report["panel"]["warnings"] = [*report["panel"].get("warnings", []), *warns]
    run = _lib().record_run(
        cfg,
        report,
        symbols=panel.symbols,
        timeframe=payload.timeframe,
        source="portfolio",
        note=payload.note,
        created_by=ctx.user_id or "user",
    )
    return {"run": run, "report": report}


@router.post("/risk", summary="风险体检：给定权重的波动、VaR/CVaR、风险贡献与压力测试")
def risk(payload: RiskIn) -> dict:
    w = {k.strip().upper(): float(v) for k, v in payload.weights.items() if abs(float(v)) > 0}
    if not w:
        raise HTTPException(400, "权重为空")
    try:
        panel = build_panel(
            get_services().market,
            _hub(),
            list(w),
            payload.timeframe,
            fields=[],
            bars_limit=payload.bars_limit,
            source=payload.source,
        )
        return PF.risk_check(w, panel, lookback=payload.lookback)
    except (PF.PortfolioError, PanelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs", summary="试验记录")
def list_runs(limit: int = 200) -> dict:
    lib = _lib()
    return {"runs": lib.runs(max(1, min(limit, 500))), "trials": lib.trial_count()}


@router.get("/runs/{rid}", summary="试验详情")
def get_run(rid: str) -> dict:
    lib = _lib()
    run = lib.run(rid)
    if run is None:
        raise HTTPException(404, "试验不存在")
    return {"run": run, "report": lib.run_report(rid)}


@router.delete("/runs/{rid}", summary="删除试验记录")
def delete_run(rid: str) -> dict:
    return {"ok": _lib().delete_run(rid)}


@router.get("", summary="方案库")
def list_schemes(status: str | None = None) -> dict:
    lib = _lib()
    return {"schemes": lib.list(status), "trials": lib.trial_count()}


@router.post("", summary="把一次试验存为方案（策略里用 optimize('名字') 调用）")
def save_scheme(payload: SaveIn) -> dict:
    services = get_services()
    ctx = context.current()
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        ctx.user_id or "",
        ctx.role,
        "portfolio",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), ctx.user_id or "", ctx.role
        ),
    )
    lib = _lib()
    run, report = lib.run(payload.run_id), lib.run_report(payload.run_id)
    if run is None or report is None:
        raise HTTPException(404, "试验不存在")
    rec = lib.save(
        name=payload.name,
        config=report["config"],
        report=report,
        symbols=run["symbols"],
        timeframe=run["timeframe"],
        status=payload.status,
        note=payload.note,
        scheme_id=payload.scheme_id,
        run_id=payload.run_id,
        created_by=ctx.user_id or "user",
    )
    return {"scheme": rec}


@router.get("/{sid}", summary="方案详情")
def get_scheme(sid: str) -> dict:
    rec = _lib().get(sid)
    if rec is None:
        raise HTTPException(404, "方案不存在")
    return {"scheme": rec}


@router.get("/{sid}/report", summary="方案的完整评估报告")
def scheme_report(sid: str) -> dict:
    lib = _lib()
    if lib.get(sid) is None:
        raise HTTPException(404, "方案不存在")
    return {"report": lib.report(sid)}


@router.patch("/{sid}", summary="修改名称、状态、备注")
def patch_scheme(sid: str, payload: PatchIn) -> dict:
    try:
        rec = _lib().patch(sid, **payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if rec is None:
        raise HTTPException(404, "方案不存在")
    return {"scheme": rec}


@router.delete("/{sid}", summary="删除方案（试验记录保留）")
def delete_scheme(sid: str) -> dict:
    lib = _lib()
    if lib.get(sid) is None:
        raise HTTPException(404, "方案不存在")
    return {"ok": lib.delete(sid)}
