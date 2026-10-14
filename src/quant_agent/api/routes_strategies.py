"""策略与实验（回测）路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from ..agents.guidance import GUIDANCES, render_guidance_prompt
from ..agents.strategy_gen import StrategyGenAgent
from ..store import Collections
from ..strategies.templates import STRATEGY_REGISTRY, describe_templates
from . import tasks
from .deps import get_services
from .routes_runs import enqueue
from .schemas import BacktestIn, GuidanceIn, StrategyGenIn, StrategyVersionIn


def _legacy_notice(response: Response) -> None:
    """统一赛道已并入策略 / 回测页：旧接口保留一个版本，响应头提示将下线。"""
    response.headers["Deprecation"] = "true"
    response.headers["X-Replaced-By"] = "/api/studio/strategies, /api/research/backtests, /api/research/compare-batches"


router = APIRouter(prefix="/api", tags=["策略与实验"], dependencies=[Depends(_legacy_notice)])


# ---------------- 模板与引导 ----------------
@router.get("/templates")
def templates() -> dict:
    return {"templates": describe_templates()}


@router.get("/guidances")
def guidances() -> dict:
    return {
        "guidances": [
            {
                "id": g.id,
                "title": g.title,
                "description": g.description,
                "template_name": g.template_name,
                "prompt_id": g.prompt_id,
                "questions": [q.__dict__ for q in g.questions],
            }
            for g in GUIDANCES.values()
        ]
    }


@router.post("/guidances/render")
def render_guidance(payload: GuidanceIn) -> dict:
    g = GUIDANCES.get(payload.guidance_id)
    if g is None:
        raise HTTPException(404, f"未知引导: {payload.guidance_id}")
    answers = {**g.filled_defaults(), **payload.answers}
    return {
        "guidance_id": g.id,
        "title": g.title,
        "template": g.template_name,
        "prompt_id": g.prompt_id,
        "prompt": render_guidance_prompt(g, answers),
        "answers": answers,
    }


# ---------------- 策略版本 ----------------
@router.get("/strategies")
def list_strategies(limit: int = Query(50, ge=1, le=500)) -> dict:
    s = get_services()
    return {"strategies": s.store.list(Collections.STRATEGIES, limit=limit)}


@router.post("/strategies")
def create_strategy(payload: StrategyVersionIn) -> dict:
    s = get_services()
    if payload.template not in STRATEGY_REGISTRY:
        raise HTTPException(400, f"未知模板: {payload.template}")
    defaults = dict(getattr(STRATEGY_REGISTRY[payload.template], "defaults", {}))
    params = {**defaults, **payload.params}
    version = {
        "name": payload.name,
        "template": payload.template,
        "params": params,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "notes": payload.notes,
        "source": payload.source,
        "code": payload.code,
        # 生命周期：草稿 → 已测试 → 已回测 → …（见 plan.md 第 5 章）
        "status": "draft",
        "human_approved": payload.source != "agent",
        "checks": s.check_strategy_code(payload.code) if payload.code else {"ok": True, "issues": []},
    }
    saved = s.store.upsert(Collections.STRATEGIES, version)
    return {"strategy": saved}


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str) -> dict:
    s = get_services()
    item = s.store.get(Collections.STRATEGIES, strategy_id)
    if item is None:
        raise HTTPException(404, "策略版本不存在")
    return {"strategy": item}


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: str) -> dict:
    s = get_services()
    if not s.store.delete(Collections.STRATEGIES, strategy_id):
        raise HTTPException(404, "策略版本不存在")
    return {"ok": True}


@router.post("/strategies/{strategy_id}/approve")
def approve_strategy(strategy_id: str) -> dict:
    """人工批准：Agent 生成的版本必须批准后才能进入实时模拟（平台红线）。"""
    s = get_services()
    item = s.store.get(Collections.STRATEGIES, strategy_id)
    if item is None:
        raise HTTPException(404, "策略版本不存在")
    item["human_approved"] = True
    item["status"] = "tested"
    return {"strategy": s.store.upsert(Collections.STRATEGIES, item)}


# ---------------- AI 生成策略 ----------------
@router.post("/strategies/generate")
def generate_strategy(payload: StrategyGenIn) -> dict:
    """策略生成 Agent：自然语言 → 候选策略版本（draft，需人工批准）。"""
    s = get_services()
    agent = StrategyGenAgent(
        s.router,
        template_search=lambda q: s.search_templates(q),
        sandbox_check=lambda code: s.check_strategy_code(code),
    )
    result = agent.generate(payload.description, symbol=payload.symbol, timeframe=payload.timeframe)
    record = {
        "name": result.name,
        "template": "generated",
        "params": result.params,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "code": result.code,
        "logic": result.logic,
        "risks": result.risks,
        "clarifications": result.clarifications,
        "prompt_hash": result.prompt_hash,
        "trace_id": result.trace_id,
        "source": "agent",
        "status": "draft",
        "human_approved": False,
        "checks": s.check_strategy_code(result.code),
    }
    saved = s.store.upsert(Collections.STRATEGIES, record)
    return {"strategy": saved, "clarifications": result.clarifications}


# ---------------- 回测 ----------------
@router.post("/backtests")
def run_backtest(payload: BacktestIn) -> Any:
    """平台内置模板的快速回测（含四档压力场景）。同样登记为 run 交给 worker 执行。"""
    if payload.strategy not in STRATEGY_REGISTRY:
        raise HTTPException(400, f"未知策略模板: {payload.strategy}")
    s = get_services()
    run = enqueue(
        kind="template_backtest",
        mode="backtest",
        title=f"模板回测 · {payload.strategy}",
        manifest={"payload": payload.model_dump()},
    )
    run = tasks.dispatch(s, run, wait_seconds=60)
    tasks.raise_if_failed(run)
    if run["state"] != "COMPLETED":
        return JSONResponse(status_code=202, content={"run_id": run["id"], "state": run["state"]})
    return s.store.get_blob("run_results", run["id"]) or {}


@router.get("/template-runs")
def list_runs(limit: int = Query(50, ge=1, le=500)) -> dict:
    s = get_services()
    runs = s.store.list(Collections.RUNS, limit=limit)
    return {
        "runs": [
            {
                "id": r.get("id"),
                "strategy": r.get("strategy"),
                "symbol": r.get("symbol"),
                "timeframe": r.get("timeframe"),
                "created_at": r.get("created_at"),
                "data_source": r.get("data_source"),
                "scenario": r.get("scenario"),
                "metrics": r.get("metrics", {}),
            }
            for r in runs
        ]
    }


@router.get("/template-runs/{run_id}")
def get_run(run_id: str) -> dict:
    s = get_services()
    run = s.store.get(Collections.RUNS, run_id)
    if run is None:
        raise HTTPException(404, "实验不存在")
    return {"run": run}


@router.delete("/template-runs/{run_id}")
def delete_run(run_id: str) -> dict:
    s = get_services()
    if not s.store.delete(Collections.RUNS, run_id):
        raise HTTPException(404, "实验不存在")
    return {"ok": True}


# ---------------- 旧版统一赛道策略 → 策略文件 ----------------
def _strategy_repo():
    from ..strategies.repo import StrategyRepository

    return StrategyRepository(get_services().store.root / "strategies")


@router.get("/strategies-migration", summary="旧版统一赛道策略：还有多少条待转换成策略文件")
def migration_status() -> dict:
    from ..strategies.migrate import pending

    rows = pending(get_services().store)
    return {
        "pending": [
            {"id": r.get("id"), "name": r.get("name"), "kind": r.get("kind") or r.get("template")} for r in rows
        ]
    }


@router.post("/strategies-migration", summary="把旧版统一赛道策略转换成策略文件（原记录保留，可重复执行）")
def migrate_strategies() -> dict:
    from ..strategies.migrate import migrate_all

    s = get_services()
    results = migrate_all(s.store, _strategy_repo())
    s.audit.record(
        "strategy.migrate",
        object_type="strategy",
        object_id="legacy",
        details={"converted": sum(1 for r in results if r["ok"]), "total": len(results)},
    )
    return {"results": results}
