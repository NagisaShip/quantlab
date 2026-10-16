"""研究中心 API：策略模板、新建/克隆、回测记录与详情、对比、参数优化。

对标聚宽"我的策略 → 编辑 → 编译运行 / 运行回测 → 回测详情 / 参数调优"。
"""

from __future__ import annotations

from typing import Literal, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..data.service import DataUnavailable
from ..engine.research import (
    BacktestSpec,
    load_market,
    summarize_metrics,
)
from ..platform.errors import AppError, ErrorCode
from ..sdk import check_source, declared_params
from ..store import Collections
from ..strategies import lang as L
from ..strategies.params_util import ParamsError
from ..strategies.params_util import rewrite_params as _rewrite_params
from ..strategies.repo import StrategyRepository
from ..strategies.sdk_templates import get_template, list_templates
from . import tasks
from .deps import get_services
from .routes_dsl import register_version
from .routes_runs import enqueue
from .schemas import (
    ApplyParamsIn,
    CloneIn,
    CompareIn,
    NewStrategyIn,
    OptimizeIn,
    ResearchBacktestIn,
    SpecIn,
)

router = APIRouter(prefix="/api/research", tags=["研究中心"])


def _repo() -> StrategyRepository:
    return StrategyRepository(get_services().store.root / "strategies")


def _record_or_404(slug: str):
    rec = _repo().get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    return rec


# ---------------------------------------------------------------------------
# 模板与策略
# ---------------------------------------------------------------------------
@router.get("/templates")
def templates() -> dict:
    return {"templates": list_templates(include_code=False)}


@router.get("/templates/{template_id}")
def template_detail(template_id: str) -> dict:
    tpl = get_template(template_id)
    if tpl is None:
        raise HTTPException(404, "模板不存在")
    return {"template": tpl.to_dict(), "params": declared_params(tpl.code)}


@router.post("/strategies")
def new_strategy(payload: NewStrategyIn) -> dict:
    """新建策略：从模板或给定代码。同名不覆盖，自动编号。"""
    tpl = get_template(payload.template_id) if payload.template_id else None
    if payload.code is None and tpl is None:
        raise HTTPException(400, "请选择模板或提供代码")
    code = payload.code if payload.code is not None else tpl.code  # type: ignore[union-attr]
    securities = payload.securities or (tpl.securities if tpl else ["BTC/USDT"])
    timeframe = payload.timeframe or (tpl.timeframe if tpl else "1h")
    repo = _repo()
    rec = repo.save(
        name=payload.name,
        source=code,
        params=declared_params(code),
        team=payload.team,
        kind="code",
        origin=payload.origin if payload.code is not None else "template",
        symbol=securities[0],
        timeframe=timeframe,
        securities=securities,
        notes=payload.notes or (f"由模板「{tpl.name}」创建" if tpl and payload.code is None else ""),
        slug=repo.unique_slug(payload.name),
    )
    return {"strategy": rec.to_dict(), "issues": check_source(code)}


@router.post("/strategies/{slug}/clone")
def clone_strategy(slug: str, payload: CloneIn) -> dict:
    src = _record_or_404(slug)
    repo = _repo()
    name = payload.name or f"{src.meta.name}（副本）"
    rec = repo.save(
        name=name,
        source=src.source,
        params=src.params,
        team=src.meta.team,
        kind=src.meta.kind,
        origin="user",
        symbol=src.meta.symbol,
        timeframe=src.meta.timeframe,
        securities=src.meta.securities,
        notes=f"克隆自「{src.meta.name}」（{src.meta.version}）",
        slug=repo.unique_slug(name),
    )
    return {"strategy": rec.to_dict()}


@router.get("/strategies/{slug}/params")
def strategy_params(slug: str) -> dict:
    rec = _record_or_404(slug)
    return {"params": L.declared_params(L.lang_of(rec.meta.kind), rec.source)}


