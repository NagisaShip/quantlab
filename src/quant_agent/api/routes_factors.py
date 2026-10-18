"""因子库 API：表达式校验与评估、因子库增删改、评估报告、健康度刷新、试验台账。

评估同步执行（一次几百毫秒到几秒，主要耗在拉行情），结果与 AI 研究室里的因子评估同口径：
每次评估都计入试验台账，显著性门槛随试验次数提高（多重检验）。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import backtest_retention as BR
from ..platform import context
from ..research import factor_expr as FX
from ..research import goals as RG
from ..research.datahub import DataHub
from ..research.factor_eval import ADMISSION, evaluate_factor
from ..research.factor_lib import CATEGORIES, SEEDS, STATUS_LABELS, FactorLibrary, expr_hash
from ..research.panel import UNIVERSES, PanelError, build_panel
from .deps import get_services

router = APIRouter(prefix="/api/factors", tags=["因子库"])

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


class ExprIn(BaseModel):
    expression: str = Field(..., min_length=1, max_length=2000)


class EvalIn(BaseModel):
    expression: str = Field(..., min_length=1, max_length=2000)
    universe: str = "major10"
    symbols: list[str] = Field(default_factory=list)
    timeframe: Timeframe = "4h"
    horizon: int = Field(1, ge=1, le=60)
    bars_limit: int = Field(1500, ge=200, le=20_000)
    start: int | None = None
    end: int | None = None
    source: Literal["auto", "exchange", "synthetic"] = "auto"
    cost_bps: float = Field(10.0, ge=0, le=200)
    exclude: str | None = None  # 评估库内因子自身时，不和自己比相关


class FactorIn(BaseModel):
    name: str = Field("", max_length=40)
    expression: str = Field(..., min_length=1, max_length=2000)
    hypothesis: dict[str, Any] = Field(default_factory=dict)
    status: Literal["candidate", "active", "decaying", "retired"] = "candidate"
    universe: list[str] = Field(default_factory=list)
    timeframe: Timeframe = "4h"
    horizon: int = Field(1, ge=1, le=60)
    tags: list[str] = Field(default_factory=list)
    note: str = Field("", max_length=400)
    evaluate: bool = True


class FactorPatch(BaseModel):
    name: str | None = Field(None, max_length=40)
    hypothesis: dict[str, Any] | None = None
    tags: list[str] | None = None
    note: str | None = Field(None, max_length=400)


class StatusIn(BaseModel):
    status: Literal["candidate", "active", "decaying", "retired"]


class RefreshIn(BaseModel):
    source: Literal["auto", "exchange", "synthetic"] = "auto"
    bars_limit: int = Field(1500, ge=200, le=20_000)
    cost_bps: float = Field(10.0, ge=0, le=200)


def _lib() -> FactorLibrary:
    return FactorLibrary(get_services().store)


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


def _run_eval(
    expr: str,
    *,
    symbols: list[str],
    timeframe: str,
    horizon: int,
    bars_limit: int,
    start: int | None,
    end: int | None,
    source: str,
    cost_bps: float,
    exclude: str | None = None,
    record: bool = True,
) -> dict[str, Any]:
    try:
        node = FX.parse(expr)
    except FX.FactorExprError as exc:
        raise HTTPException(400, f"表达式有误：{exc}") from exc
    lib = _lib()
    try:
        panel = build_panel(
            get_services().market,
            _hub(),
            symbols,
            timeframe,
            fields=FX.fields_used(node),
            start=start,
            end=end,
            bars_limit=bars_limit,
            source=source,
        )
    except PanelError as exc:
        raise HTTPException(400, str(exc)) from exc
    norm = str(node)
    trials = lib.trial_count() + (0 if lib.store.get("research_trials", expr_hash(norm)) else 1)
    report = evaluate_factor(
        node,
        panel,
        horizon=horizon,
        cost_bps=cost_bps,
        library=lib.library_values(panel, exclude=exclude),
        trials=trials,
    )
    if record:
        lib.record_trial(norm, report["summary"], source="factors")
    report["expression"] = norm
    report["settings"] = {
        "symbols": panel.symbols,
        "timeframe": timeframe,
        "horizon": horizon,
        "bars_limit": bars_limit,
        "start": start,
        "end": end,
        "source": source,
        "cost_bps": cost_bps,
    }
    return report


def _brief(lib: FactorLibrary, rec: dict[str, Any]) -> dict[str, Any]:
    return {**{k: v for k, v in rec.items() if k != "checks"}, "health_flag": lib.health_flag(rec)}


# ---------------------------------------------------------------------------
@router.get("/meta", summary="因子研究元信息：字段、算子、标的池、类别、状态、入库门槛、目标指标、种子假设")
def factors_meta() -> dict:
    lib = _lib()
    custom = sorted({d.get("field") for d in _hub().datasets() if str(d.get("field", "")).startswith("x_")})
    return {
        "catalog": FX.catalog(),
        "custom_fields": custom,
        "universes": [{"id": k, "name": v["name"], "symbols": v["symbols"]} for k, v in UNIVERSES.items()],
        "categories": CATEGORIES,
        "statuses": STATUS_LABELS,
        "admission": ADMISSION,
        "goal": RG.FACTOR.meta(),
        "seeds": [
            {"name": s["name"], "expression": s["expression"], "hypothesis": s.get("hypothesis") or {}} for s in SEEDS
        ],
        "trials": lib.trial_count(),
        "limits": {"max_window": FX.MAX_WINDOW, "max_nodes": FX.MAX_NODES, "max_depth": FX.MAX_DEPTH},
    }


@router.post("/validate", summary="校验因子表达式：返回规范写法、复杂度、用到的字段与最长回看")
def validate_expression(payload: ExprIn) -> dict:
    try:
        node = FX.parse(payload.expression)
    except FX.FactorExprError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "expression": str(node),
        "complexity": FX.complexity(node),
        "fields": FX.fields_used(node),
        "lookback": FX.max_lookback(node),
    }


@router.post("/evaluate", summary="评估一个因子表达式：IC、衰减、分组、多空、换手、样本外、库内相关与入库检查")
def evaluate_expression(payload: EvalIn) -> dict:
    return _run_eval(
        payload.expression,
        symbols=_symbols(payload.universe, payload.symbols),
        timeframe=payload.timeframe,
        horizon=payload.horizon,
        bars_limit=payload.bars_limit,
        start=payload.start,
        end=payload.end,
        source=payload.source,
        cost_bps=payload.cost_bps,
        exclude=payload.exclude,
    )


@router.get("/trials", summary="试验台账：评估过的表达式（含失败的），用于多重检验与避免重复")
def list_trials(limit: int = 100) -> dict:
    lib = _lib()
    rows = sorted(lib.store.list("research_trials"), key=lambda r: -(r.get("last_at") or 0))
    return {"trials": rows[: max(1, min(limit, 500))], "total": len(rows)}


@router.get("", summary="因子库列表（status 过滤）")
def list_factors(status: str | None = None, q: str = "") -> dict:
    lib = _lib()
    rows = lib.list(status)
    if q:
        ql = q.lower()
        rows = [
            r
            for r in rows
            if ql in (r.get("name") or "").lower()
            or ql in (r.get("expression") or "").lower()
            or ql in str(r.get("hypothesis") or "").lower()
        ]
    return {"factors": [_brief(lib, r) for r in rows], "trials": lib.trial_count()}


@router.post("", summary="保存因子到库（默认先评估，报告一并保存）")
def create_factor(payload: FactorIn) -> dict:
    services = get_services()
    ctx = context.current()
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        ctx.user_id or "",
        ctx.role,
        "factor",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), ctx.user_id or "", ctx.role
        ),
    )
    lib = _lib()
    try:
        expr = str(FX.parse(payload.expression))
    except FX.FactorExprError as exc:
        raise HTTPException(400, f"表达式有误：{exc}") from exc
    report = None
    universe = [x.strip().upper() for x in payload.universe if x.strip()] or list(UNIVERSES["major10"]["symbols"])
    if payload.evaluate:
        report = _run_eval(
            expr,
            symbols=universe,
            timeframe=payload.timeframe,
            horizon=payload.horizon,
            bars_limit=1500,
            start=None,
            end=None,
            source="auto",
            cost_bps=10.0,
        )
    rec = lib.save(
        {
            **payload.model_dump(exclude={"evaluate"}),
            "expression": expr,
            "universe": universe,
            "created_by": ctx.user_id or "user",
        },
        report=report,
    )
    return {"factor": _brief(lib, rec)}


@router.get("/{fid}", summary="因子详情")
def get_factor(fid: str) -> dict:
    lib = _lib()
    rec = lib.get(fid)
    if rec is None:
        raise HTTPException(404, "因子不存在")
    return {"factor": _brief(lib, rec)}


@router.patch("/{fid}", summary="修改因子名称、假设卡、标签、备注（表达式不可改，改了就是新因子）")
def patch_factor(fid: str, payload: FactorPatch) -> dict:
    lib = _lib()
    rec = lib.get(fid)
    if rec is None:
        raise HTTPException(404, "因子不存在")
    changes = payload.model_dump(exclude_none=True)
    rec = lib.save({**rec, **changes, "id": fid})
    return {"factor": _brief(lib, rec)}


@router.delete("/{fid}", summary="删除因子（试验台账保留）")
def delete_factor(fid: str) -> dict:
    lib = _lib()
    rec = lib.get(fid)
    if rec is None:
        raise HTTPException(404, "因子不存在")
    ctx = context.current()
    if rec.get("pinned") and ctx.role != "admin":
        raise HTTPException(409, "这条因子已 PIN 保护，请先取消 PIN")
    if rec.get("created_by") not in (None, "", "user", ctx.user_id) and ctx.role != "admin":
        raise HTTPException(403, "只能删除自己的因子")
    return {"ok": lib.delete(fid)}


@router.get("/{fid}/report", summary="因子最近一次完整评估报告")
def factor_report(fid: str) -> dict:
    lib = _lib()
    if lib.get(fid) is None:
        raise HTTPException(404, "因子不存在")
    rep = lib.report(fid)
    if rep is None:
        raise HTTPException(404, "这个因子还没有评估报告")
    return {"report": rep}


@router.post("/{fid}/status", summary="改变因子状态：候选 / 入库 / 衰减预警 / 退役")
def set_status(fid: str, payload: StatusIn) -> dict:
    lib = _lib()
    rec = lib.set_status(fid, payload.status)
    if rec is None:
        raise HTTPException(404, "因子不存在")
    return {"factor": _brief(lib, rec)}


@router.post("/{fid}/refresh", summary="用最新数据重新评估因子，记录健康度（IC 是否衰减）")
def refresh_factor(fid: str, payload: RefreshIn) -> dict:
    lib = _lib()
    rec = lib.get(fid)
    if rec is None:
        raise HTTPException(404, "因子不存在")
    report = _run_eval(
        rec["expression"],
        symbols=rec.get("universe") or list(UNIVERSES["major10"]["symbols"]),
        timeframe=rec.get("timeframe") or "4h",
        horizon=int(rec.get("horizon") or 1),
        bars_limit=payload.bars_limit,
        start=None,
        end=None,
        source=payload.source,
        cost_bps=payload.cost_bps,
        exclude=fid,
        record=False,
    )
    rec = lib.save({**rec, "id": fid}, report=report)
    flag = lib.health_flag(rec)
    if flag == "decaying" and rec.get("status") == "active":
        rec = lib.set_status(fid, "decaying") or rec
    return {"factor": _brief(lib, rec), "report": report}
