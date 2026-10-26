"""模型库 API：训练（实验）、实验记录与对比、模型库增删改、模型卡、健康度、最新预测。

训练在后台线程执行（``wait=true`` 时同步，测试与脚本用），可以取消；进度写在实验记录上，前端轮询。
每次训练都记一笔实验，样本外指标、学习曲线、泄漏检查、重要性、漂移都在报告里。
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import backtest_retention as BR
from ..platform import context
from ..research import goals as RG
from ..research import learners as L
from ..research import models as M
from ..research.datahub import DataHub
from ..research.factor_lib import FactorLibrary
from ..research.model_lib import STATUS_LABELS, ModelLibrary
from ..research.panel import UNIVERSES, PanelError, build_panel
from .deps import get_services

router = APIRouter(prefix="/api/models", tags=["模型库"])

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
_JOBS: dict[str, dict[str, Any]] = {}  # 训练任务：进度与取消标记（进程内）
_JOBS_LOCK = threading.Lock()


class TrainIn(BaseModel):
    model: str = "ridge"
    label: str = "fwd_ret"
    horizon: int = Field(1, ge=1, le=60)
    features: list[dict[str, Any] | str] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    universe: str = "major10"
    symbols: list[str] = Field(default_factory=list)
    timeframe: Timeframe = "4h"
    bars_limit: int = Field(1500, ge=300, le=20_000)
    start: int | None = None
    end: int | None = None
    source: Literal["auto", "exchange", "synthetic"] = "auto"
    cost_bps: float = Field(10.0, ge=0, le=200)
    folds: int = Field(4, ge=2, le=8)
    seed: int = 7
    barrier_k: float = Field(1.5, gt=0, le=10)
    shuffle_test: bool = True
    note: str = Field("", max_length=200)
    wait: bool = False


class SaveIn(BaseModel):
    run_id: str
    name: str = Field("", max_length=40)
    status: Literal["candidate", "active", "decaying", "retired"] = "candidate"
    note: str = Field("", max_length=400)
    model_id: str | None = None


class PatchIn(BaseModel):
    name: str | None = Field(None, max_length=40)
    status: Literal["candidate", "active", "decaying", "retired"] | None = None
    note: str | None = Field(None, max_length=400)
    frozen: bool | None = None


class RefreshIn(BaseModel):
    source: Literal["auto", "exchange", "synthetic"] = "auto"
    bars_limit: int = Field(1500, ge=300, le=20_000)


def _lib() -> ModelLibrary:
    return ModelLibrary(get_services().store)


def _factors() -> FactorLibrary:
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


def _resolve(ref: str) -> dict[str, Any] | None:
    lib = _factors()
    return lib.get(ref) or lib.by_name(ref)


def _panel(spec: dict[str, Any]):
    return build_panel(
        get_services().market,
        _hub(),
        spec["symbols"],
        spec["timeframe"],
        fields=M.fields_for(spec),
        start=spec.get("start"),
        end=spec.get("end"),
        bars_limit=spec["bars_limit"],
        source=spec["source"],
    )


# ---------------------------------------------------------------------------
@router.get("/meta", summary="模型研究元信息：模型菜单、标签、默认特征、后端是否可用、目标指标")
def models_meta() -> dict:
    return {
        "learners": [
            {
                "id": k,
                "label": v["label"],
                "hint": v["hint"],
                "family": v["family"],
                "params": v["params"],
                "available": L.available()[k],
                "needs": v.get("needs"),
            }
            for k, v in L.LEARNERS.items()
        ],
        "labels": [{"id": k, **v} for k, v in M.LABELS.items()],
        "default_features": M.DEFAULT_FEATURES,
        "backends": L.backends(),
        "universes": [{"id": k, "name": v["name"], "symbols": v["symbols"]} for k, v in UNIVERSES.items()],
        "statuses": STATUS_LABELS,
        "goal": RG.MODEL.meta(),
        "runs": _lib().trial_count(),
    }


def _train_job(jid: str, spec: dict[str, Any], note: str, created_by: str) -> None:
    job = _JOBS[jid]

    def stop() -> None:
        if job.get("cancel"):
            raise M.Stopped("已取消")

    def progress(text: str) -> None:
        job["phase"] = text

    try:
        job["phase"] = "取数据"
        panel = _panel(spec)
        lib = _lib()
        out = M.train_and_evaluate(spec, panel, stop=stop, progress=progress, trials=lib.trial_count() + 1)
        rec = lib.record_run(spec, out["report"], out["state"], source="models", note=note, created_by=created_by)
        job.update(status="done", run_id=rec["id"], phase="完成")
    except M.Stopped:
        job.update(status="cancelled", phase="已取消")
    except (M.ModelError, L.LearnerError, PanelError) as exc:
        job.update(status="failed", error=str(exc), phase="失败")
    except Exception as exc:  # pragma: no cover - 兜底，避免线程静默死掉
        job.update(status="failed", error=f"{type(exc).__name__}: {exc}", phase="失败")
    finally:
        job["finished_at"] = time.time()


@router.post("/train", summary="训练一个模型（一次实验）：滚动样本外评估、基线对照、泄漏检查。wait=false 时后台执行")
def train(payload: TrainIn) -> dict:
    raw = payload.model_dump(exclude={"wait", "note", "universe"})
    raw["symbols"] = _symbols(payload.universe, payload.symbols)
    try:
        spec = M.normalize_spec(raw, resolve=_resolve)
        L.make_learner(spec["model"], spec["params"], M.LABELS[spec["label"]]["task"])
    except (M.ModelError, L.LearnerError) as exc:
        raise HTTPException(400, str(exc)) from exc
    services = get_services()
    app_ctx = context.current()
    user_id = app_ctx.user_id or "user"
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        app_ctx.user_id or "",
        app_ctx.role,
        "model_run",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), app_ctx.user_id or "", app_ctx.role
        ),
    )
    jid = f"j_{uuid.uuid4().hex[:10]}"
    with _JOBS_LOCK:
        running = [j for j in _JOBS.values() if j["status"] == "running"]
        if len(running) >= 2:
            raise HTTPException(409, "已经有两个训练在进行，请等它们结束")
        _JOBS[jid] = {"id": jid, "status": "running", "phase": "排队", "spec": spec, "started_at": time.time()}
    if payload.wait:
        _train_job(jid, spec, payload.note, user_id)
    else:
        run_ctx = contextvars.copy_context()
        threading.Thread(
            target=run_ctx.run, args=(_train_job, jid, spec, payload.note, user_id), daemon=True, name=f"train-{jid}"
        ).start()
    return {"job": _job_view(jid)}


def _job_view(jid: str) -> dict[str, Any]:
    j = _JOBS.get(jid)
    if j is None:
        raise HTTPException(404, "训练任务不存在（服务重启后任务记录会清空，结果在实验记录里）")
    out = {k: v for k, v in j.items() if k != "spec"}
    out["model"] = j["spec"]["model"]
    out["label"] = j["spec"]["label"]
    if j.get("run_id"):
        out["run"] = _lib().run(j["run_id"])
    return out


@router.get("/jobs/{jid}", summary="训练进度")
def job_status(jid: str) -> dict:
    return {"job": _job_view(jid)}


@router.post("/jobs/{jid}/cancel", summary="取消训练（在下一个检查点停下）")
def job_cancel(jid: str) -> dict:
    j = _JOBS.get(jid)
    if j is None:
        raise HTTPException(404, "训练任务不存在")
    j["cancel"] = True
    return {"job": _job_view(jid)}


@router.get("/runs", summary="实验记录（每次训练一笔）")
def list_runs(limit: int = 200, project_id: str | None = None) -> dict:
    lib = _lib()
    running = [_job_view(j["id"]) for j in _JOBS.values() if j["status"] == "running"]
    return {
        "runs": lib.runs(max(1, min(limit, 500)), project_id=project_id),
        "running": running,
        "trials": lib.trial_count(),
    }


@router.get("/runs/{rid}", summary="实验详情：完整报告")
def get_run(rid: str) -> dict:
    lib = _lib()
    run = lib.run(rid)
    if run is None:
        raise HTTPException(404, "实验不存在")
    return {"run": run, "report": lib.run_report(rid)}


@router.delete("/runs/{rid}", summary="删除实验记录（已存入模型库的模型不受影响）")
def delete_run(rid: str) -> dict:
    return {"ok": _lib().delete_run(rid)}


@router.get("/compare", summary="两次实验对比：规格差异与指标差异")
def compare_runs(a: str, b: str) -> dict:
    lib = _lib()
    ra, rb = lib.run_report(a), lib.run_report(b)
    if ra is None or rb is None:
        raise HTTPException(404, "实验不存在")
    keys = [
        "oos_rank_ic",
        "baseline_rank_ic",
        "beat_baseline",
        "oos_icir",
        "dir_acc",
        "brier",
        "ls_sharpe",
        "turnover",
        "fold_positive",
        "psi_max",
        "leak_passed",
        "samples",
        "seconds",
    ]
    sa, sb = ra.get("summary") or {}, rb.get("summary") or {}
    spec_keys = ["model", "label", "horizon", "params", "folds", "seed", "timeframe"]
    return {
        "metrics": [{"key": k, "a": sa.get(k), "b": sb.get(k)} for k in keys],
        "spec": [{"key": k, "a": (ra.get("spec") or {}).get(k), "b": (rb.get("spec") or {}).get(k)} for k in spec_keys],
        "features": {
            "a": [f["name"] for f in (ra.get("spec") or {}).get("features", [])],
            "b": [f["name"] for f in (rb.get("spec") or {}).get("features", [])],
        },
        "curves": {"a": ra.get("curve"), "b": rb.get("curve")},
        "folds": {"a": ra.get("folds"), "b": rb.get("folds")},
    }


@router.get("", summary="模型库列表（status 过滤）")
def list_models(status: str | None = None) -> dict:
    lib = _lib()
    return {"models": lib.list(status), "runs": lib.trial_count()}


@router.post("", summary="把一次实验存为模型（带模型卡）")
def save_model(payload: SaveIn) -> dict:
    services = get_services()
    ctx = context.current()
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        ctx.user_id or "",
        ctx.role,
        "model",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), ctx.user_id or "", ctx.role
        ),
    )
    lib = _lib()
    try:
        rec = lib.save_from_run(
            payload.run_id,
            name=payload.name,
            status=payload.status,
            note=payload.note,
            model_id=payload.model_id,
            created_by=ctx.user_id or "user",
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"model": rec}


@router.get("/{mid}", summary="模型详情（含模型卡）")
def get_model(mid: str) -> dict:
    rec = _lib().get(mid)
    if rec is None:
        raise HTTPException(404, "模型不存在")
    return {"model": rec}


@router.get("/{mid}/report", summary="模型的完整训练报告")
def model_report(mid: str) -> dict:
    lib = _lib()
    if lib.get(mid) is None:
        raise HTTPException(404, "模型不存在")
    return {"report": lib.report(mid)}


@router.patch("/{mid}", summary="修改名称、状态、备注；冻结后不能再被覆盖")
def patch_model(mid: str, payload: PatchIn) -> dict:
    try:
        rec = _lib().patch(mid, **payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if rec is None:
        raise HTTPException(404, "模型不存在")
    return {"model": rec}


@router.delete("/{mid}", summary="删除模型（实验记录保留）")
def delete_model(mid: str) -> dict:
    lib = _lib()
    if lib.get(mid) is None:
        raise HTTPException(404, "模型不存在")
    return {"ok": lib.delete(mid)}


@router.post("/{mid}/refresh", summary="健康度：用冻结参数在最新数据上预测，记录训练区间之后的 RankIC 与特征漂移")
def refresh_model(mid: str, payload: RefreshIn) -> dict:
    lib = _lib()
    rec = lib.get(mid)
    if rec is None:
        raise HTTPException(404, "模型不存在")
    spec = {**rec["spec"], "bars_limit": payload.bars_limit, "start": None, "end": None, "source": payload.source}
    try:
        panel = _panel(spec)
        out = lib.health(mid, panel)
    except (PanelError, M.ModelError, L.LearnerError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"model": lib.get(mid), **out}