@router.post("/strategies/{slug}/apply-params")
def apply_params(slug: str, payload: ApplyParamsIn) -> dict:
    """把一组参数写回策略顶部的 PARAMS（保留注释与格式），生成新版本。"""
    rec = _record_or_404(slug)
    declared = declared_params(rec.source)
    unknown = sorted(set(payload.params) - set(declared))
    if unknown:
        raise HTTPException(400, f"参数 {', '.join(unknown)} 不在 PARAMS 中")
    code = rewrite_params(rec.source, payload.params)
    saved = _repo().save(
        name=rec.meta.name,
        source=code,
        params=declared_params(code),
        team=rec.meta.team,
        kind=rec.meta.kind,
        origin=rec.meta.source,
        symbol=rec.meta.symbol,
        timeframe=rec.meta.timeframe,
        securities=rec.meta.securities,
        notes=rec.meta.notes,
        slug=slug,
    )
    return {"strategy": saved.to_dict()}


def rewrite_params(source: str, values: dict[str, Any]) -> str:
    try:
        return _rewrite_params(source, values)
    except ParamsError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------
def _spec(
    payload: SpecIn, *, default_securities: list[str] | None = None, timeframe: str | None = None
) -> BacktestSpec:
    data = payload.model_dump()
    data["securities"] = [
        s.strip().upper() for s in (payload.securities or default_securities or ["BTC/USDT"]) if s.strip()
    ][:10]
    if timeframe and "timeframe" not in payload.model_fields_set:
        data["timeframe"] = timeframe
    allowed = set(BacktestSpec.__dataclass_fields__)
    return BacktestSpec(**{k: v for k, v in data.items() if k in allowed})


def _market(spec: BacktestSpec):
    try:
        return load_market(get_services().market, spec)
    except DataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/backtests")
def run_backtest_endpoint(payload: ResearchBacktestIn, request: Request) -> Any:
    """运行回测：登记 run → worker 执行 → 返回结果。

    控制面不执行回测：请求只做校验并登记任务；`wait_seconds` 内完成则直接返回结果，
    否则返回 202 与 run_id，前端轮询 /api/runs/{run_id}。`persist=false` 相当于聚宽的“编译运行”。
    """
    rec = None
    if payload.slug:
        rec = _record_or_404(payload.slug)
        source, name = (payload.code if payload.code is not None else rec.source), rec.meta.name
        spec = _spec(payload, default_securities=rec.meta.securities, timeframe=rec.meta.timeframe)
        lang = L.lang_of(rec.meta.kind)
    elif payload.code:
        source, name = payload.code, payload.name or "未命名策略"
        spec = _spec(payload)
        lang = payload.lang or (L.DSL if source.lstrip().startswith("{") else L.PYTHON)
    else:
        raise HTTPException(400, "请提供 slug 或 code")

    issues = L.issues(lang, source)
    if issues:
        raise AppError(ErrorCode.INVALID_PARAMETER, "；".join(issues), details={"problems": issues})
    if lang == L.PYTHON:
        _python_allowed()
    else:
        spec.warmup_bars = max(spec.warmup_bars, L.load(lang, source, name="check").compiled.warmup_bars)
    services = get_services()
    version = _version_for(rec, lang, source, name, spec)
    m = {
        "lang": lang,
        "source": source,
        "module_name": rec.meta.slug if rec else "inline",
        "name": name,
        "slug": rec.meta.slug if rec else None,
        "spec": spec.to_dict(),
        "persist": payload.persist,
        "label": payload.label,
        "lab_project_id": payload.lab_project_id,
        "lab_title": payload.lab_title,
        "version": rec.meta.version if rec and payload.code is None else "未保存的草稿",
        "mark_backtested": bool(rec and payload.code is None),
    }
    from ..research.discipline import research_cutoff

    if research_cutoff(services.store) is None and payload.label == "紧封测试集开封":
        m["holdout_bypass"] = True  # 只有开封流程（在 unsealed() 里调用）能看到紧封测试集
    version_id = version["id"]
    run = enqueue(
        kind="backtest",
        mode="backtest",
        title=f"回测 · {name}",
        manifest=m,
        strategy_version_id=version_id,
        idempotency_key=request.headers.get("idempotency-key") or None,
    )
    run = tasks.dispatch(services, run, wait_seconds=payload.wait_seconds)
    tasks.raise_if_failed(run)
    if run["state"] != "COMPLETED":
        return JSONResponse(
            status_code=202, content={"id": None, "run_id": run["id"], "state": run["state"], "result": None}
        )
    body = services.store.get_blob("run_results", run["id"]) or {}
    summary = run.get("summary") or {}
    return {"id": summary.get("bt_id"), "run_id": run["id"], "result": body, "row": summary.get("row")}


