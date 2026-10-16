"""上线验证：仿真真实度报告、毕业闸门、AI 值守员、压力演练。

这些都只读实验数据、跑模拟，不会下任何真实订单。晋级和应用校准值都要人点确认，并写审计记录。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..data.quality import TIMEFRAME_MS
from ..engine import fidelity as FD
from ..platform import context
from ..research import graduation as GR
from ..research import watcher as W
from ..store import Collections
from .deps import get_services

router = APIRouter(prefix="/api/validation", tags=["上线验证"])


def _user() -> str:
    return context.current().user_id


def _repo():
    from ..strategies.repo import StrategyRepository

    return StrategyRepository(get_services().store.root / "strategies")


def _llm():
    from .routes_datahub import _llm as llm

    return llm()


# ---------------------------------------------------------------------------
# 仿真真实度
# ---------------------------------------------------------------------------
class FidelityIn(BaseModel):
    session_ids: list[str] = Field(..., min_length=1, max_length=3)


@router.post("/fidelity", summary="生成仿真真实度报告：在实验跑过的行情上重跑回测，逐项拆解收益差并给出成本校准建议")
def make_fidelity(payload: FidelityIn) -> dict:
    try:
        rep = FD.build_report(get_services(), payload.session_ids, user=_user())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"report": rep}


@router.get("/fidelity", summary="真实度报告列表（可按策略过滤）")
def list_fidelity(strategy: str | None = None, limit: int = 30) -> dict:
    rows = get_services().store.list(FD.REPORTS)
    if strategy:
        rows = [r for r in rows if r.get("strategy_id") == strategy]
    rows.sort(key=lambda r: -(r.get("created_at") or 0))
    return {
        "reports": [
            {
                k: r.get(k)
                for k in ("id", "strategy_id", "strategy_name", "sessions", "window", "deviation", "created_at")
            }
            for r in rows[:limit]
        ]
    }


@router.get("/fidelity/{rid}", summary="一份真实度报告")
def get_fidelity(rid: str) -> dict:
    rep = get_services().store.get(FD.REPORTS, rid)
    if rep is None:
        raise HTTPException(404, "报告不存在")
    return {"report": rep}


class CalibIn(BaseModel):
    confirm: bool = False


@router.post("/fidelity/{rid}/calibration", summary="把报告里的校准建议设为这家交易所的回测成本（需确认）")
def apply_calibration(rid: str, payload: CalibIn) -> dict:
    s = get_services()
    rep = s.store.get(FD.REPORTS, rid)
    if rep is None:
        raise HTTPException(404, "报告不存在")
    sugg = (rep.get("calibration") or {}).get("suggested")
    if not sugg:
        raise HTTPException(400, "这份报告没有校准建议（需要交易所模拟盘成交）")
    if not payload.confirm:
        raise HTTPException(400, "应用校准值需要确认")
    ex = next((x.get("exchange") for x in rep.get("sessions") or [] if x.get("execution") == "demo"), None) or "binance"
    rec = FD.apply_calibration(s.store, exchange=ex, suggested=sugg, report_id=rid, user=_user())
    s.audit.record("validation.calibration", object_type="fidelity_report", object_id=rid, details=rec)
    return {"calibration": rec}


@router.get("/calibration", summary="各交易所校准过的回测成本（回测表单里可以一键套用）")
def calibrations() -> dict:
    return {"calibrations": get_services().store.list(FD.CALIBRATIONS)}


# ---------------------------------------------------------------------------
# 毕业闸门
# ---------------------------------------------------------------------------
@router.get("/graduation", summary="所有策略的阶段：回测 → 纸面模拟 → 模拟盘 → 毕业")
def graduation_board() -> dict:
    s = get_services()
    recs = [(r.meta.slug, r.meta.name) for r in _repo().list()]
    return {
        "board": GR.board(s.store, recs),
        "config": GR.config(s.store),
        "stages": GR.STAGE_LABEL,
        "live_trading": "closed",
    }


@router.get("/graduation/{slug}", summary="一个策略的当前阶段与晋级检查")
def graduation_detail(slug: str) -> dict:
    if _repo().get(slug) is None:
        raise HTTPException(404, "策略不存在")
    return GR.evaluate(get_services().store, slug)


class PromoteIn(BaseModel):
    confirm: bool = False
    note: str = Field("", max_length=300)


@router.post("/graduation/{slug}/promote", summary="晋级到下一阶段（全部检查通过 + 你确认）")
def graduation_promote(slug: str, payload: PromoteIn) -> dict:
    s = get_services()
    if _repo().get(slug) is None:
        raise HTTPException(404, "策略不存在")
    try:
        out = GR.promote(s.store, slug, confirm=payload.confirm, user=_user(), note=payload.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s.audit.record("validation.promote", object_type="strategy", object_id=slug, details={"stage": out["stage"]})
    return out


class DemoteIn(BaseModel):
    to: str
    note: str = Field("", max_length=300)


@router.post("/graduation/{slug}/demote", summary="退回到更早的阶段")
def graduation_demote(slug: str, payload: DemoteIn) -> dict:
    s = get_services()
    try:
        out = GR.demote(s.store, slug, to=payload.to, user=_user(), note=payload.note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s.audit.record("validation.demote", object_type="strategy", object_id=slug, details={"stage": out["stage"]})
    return out


class GradConfigIn(BaseModel):
    min_dsr: float | None = Field(None, ge=0, le=1)
    max_drawdown_backtest: float | None = Field(None, gt=0, le=1)
    paper_days: float | None = Field(None, ge=1, le=90)
    demo_days: float | None = Field(None, ge=1, le=180)
    max_drawdown_live: float | None = Field(None, gt=0, le=1)
    max_deviation_paper_bps: float | None = Field(None, gt=0, le=5000)
    max_deviation_demo_bps: float | None = Field(None, gt=0, le=5000)


@router.post("/graduation-config", summary="修改闸门门槛（部署管理员）")
def graduation_config(payload: GradConfigIn) -> dict:
    if not context.current().is_admin:
        raise HTTPException(403, "只有部署管理员可以修改闸门门槛")
    return {"config": GR.save_config(get_services().store, payload.model_dump())}


# ---------------------------------------------------------------------------
# AI 值守员
# ---------------------------------------------------------------------------
def watch_session(session_id: str, *, use_llm: bool = True) -> dict[str, Any]:
    from ..strategies.package import default_spec

    s = get_services()
    row = s.store.get(Collections.PAPER_SESSIONS, session_id)
    if row is None:
        raise ValueError("实验不存在")
    repo = _repo()
    spec = ""
    rec = repo.get(row.get("strategy_id") or "")
    if rec is not None:
        spec = repo.read_spec(rec.meta.slug) or default_spec(
            name=rec.meta.name,
            notes=rec.meta.notes or "",
            securities=rec.meta.securities or [],
            timeframe=rec.meta.timeframe,
            params=row.get("params") or {},
            source=rec.source,
        )
    return W.review(
        s.store,
        row,
        spec,
        llm=_llm() if use_llm else None,
        bar_ms=TIMEFRAME_MS.get(row.get("timeframe") or "1h", 3_600_000),
    )


@router.post("/watcher/{session_id}", summary="让值守员现在看一遍这个实验：逐笔解释成交、标出和说明书矛盾的行为")
def watcher_run(session_id: str, llm: bool = True) -> dict:
    try:
        return {"review": watch_session(session_id, use_llm=llm)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/watcher/{session_id}", summary="值守员对这个实验的最近一次结论")
def watcher_get(session_id: str) -> dict:
    return {"review": get_services().store.get(W.REVIEWS, session_id)}


@router.get("/watcher", summary="所有实验的值守结论摘要")
def watcher_list() -> dict:
    rows = get_services().store.list(W.REVIEWS)
    return {
        "reviews": [
            {
                k: r.get(k)
                for k in ("id", "strategy_name", "execution", "has_spec", "reviewed_at", "fills_seen", "tagger")
            }
            | {"flags": (r.get("flags") or [])[-5:], "flag_count": len(r.get("flags") or [])}
            for r in sorted(rows, key=lambda r: -(r.get("reviewed_at") or 0))
        ]
    }


# ---------------------------------------------------------------------------
# 压力演练：后台线程里先取行情（有网络），再在隔离子进程里跑策略
# ---------------------------------------------------------------------------
STRESS_RUNS = "stress_runs"


@router.get("/stress/scenarios", summary="压力演练的历史极端日")
def stress_scenarios() -> dict:
    from ..engine.stress import SCENARIOS

    return {"scenarios": SCENARIOS}


class StressIn(BaseModel):
    slug: str
    scenarios: list[str] | None = None
    faults: bool = True
    timeframe: str | None = None
    initial_cash: float = Field(10_000, gt=0, le=1e9)


def _stress_job(ws: str, user: str, run_id: str, req: dict[str, Any]) -> None:
    import time as _t

    from ..engine import stress as ST
    from ..platform import isolation

    with context.bind(ws, user_id=user):
        store = get_services().store

        def save(**kw: Any) -> None:
            cur = store.get(STRESS_RUNS, run_id) or {"id": run_id}
            store.upsert(STRESS_RUNS, {**cur, **kw})

        try:
            save(progress="正在准备历史行情")
            data = ST.prepare_data(
                get_services(),
                source=req["source"],
                securities=req["securities"],
                timeframe=req["timeframe"],
                scenarios=req.get("scenarios"),
            )
            save(progress="开始回放")
            last = [0.0]

            def on_line(msg: dict[str, Any]) -> None:
                p = msg.get("progress")
                if isinstance(p, dict) and _t.monotonic() - last[0] > 1.0:
                    last[0] = _t.monotonic()
                    save(progress=str(p.get("message") or ""), fraction=p.get("fraction"))

            report = isolation.run_batch({"op": "stress", **req, "data": data}, on_line=on_line, timeout=900)
            if isinstance(report, dict) and report.get("error"):
                raise RuntimeError(report["error"])
            save(status="done", report=report, progress="", finished_at=int(_t.time() * 1000))
        except Exception as exc:
            save(status="failed", error=f"{type(exc).__name__}: {exc}"[:400], finished_at=int(_t.time() * 1000))


@router.post("/stress", summary="开始一次压力演练（后台运行，轮询结果）")
def stress_start(payload: StressIn) -> dict:
    import threading
    import time as _t
    import uuid

    from ..strategies import lang as L

    s = get_services()
    rec = _repo().get(payload.slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    if any(r.get("status") == "running" and r.get("strategy_id") == payload.slug for r in s.store.list(STRESS_RUNS)):
        raise HTTPException(409, "这个策略的演练还在进行")
    tf = payload.timeframe or rec.meta.timeframe or "1h"
    if tf not in TIMEFRAME_MS or TIMEFRAME_MS[tf] > 86_400_000:
        tf = "1h"
    req = {
        "source": rec.source,
        "lang": L.lang_of(rec.meta.kind),
        "params": None,
        "securities": rec.meta.securities or None,
        "timeframe": tf,
        "initial_cash": payload.initial_cash,
        "scenarios": payload.scenarios,
        "faults": None
        if payload.faults
        else {"disconnects": [], "reject_rate": 0, "base_reject_rate": 0, "delay_rate": 0, "partial_rate": 0},
    }
    run = {
        "id": f"st_{uuid.uuid4().hex[:10]}",
        "strategy_id": rec.meta.slug,
        "strategy_name": rec.meta.name,
        "status": "running",
        "progress": "排队中",
        "faults": payload.faults,
        "timeframe": tf,
        "created_at": int(_t.time() * 1000),
        "created_by": _user(),
    }
    s.store.upsert(STRESS_RUNS, run)
    s.audit.record("validation.stress", object_type="strategy", object_id=rec.meta.slug, details={"run": run["id"]})
    threading.Thread(
        target=_stress_job,
        args=(context.current_workspace(), _user(), run["id"], req),
        daemon=True,
        name=f"stress-{run['id']}",
    ).start()
    return {"run": run}


@router.get("/stress", summary="压力演练记录（不含曲线）")
def stress_list() -> dict:
    rows = sorted(get_services().store.list(STRESS_RUNS), key=lambda r: -(r.get("created_at") or 0))[:30]
    out = []
    for r in rows:
        rep = r.get("report") or {}
        out.append(
            {
                **{k: v for k, v in r.items() if k != "report"},
                "report": {"summary": rep.get("summary"), "passed": (rep.get("summary") or {}).get("passed")}
                if rep
                else None,
            }
        )
    return {"runs": out}


@router.get("/stress/{rid}", summary="一次压力演练的完整结果")
def stress_get(rid: str) -> dict:
    r = get_services().store.get(STRESS_RUNS, rid)
    if r is None:
        raise HTTPException(404, "记录不存在")
    return {"run": r}


# ---------------------------------------------------------------------------
# 策略体检单：未来函数、预热、成本翻倍、参数平台 + AI 红队。后台线程取数，隔离子进程跑策略
# ---------------------------------------------------------------------------
CHECKUPS = "checkups"


class CheckupIn(BaseModel):
    slug: str
    securities: list[str] | None = None
    timeframe: str | None = None
    bars_limit: int = Field(1500, ge=300, le=5000)
    use_llm: bool = True


def source_hash(source: str) -> str:
    import hashlib

    return hashlib.sha256(source.encode()).hexdigest()[:16]


def _checkup_job(ws: str, user: str, cid: str, req: dict[str, Any]) -> None:
    import time as _t

    from ..agents import redteam
    from ..engine import checkup as CK
    from ..engine.research import BacktestSpec, load_market
    from ..platform import isolation
    from ..platform.sandbox_exec import bars_to_wire

    with context.bind(ws, user_id=user):
        s = get_services()
        store = s.store

        def save(**kw: Any) -> None:
            cur = store.get(CHECKUPS, cid) or {"id": cid}
            store.upsert(CHECKUPS, {**cur, **kw})

        try:
            save(progress="正在取行情")
            spec = BacktestSpec(securities=req["securities"], timeframe=req["timeframe"], bars_limit=req["bars_limit"])
            bars, datasets, warnings = load_market(s.market, spec)
            research, by_cut = None, None
            src = req["source"]
            if req["lang"] == "python" and any(f"{k}(" in src for k in ("factor", "predict", "optimize")):
                # 研究数据也要按截断后的数据重算，才能查出因子 / 模型本身的未来函数
                from ..research.datahub import DataHub
                from ..research.signals import prepare

                save(progress="正在按截断点重算研究数据")
                hub = DataHub(store, secrets_fn=getattr(s, "read_secrets", None))
                research, w = prepare(src, bars, spec.timeframe, store=store, hub=hub, data_source=spec.source)
                warnings += w
                by_cut = {
                    str(ts): prepare(
                        src, CK._upto(bars, ts), spec.timeframe, store=store, hub=hub, data_source=spec.source
                    )[0]
                    for ts in CK.cut_points(bars, spec.warmup_bars)
                }
            last = [0.0]

            def on_line(msg: dict[str, Any]) -> None:
                p = msg.get("progress")
                if isinstance(p, dict) and _t.monotonic() - last[0] > 0.5:
                    last[0] = _t.monotonic()
                    save(progress=f"正在检查：{p.get('label')}（{p.get('done')}/{p.get('total')}）")

            save(progress="开始体检")
            rep = isolation.run_batch(
                {
                    "op": "checkup",
                    "source": src,
                    "lang": req["lang"],
                    "name": req["slug"],
                    "spec": spec.to_dict(),
                    "bars": bars_to_wire(bars),
                    "research": research,
                    "research_by_cut": by_cut,
                },
                on_line=on_line,
                timeout=1200,
            )
            if isinstance(rep, dict) and rep.get("error"):
                raise RuntimeError(rep["error"])
            save(progress="红队审查中")
            chat = None
            if req.get("use_llm"):
                call = _llm()
                if call is not None:
                    chat = call
            red = redteam.critique(src, rep, rep.get("metrics") or {}, chat=chat)
            from ..research.graduation import sample_window

            starts = [b[0].open_time for b in bars.values() if b]
            ends = [b[-1].close_time for b in bars.values() if b]
            save(
                status="done",
                # 体检自己的样本区间：与晋级关卡引用的回测不是同一段时，成绩不能互相印证
                sample=sample_window(min(starts, default=0), max(ends, default=0), spec.timeframe)
                + f" · {spec.bars_limit} 根",
                report=rep,
                redteam=red,
                overall=rep["overall"],
                datasets=datasets,
                warnings=warnings,
                progress="",
                finished_at=int(_t.time() * 1000),
            )
        except Exception as exc:
            save(
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:400],
                progress="",
                finished_at=int(_t.time() * 1000),
            )


@router.post("/checkup", summary="开始一次策略体检（后台运行，轮询结果）")
def checkup_start(payload: CheckupIn) -> dict:
    import threading
    import time as _t
    import uuid

    from ..strategies import lang as L

    s = get_services()
    rec = _repo().get(payload.slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    if any(r.get("status") == "running" and r.get("strategy_id") == payload.slug for r in s.store.list(CHECKUPS)):
        raise HTTPException(409, "这个策略的体检还在进行")
    lang = L.lang_of(rec.meta.kind)
    if lang == L.PYTHON:
        from .routes_research import _python_allowed

        _python_allowed()
    tf = payload.timeframe or rec.meta.timeframe or "1h"
    if tf not in TIMEFRAME_MS:
        tf = "1h"
    req = {
        "source": rec.source,
        "lang": lang,
        "slug": rec.meta.slug,
        "timeframe": tf,
        "securities": payload.securities or rec.meta.securities or ["BTC/USDT"],
        "bars_limit": payload.bars_limit,
        "use_llm": payload.use_llm,
    }
    run = {
        "id": f"ck_{uuid.uuid4().hex[:10]}",
        "strategy_id": rec.meta.slug,
        "strategy_name": rec.meta.name,
        "version": rec.meta.version,
        "source_hash": source_hash(rec.source),
        "status": "running",
        "progress": "排队中",
        "timeframe": tf,
        "securities": req["securities"],
        "created_at": int(_t.time() * 1000),
        "created_by": _user(),
    }
    s.store.upsert(CHECKUPS, run)
    s.audit.record("validation.checkup", object_type="strategy", object_id=rec.meta.slug, details={"run": run["id"]})
    threading.Thread(
        target=_checkup_job,
        args=(context.current_workspace(), _user(), run["id"], req),
        daemon=True,
        name=f"checkup-{run['id']}",
    ).start()
    return {"run": run}


@router.get("/checkup", summary="体检记录（不含明细）")
def checkup_list(slug: str | None = None) -> dict:
    rows = [r for r in get_services().store.list(CHECKUPS) if not slug or r.get("strategy_id") == slug]
    rows.sort(key=lambda r: -(r.get("created_at") or 0))
    out = []
    for r in rows[:40]:
        rep = r.get("report") or {}
        out.append(
            {
                **{k: v for k, v in r.items() if k not in ("report", "redteam")},
                "items": [{k: i.get(k) for k in ("id", "title", "verdict", "summary")} for i in rep.get("items") or []],
            }
        )
    return {"runs": out}


@router.get("/checkup/{cid}", summary="一次体检的完整结果")
def checkup_get(cid: str) -> dict:
    r = get_services().store.get(CHECKUPS, cid)
    if r is None:
        raise HTTPException(404, "记录不存在")
    rec = _repo().get(r.get("strategy_id") or "")
    return {"run": r, "stale": bool(rec and source_hash(rec.source) != r.get("source_hash"))}