def _version_for(rec, lang: str, source: str, name: str, spec: BacktestSpec) -> dict:
    """每次运行都引用一个不可改写的策略版本（未保存的草稿登记在 _scratch 下）。"""
    from ..platform import context

    if rec is not None:
        v = register_version(rec, source=source)
    else:
        v = get_services().versions.ensure(
            workspace_id=context.current_workspace(),
            slug="_scratch",
            lang=lang,
            name=name,
            code=source,
            params=L.declared_params(lang, source),
            securities=spec.securities,
            timeframe=spec.timeframe,
            user_id=context.current().user_id,
            issues=[],
        )
    get_services().versions.require_runnable(v)
    return v


def _python_allowed() -> None:
    """工作区可关闭 Python 策略（只允许规则 DSL）。"""
    from ..platform import context

    ws = get_services().auth.workspace(context.current_workspace())
    if not (ws.get("settings") or {}).get("quotas", {}).get("allow_python_strategies", True):
        raise AppError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "该工作区已关闭 Python 策略，请使用规则 DSL 策略",
            details={"setting": "allow_python_strategies"},
        )


class PyCodeIn(BaseModel):
    kind: Literal["factor", "model", "portfolio", "execution"]
    artifact: Any = Field(..., description="因子表达式，或模型 / 组合 / 执行规格（对象或 JSON 字符串）")
    name: str = Field("", max_length=60)


@router.post("/pycode", summary="研究产物 → Python 代码：因子（独立可运行，结果与平台一致）、模型 / 组合 / 执行（规格 + 复现代码）")
def to_python(payload: PyCodeIn) -> dict:
    try:
        from ..research import pycode as PC

        return {"code": PC.to_python(payload.kind, payload.artifact, name=payload.name)}
    except ImportError as exc:  # pragma: no cover - numpy 没装
        raise HTTPException(503, "需要 numpy") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, f"无法生成 Python 代码：{exc}") from exc


@router.get("/backtests")
def list_backtests(slug: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict:
    rows = get_services().store.list(Collections.BACKTESTS)
    if slug:
        rows = [r for r in rows if r.get("slug") == slug]
    return {"backtests": rows[:limit], "total": len(rows)}


@router.get("/backtests/{bt_id}")
def get_backtest(bt_id: str) -> dict:
    body = get_services().store.get_blob("backtests", bt_id)
    if body is None:
        raise HTTPException(404, "回测记录不存在")
    return body


@router.delete("/backtests/{bt_id}")
def delete_backtest(bt_id: str) -> dict:
    s = get_services().store
    found = s.delete(Collections.BACKTESTS, bt_id)
    s.delete_blob("backtests", bt_id)
    if not found:
        raise HTTPException(404, "回测记录不存在")
    return {"ok": True}


class BatchDeleteIn(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=500)


@router.post("/backtests/batch-delete", summary="批量删除回测记录")
def batch_delete_backtests(payload: BatchDeleteIn) -> dict:
    s = get_services().store
    deleted = 0
    for bt_id in dict.fromkeys(payload.ids):
        if s.delete(Collections.BACKTESTS, bt_id):
            deleted += 1
        s.delete_blob("backtests", bt_id)
    return {"ok": True, "deleted": deleted}


@router.post("/backtests/compare")
def compare_backtests(payload: CompareIn) -> dict:
    """多次回测对比：统一换算成累计收益率曲线，指标并排。"""
    return {"items": _compare_items(payload.ids)}


def _compare_items(ids: list[str], *, missing_ok: bool = False) -> list[dict[str, Any]]:
    store = get_services().store
    items: list[dict[str, Any]] = []
    for bt_id in ids:
        body = store.get_blob("backtests", bt_id)
        if body is None:
            if missing_ok:
                continue
            raise HTTPException(404, f"回测记录 {bt_id} 不存在")
        eq = body.get("equity_curve") or []
        base = eq[0] if eq else 1
        items.append(
            {
                "id": bt_id,
                "strategy_name": body.get("strategy_name"),
                "version": (body.get("row") or {}).get("version"),
                "params": body.get("params"),
                "spec": body.get("spec"),
                "metrics": summarize_metrics(body.get("metrics") or {}),
                "timestamps": body.get("timestamps") or [],
                "returns": [v / base - 1 for v in eq] if base else [],
            }
        )
    return items


# ---------------------------------------------------------------------------
# 策略对比批次：几个策略在同一份冻结条件（标的、区间、资金、成本）下各跑一次回测
# ---------------------------------------------------------------------------
class CompareBatchIn(BaseModel):
    name: str = Field("", max_length=60)
    slugs: list[str] = Field(..., min_length=2, max_length=8)
    spec: SpecIn
    stress: bool = Field(False, description="同时跑一组 2 倍手续费与滑点的压力回测")


class CompareResultIn(BaseModel):
    slug: str
    backtest_id: str
    stress: bool = False


def _batch_or_404(batch_id: str) -> dict[str, Any]:
    b = get_services().store.get(Collections.COMPARE_BATCHES, batch_id)
    if b is None:
        raise HTTPException(404, "对比批次不存在")
    return b


@router.post("/compare-batches", summary="创建策略对比批次：冻结标的、区间、资金与成本")
def create_compare_batch(payload: CompareBatchIn) -> dict:
    import hashlib
    import json
    import time
    import uuid

    names = {}
    for slug in dict.fromkeys(payload.slugs):
        names[slug] = _record_or_404(slug).meta.name
    if len(names) < 2:
        raise HTTPException(400, "至少选择两个不同的策略")
    spec = payload.spec.model_dump()
    if not spec.get("end"):  # 冻结结束时间（取整到小时），之后补跑的成员也用同一段行情
        spec["end"] = int(time.time() // 3600 * 3600 * 1000)
    stress_spec = (
        {**spec, "taker_fee": min(spec["taker_fee"] * 2, 0.05), "slippage_bps": min(spec["slippage_bps"] * 2, 500)}
        if payload.stress
        else None
    )
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    doc = {
        "id": f"cmp_{uuid.uuid4().hex[:10]}",
        "name": payload.name or "、".join(list(names.values())[:3]) + (" 等" if len(names) > 3 else ""),
        "members": [{"slug": k, "name": v, "backtest_id": None, "stress_backtest_id": None} for k, v in names.items()],
        "spec": spec,
        "stress_spec": stress_spec,
        "digest": digest,
        "created_at": time.time(),
    }
    get_services().store.upsert(Collections.COMPARE_BATCHES, doc)
    return {"batch": doc}


@router.post("/compare-batches/{batch_id}/results", summary="登记对比批次里某个策略的回测结果")
def add_compare_result(batch_id: str, payload: CompareResultIn) -> dict:
    b = _batch_or_404(batch_id)
    if get_services().store.get_blob("backtests", payload.backtest_id) is None:
        raise HTTPException(404, "回测记录不存在")
    member = next((m for m in b["members"] if m["slug"] == payload.slug), None)
    if member is None:
        raise HTTPException(400, "这个策略不在批次里")
    member["stress_backtest_id" if payload.stress else "backtest_id"] = payload.backtest_id
    get_services().store.upsert(Collections.COMPARE_BATCHES, b)
    return {"batch": b}


@router.get("/compare-batches", summary="策略对比批次列表")
def list_compare_batches(limit: int = Query(50, ge=1, le=200)) -> dict:
    rows = sorted(
        get_services().store.list(Collections.COMPARE_BATCHES), key=lambda b: b.get("created_at", 0), reverse=True
    )
    return {"batches": rows[:limit]}


@router.get("/compare-batches/{batch_id}", summary="策略对比批次详情：曲线与指标并排，含压力回测")
def get_compare_batch(batch_id: str) -> dict:
    b = _batch_or_404(batch_id)
    ids = [m["backtest_id"] for m in b["members"] if m.get("backtest_id")]
    stress = [m["stress_backtest_id"] for m in b["members"] if m.get("stress_backtest_id")]
    return {
        "batch": b,
        "items": _compare_items(ids, missing_ok=True),
        "stress_items": _compare_items(stress, missing_ok=True),
    }


@router.delete("/compare-batches/{batch_id}", summary="删除策略对比批次（回测记录保留）")
def delete_compare_batch(batch_id: str) -> dict:
    _batch_or_404(batch_id)
    return {"ok": get_services().store.delete(Collections.COMPARE_BATCHES, batch_id)}


# ---------------------------------------------------------------------------
# 参数优化
# ---------------------------------------------------------------------------
@router.post("/optimize")
def start_optimize(payload: OptimizeIn) -> dict:
    rec = _record_or_404(payload.slug)
    lang = L.lang_of(rec.meta.kind)
    declared = L.declared_params(lang, rec.source)
    if not declared:
        raise HTTPException(
            400, "策略没有声明可调参数：Python 策略在顶部写 PARAMS = {...}；DSL 策略在 params 中声明并用 $名字 引用"
        )
    space: dict[str, Any] = {}
    if payload.method == "bayes":
        from ..engine.research import declared_space, normalize_space

        try:
            space = normalize_space(payload.space or declared_space(rec.source))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload.grid = {}
    unknown = sorted((set(payload.grid) | set(space)) - set(declared))
    if unknown:
        raise HTTPException(400, f"参数 {', '.join(unknown)} 不在 PARAMS 中")
    issues = L.issues(lang, rec.source)
    if issues:
        raise HTTPException(400, "；".join(issues))
    spec = _spec(payload, default_securities=rec.meta.securities, timeframe=rec.meta.timeframe)
    if lang == L.DSL:
        spec.warmup_bars = max(spec.warmup_bars, L.load(lang, rec.source, name="check").compiled.warmup_bars)
    source, slug, name = rec.source, rec.meta.slug, rec.meta.name
    grid = payload.grid

    # 先做一次参数校验，组合太多等错误立刻返回，而不是在后台任务里才失败
    from ..engine.research import MAX_COMBINATIONS, expand_grid

    if payload.method == "bayes":
        combos = [{}] * payload.n_trials
    else:
        combos = expand_grid(grid)
    if not combos:
        raise HTTPException(400, "参数网格为空：每个参数至少给一个取值")
    if len(combos) > MAX_COMBINATIONS:
        raise HTTPException(400, f"组合数 {len(combos)} 超过上限 {MAX_COMBINATIONS}，请缩小网格")

    version = register_version(rec)
    get_services().versions.require_runnable(version)
    m = {
        "lang": lang,
        "source": source,
        "module_name": slug,
        "name": name,
        "slug": slug,
        "version": rec.meta.version,
        "spec": spec.to_dict(),
        "grid": grid,
        "split": payload.split,
        "objective": payload.objective,
        "combinations": len(combos),
        "embargo_bars": payload.embargo_bars,
        "holdout": payload.holdout,
        "walk_forward_folds": payload.walk_forward_folds,
        "method": payload.method,
        "space": space or None,
        "n_trials": payload.n_trials if payload.method == "bayes" else 0,
        "multi_objective": payload.multi_objective and payload.method == "bayes",
    }
    if lang == L.PYTHON:
        _python_allowed()
    run = enqueue(
        kind="optimize",
        mode="backtest",
        title=f"{'智能调参' if payload.method == 'bayes' else '参数优化'} · {name}",
        manifest=m,
        strategy_version_id=version["id"],
    )
    tasks.dispatch(get_services(), run)
    return {"job_id": run["id"], "run_id": run["id"], "combinations": len(combos)}


@router.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    """兼容旧的“任务进度”接口：返回对应 run 的状态。"""
    from ..platform import context

    run = get_services().runs.get(job_id, workspace_id=context.current_workspace())
    if run is None:
        raise HTTPException(404, "任务不存在")
    status = {"COMPLETED": "done", "FAILED": "failed", "CANCELED": "canceled"}.get(run["state"], "running")
    return {
        "id": run["id"],
        "kind": run["kind"],
        "title": run["title"],
        "status": status,
        "state": run["state"],
        "done": run["progress"]["done"],
        "total": run["progress"]["total"],
        "result": run.get("summary") or None,
        "error": run.get("error") or "",
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
    }


@router.get("/optimizations")
def list_optimizations(slug: str | None = None) -> dict:
    rows = get_services().store.list(Collections.OPTIMIZATIONS)
    if slug:
        rows = [r for r in rows if r.get("slug") == slug]
    return {"optimizations": rows}


@router.get("/optimizations/{opt_id}")
def get_optimization(opt_id: str) -> dict:
    body = get_services().store.get_blob("optimizations", opt_id)
    if body is None:
        raise HTTPException(404, "优化记录不存在")
    return body
