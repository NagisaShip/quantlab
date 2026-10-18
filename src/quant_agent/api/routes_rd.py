"""研究纪律与自动化 API。

试验台账（DSR / PBO）、紧封测试集、血缘图、夜班、AI 晨报、积木式组装、影子运行、研究流水线摘要。
"""

from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import context
from ..research import assemble as AS
from ..research import brief as BR
from ..research import discipline as D
from ..research import lineage as LN
from ..research import nightshift as NS
from ..research import shadow as SH
from ..timeframes import is_timeframe
from .deps import get_services

router = APIRouter(prefix="/api/rd", tags=["研究纪律与自动化"])


def _store() -> Any:
    return get_services().store


def _user() -> str:
    """留痕用的显示名：优先昵称，其次邮箱（不写内部 id，界面上看不懂）。"""
    c = context.current()
    name = c.user_name if c.user_name and c.user_name != c.user_id else ""
    return name or getattr(c, "user_email", "") or c.user_id or "user"


def _lab() -> Any:
    from .routes_lab import _lab

    return _lab()


def _titles() -> dict[str, str]:
    try:
        return {p["id"]: p.get("title") or p.get("idea", "")[:30] or p["id"] for p in _lab().store.list()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 试验台账
# ---------------------------------------------------------------------------
@router.get("/ledger", summary="试验台账：各研究类型的试验次数，每个研究方向的最好成绩按试验次数修正（DSR / t 门槛）")
def ledger(domain: str | None = None) -> dict:
    out = D.ledger(_store(), project_titles=_titles())
    if domain:
        out["families"] = [f for f in out["families"] if f["domain"] == domain]
        out["recent"] = [r for r in out["recent"] if r["domain"] == domain]
    out["holdout"] = _holdout_view()
    return out


class PboIn(BaseModel):
    project_id: str | None = None
    slug: str | None = None
    splits: int = Field(10, ge=4, le=16)


def _project_series(pid: str) -> tuple[dict[str, list[tuple[int, float]]], dict[str, float], str]:
    from .routes_lab import _lab_for

    lab = _lab_for(pid)
    p = lab.store.get(pid)
    if p is None:
        raise HTTPException(404, "研究项目不存在")
    kind = p.get("kind") or "strategy"
    if kind == "execution":
        raise HTTPException(400, "执行研究比较的是成本而不是收益，不适用过拟合概率")
    series: dict[str, list[tuple[int, float]]] = {}
    sharpe: dict[str, float] = {}
    for v in p.get("versions") or []:
        key = f"v{v['v']}"
        if kind == "strategy":
            if not v.get("bt_id"):
                continue
            body = _store().get_blob("backtests", v["bt_id"]) or {}
            ts, eq = body.get("timestamps") or [], body.get("equity_curve") or []
            series[key] = list(zip([int(t) for t in ts], [float(x) for x in eq], strict=False))
            sh = (v.get("metrics") or {}).get("sharpe")
        else:
            rep = lab.report(pid, v["v"]) if hasattr(lab, "report") else None
            if not rep:
                continue
            nav = rep.get("nav") if kind == "portfolio" else rep.get("ls_nav")
            series[key] = [(int(x["t"]), float(x["nav"])) for x in nav or [] if x.get("nav") is not None]
            sh = (rep.get("summary") or {}).get("sharpe" if kind == "portfolio" else "ls_sharpe")
        if sh is not None:
            sharpe[key] = float(sh)
    return series, sharpe, p.get("title") or pid


@router.post("/ledger/pbo", summary="过拟合概率（CSCV）：同一研究项目的各个版本，或同一策略的历次回测")
def pbo(payload: PboIn) -> dict:
    if payload.project_id:
        series, sharpe, title = _project_series(payload.project_id)
    elif payload.slug:
        rows = sorted(
            [r for r in _store().list("backtests") if r.get("slug") == payload.slug],
            key=lambda r: r.get("created_at") or 0,
        )[-40:]
        series, sharpe = {}, {}
        for r in rows:
            body = _store().get_blob("backtests", r["id"]) or {}
            series[r["id"]] = list(
                zip(
                    [int(t) for t in body.get("timestamps") or []],
                    [float(x) for x in body.get("equity_curve") or []],
                    strict=False,
                )
            )
            if (r.get("metrics") or {}).get("sharpe") is not None:
                sharpe[r["id"]] = float(r["metrics"]["sharpe"])
        title = rows[0].get("strategy_name") if rows else payload.slug
    else:
        raise HTTPException(400, "需要 project_id 或 slug")
    keys, mat, ts = D.align_series(series)
    if len(keys) < 2:
        raise HTTPException(400, "至少要两个有收益曲线的版本才能算过拟合概率")
    if mat.shape[0] < 32:
        raise HTTPException(400, f"各版本共同的时间点只有 {mat.shape[0]} 个（回测区间或周期不一致），至少需要 32 个")
    try:
        res = D.pbo_cscv(mat, splits=payload.splits)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    import numpy as np

    sr = mat.mean(axis=0) / np.where(mat.std(axis=0) > 0, mat.std(axis=0), 1)
    best = int(np.argmax(sr))
    skew, kurt = D.return_moments(mat[:, best])
    from ..research.portfolio import bars_per_year

    step = (ts[-1] - ts[0]) / max(len(ts) - 1, 1) if len(ts) > 1 else 3_600_000
    ppy = 365 * 86_400_000 / step if step > 0 else bars_per_year("1h")
    dsr = D.deflated_sharpe(
        float(sr[best] * np.sqrt(ppy)),
        n_trials=len(keys),
        sr_ann_var=float(np.var(sr * np.sqrt(ppy), ddof=1)),
        periods=mat.shape[0],
        periods_per_year=ppy,
        skew=skew,
        kurt=kurt,
    )
    verdict = "ok" if res["pbo"] < 0.2 else "warn" if res["pbo"] < 0.5 else "bad"
    return {
        "title": title,
        "versions": keys,
        "best": keys[best],
        **res,
        "dsr": dsr,
        "verdict": verdict,
        "sharpe": {k: sharpe.get(k) for k in keys},
    }


# ---------------------------------------------------------------------------
# 紧封测试集
# ---------------------------------------------------------------------------
def _holdout_view() -> dict[str, Any]:
    st = _store()
    cfg = D.holdout_config(st)
    if not cfg:
        return {"sealed": False}
    days = (time.time() * 1000 - cfg["cutoff"]) / 86_400_000
    return {"sealed": True, **cfg, "days": round(days, 1), "unseals": len(D.unseals(st))}


@router.get("/holdout", summary="紧封测试集：紧封线、开封记录、设置留痕")
def holdout() -> dict:
    st = _store()
    return {**_holdout_view(), "records": D.unseals(st)[:200], "log": D.holdout_log(st)[:50]}


class SealIn(BaseModel):
    days: int | None = Field(None, ge=7, le=730, description="最近多少天作为紧封测试集")
    cutoff: int | None = Field(None, description="直接指定紧封线（毫秒）")
    note: str = Field("", max_length=200)


@router.post("/holdout", summary="设置（或调整）紧封线：之后的数据在研究里看不到")
def set_holdout(payload: SealIn) -> dict:
    if payload.cutoff is None and payload.days is None:
        raise HTTPException(400, "给出 days 或 cutoff")
    now = int(time.time() * 1000)
    cutoff = int(payload.cutoff) if payload.cutoff is not None else now - int(payload.days or 90) * 86_400_000
    if cutoff >= now - 86_400_000:
        raise HTTPException(400, "紧封线要早于现在至少一天")
    prev = D.holdout_config(_store())
    if prev and cutoff > prev["cutoff"] and D.unseals(_store()):
        raise HTTPException(409, "已经有成果开封过，紧封线只能往前移（让测试集变长），不能往后移")
    D.seal(_store(), cutoff=cutoff, user=_user(), note=payload.note)
    return holdout()


class UnsealAllIn(BaseModel):
    reason: str = Field(..., min_length=2, max_length=200)


@router.post("/holdout/remove", summary="解除紧封（留痕）：之后研究会用到全部数据")
def remove_holdout(payload: UnsealAllIn) -> dict:
    D.unseal_all(_store(), user=_user(), reason=payload.reason)
    return holdout()


class UnsealIn(BaseModel):
    kind: Literal["strategy", "factor", "model", "portfolio"]
    ref: str = Field(..., min_length=1, max_length=80)


@router.post("/holdout/unseal", summary="开封：用紧封测试集评估一个成果。每个成果只能开封一次，结果永久记录")
def unseal(payload: UnsealIn) -> dict:
    st = _store()
    cfg = D.holdout_config(st)
    if not cfg:
        raise HTTPException(400, "还没有设置紧封测试集")
    prev = D.unseal_record(st, payload.kind, payload.ref)
    if prev:
        return {"record": prev, "again": True}
    cutoff = int(cfg["cutoff"])
    with D.unsealed():
        name, result = _evaluate_holdout(payload.kind, payload.ref, cutoff)
    rec = D.record_unseal(st, kind=payload.kind, ref=payload.ref, name=name, user=_user(), cutoff=cutoff, result=result)
    get_services().audit.record("rd.unseal", object_type=payload.kind, object_id=payload.ref)
    return {"record": rec, "again": False}


def _evaluate_holdout(kind: str, ref: str, cutoff: int) -> tuple[str, dict[str, Any]]:
    import numpy as np

    s = get_services()
    from ..research.datahub import DataHub
    from ..research.panel import PanelError, build_panel

    hub = DataHub(s.store, secrets_fn=s.read_secrets)
    now = int(time.time() * 1000)
    if kind == "strategy":
        from ..api.schemas import ResearchBacktestIn
        from .routes_research import _record_or_404, run_backtest_endpoint

        rec = _record_or_404(ref)
        out = run_backtest_endpoint(
            ResearchBacktestIn(
                slug=ref,
                start=cutoff,
                bars_limit=20_000,
                persist=True,
                label="紧封测试集开封",
                wait_seconds=120,
                securities=rec.meta.securities,
                timeframe=rec.meta.timeframe,
            ),
            SimpleNamespace(headers={}),
        )  # type: ignore[arg-type]
        if not isinstance(out, dict) or not out.get("result"):
            raise HTTPException(504, "回测还没跑完，稍后在回测列表里查看（这次开封不计数）")
        m = (out.get("row") or {}).get("metrics") or out["result"].get("metrics") or {}
        return rec.meta.name, {
            "sharpe": m.get("sharpe"),
            "total_return": m.get("total_return"),
            "max_drawdown": m.get("max_drawdown"),
            "trades": m.get("trades"),
            "bt_id": out.get("id"),
            "label": "策略回测",
        }
    if kind == "factor":
        from ..research import factor_expr as FX
        from ..research.factor_eval import evaluate_matrix
        from ..research.factor_lib import FactorLibrary
        from ..research.portfolio import bars_per_year

        f = FactorLibrary(s.store).get(ref) or FactorLibrary(s.store).by_name(ref)
        if not f:
            raise HTTPException(404, "因子不存在")
        node = FX.parse(f["expression"])
        tf = f.get("timeframe") or "4h"
        step = 365 * 86_400_000 / bars_per_year(tf)
        n = int((now - cutoff) / step) + 400
        syms = f.get("universe") or ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
        try:
            panel = build_panel(s.market, hub, syms, tf, fields=FX.fields_used(node), bars_limit=min(n, 20_000))
        except PanelError as exc:
            raise HTTPException(400, str(exc)) from exc
        F = FX.evaluate(node, panel.data)
        mask = panel.times >= cutoff
        if mask.sum() < 30:
            raise HTTPException(400, "紧封测试集里的数据太少（不足 30 根）")
        rep = evaluate_matrix(
            np.where(mask[:, None], F, np.nan), panel, horizon=int(f.get("horizon") or 1), cost_bps=10
        )
        sm = rep["summary"]
        base = (s.store.get_blob("factor_reports", f["id"]) or {}).get("summary") or {}
        return f["name"], {
            "rank_ic": sm.get("rank_ic"),
            "ic_t": sm.get("ic_t"),
            "ls_sharpe": sm.get("ls_sharpe"),
            "bars": int(mask.sum()),
            "research_rank_ic": base.get("rank_ic"),
            "label": "因子",
        }
    if kind == "model":
        from ..research.factor_eval import evaluate_matrix
        from ..research.model_lib import ModelLibrary
        from ..research.models import fields_for, predict_panel
        from ..research.portfolio import bars_per_year

        lib = ModelLibrary(s.store)
        m = lib.get(ref) or lib.by_name(ref)
        state = lib.state(m["id"]) if m else None
        if not m or state is None:
            raise HTTPException(404, "模型不存在")
        spec = m["spec"]
        tf = spec.get("timeframe") or "4h"
        step = 365 * 86_400_000 / bars_per_year(tf)
        n = int((now - cutoff) / step) + 400
        try:
            panel = build_panel(s.market, hub, spec["symbols"], tf, fields=fields_for(spec), bars_limit=min(n, 20_000))
        except PanelError as exc:
            raise HTTPException(400, str(exc)) from exc
        P = predict_panel(spec, state, panel)
        mask = panel.times >= max(cutoff, int(m.get("train_end") or 0))
        if mask.sum() < 30:
            raise HTTPException(400, "紧封测试集里的数据太少（不足 30 根）")
        rep = evaluate_matrix(
            np.where(mask[:, None], P, np.nan), panel, horizon=int(spec.get("horizon") or 1), cost_bps=10
        )
        sm = rep["summary"]
        return m["name"], {
            "rank_ic": sm.get("rank_ic"),
            "ic_t": sm.get("ic_t"),
            "ls_sharpe": sm.get("ls_sharpe"),
            "bars": int(mask.sum()),
            "research_rank_ic": (m.get("summary") or {}).get("oos_rank_ic"),
            "label": "模型",
        }
    from ..research import portfolio as PF
    from ..research.portfolio_lib import PortfolioLibrary, signal_for
    from .routes_portfolio import _fields

    sch = PortfolioLibrary(s.store).get(ref) or PortfolioLibrary(s.store).by_name(ref)
    if not sch:
        raise HTTPException(404, "配置方案不存在")
    cfg = PF.normalize_config(sch["config"])
    tf = sch.get("timeframe") or "4h"
    step = 365 * 86_400_000 / PF.bars_per_year(tf)
    start = int(cutoff - (cfg.get("lookback") or 120) * step)
    try:
        panel = build_panel(s.market, hub, sch.get("symbols") or [], tf, fields=_fields(cfg), start=start)
        signal, ic, _ = signal_for(cfg, panel, s.store)
        rep = PF.evaluate_scheme(cfg, panel, cost_bps=10, signal=signal, signal_ic=ic)
    except (PF.PortfolioError, PanelError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    sm = rep["summary"]
    base = sch.get("summary") or {}
    return sch["name"], {
        "sharpe": sm.get("sharpe"),
        "max_drawdown": sm.get("max_drawdown"),
        "ann_vol": sm.get("ann_vol"),
        "beat_ew": sm.get("beat_ew"),
        "bars": sm.get("bars"),
        "research_sharpe": base.get("sharpe"),
        "label": "组合",
    }


# ---------------------------------------------------------------------------
# 血缘图
# ---------------------------------------------------------------------------
def _strategies() -> list[dict[str, Any]]:
    from .routes_research import _repo

    out = []
    for r in _repo().list():
        out.append(
            {
                "slug": r.meta.slug,
                "name": r.meta.name,
                "source": r.source,
                "updated_at": r.meta.updated_at,
                "status": r.meta.status,
            }
        )
    return out


FIELD_LABELS = {
    "funding": "资金费率",
    "oi": "持仓量",
    "ls_ratio": "多空比",
    "taker_ratio": "主动买卖比",
    "basis": "期现基差",
    "fng": "恐惧贪婪指数",
    "stablecoin": "稳定币市值",
    "tvl": "DeFi 锁仓",
}


def _field_labels() -> dict[str, str]:
    labels = dict(FIELD_LABELS)
    try:
        from ..research.datahub import DataHub

        for row in DataHub(_store()).datasets():
            f = row.get("field") or row.get("name")
            if f and f not in labels:
                labels[f] = row.get("label") or row.get("title") or f
    except Exception:
        pass
    return labels


@router.get("/lineage", summary="血缘图：信息源 → 因子 → 模型 → 组合 / 执行方案 → 策略 → 回测，以及需要重跑的提示")
def lineage(node: str | None = None) -> dict:
    g = LN.build(_store(), _strategies(), field_labels=_field_labels())
    if node:
        g["trace"] = LN.trace(g, node)
    return g


# ---------------------------------------------------------------------------
# 定时任务
# ---------------------------------------------------------------------------
def _tokens() -> float:
    s = get_services()
    try:
        from .routes_ops import _quota

        return float(_quota().daily(workspace_id=context.current_workspace()).get("llm_tokens") or 0)
    except Exception:
        _ = s
        return 0.0


def _start_task(task: dict[str, Any], rounds: int) -> str:
    from .routes_lab import ProjectIn, create_project

    kind = task["kind"]
    tf = task.get("timeframe") or {"execution": "1m", "strategy": "1h"}.get(kind, "4h")
    view = create_project(
        ProjectIn(
            kind=kind,
            idea=task["idea"],
            executor=task.get("executor") or "solo",
            securities=task.get("symbols") or [],
            timeframe=tf,
            max_rounds=rounds,
            title=task["idea"][:30],
            data_source=task.get("data_source") or "auto",
            bars_limit=3000 if kind == "execution" else 1500,
        )
    )
    pid = (view.get("project") or view)["id"]
    _lab().store.patch(pid, nightshift=True)
    return pid


def _run_task(pid: str) -> None:
    from .routes_lab import AutopilotIn, start_autopilot

    p = _lab().store.get(pid) or {}
    start_autopilot(pid, AutopilotIn(rounds=p.get("max_rounds"), wait=True))


def _versions(pid: str) -> int:
    return len((_lab().store.get(pid) or {}).get("versions") or [])


def _stop_project(pid: str) -> None:
    _lab().request_stop(pid)


def _summarize(pid: str) -> dict[str, Any]:
    p = _lab().store.get(pid) or {}
    vs = p.get("versions") or []
    cur = next((v for v in vs if v["v"] == p.get("current_v")), vs[-1] if vs else None)
    passed = bool(cur and (cur.get("evaluation") or {}).get("passed"))
    kind = NS.KINDS.get(p.get("kind") or "strategy", "")
    head = f"{kind}「{p.get('title') or p.get('idea', '')[:24]}」：{len(vs)} 版，"
    head += (f"v{cur['v']} 达到目标" if passed else f"未达标，最好的是 v{cur['v']}") if cur else "没有产出版本"
    return {
        "headline": head,
        "passed": passed,
        "admitted": bool(cur and cur.get("admitted")),
        "best_v": cur["v"] if cur else None,
        "awaiting": p.get("status") == "awaiting_user",
    }


def _after_shift(final: dict[str, Any]) -> None:
    cfg = NS.config(_store())
    if cfg.get("brief"):
        try:
            make_brief(
                BriefIn(
                    push=cfg.get("channel") not in (None, "", "none"),
                    channel=cfg.get("channel") or "console",
                    source="nightshift",
                )
            )
        except Exception:
            pass


def run_nightshift(trigger: str = "manual") -> dict[str, Any]:
    """同步跑一个班次。worker 的定时任务和"立即开始"按钮都走这里。"""
    return NS.run_shift(
        _store(),
        start_task=_start_task,
        run_task=_run_task,
        versions_of=_versions,
        stop_project=_stop_project,
        summarize=_summarize,
        tokens_fn=_tokens,
        trigger=trigger,
        on_done=_after_shift,
    )


@router.get("/nightshift", summary="定时任务：预算设置、任务队列、正在执行的批次与历史")
def nightshift() -> dict:
    st = _store()
    return {
        "config": NS.config(st),
        "tasks": NS.tasks(st)[:100],
        "running": NS.running_shift(st),
        "shifts": NS.shifts(st),
        "kinds": NS.KINDS,
    }


class NightConfigIn(BaseModel):
    enabled: bool | None = None
    hour: int | None = Field(None, ge=0, le=23)
    minute: int | None = Field(None, ge=0, le=59)
    max_trials: int | None = Field(None, ge=1, le=500)
    max_tokens: int | None = Field(None, ge=1000, le=50_000_000)
    max_minutes: int | None = Field(None, ge=5, le=1440)
    rounds: int | None = Field(None, ge=1, le=8)
    brief: bool | None = None
    channel: str | None = Field(None, max_length=40)


@router.put("/nightshift/config", summary="修改定时任务预算与执行时间")
def nightshift_config(payload: NightConfigIn) -> dict:
    return {"config": NS.save_config(_store(), payload.model_dump(exclude_none=True))}


class NightTaskIn(BaseModel):
    kind: Literal["strategy", "factor", "model", "portfolio", "execution"] = "factor"
    idea: str = Field(..., min_length=2, max_length=600)
    executor: Literal["solo", "team"] = "solo"
    symbols: list[str] = Field(default_factory=list)
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"] | None = None
    rounds: int | None = Field(None, ge=1, le=8)
    data_source: Literal["auto", "exchange", "synthetic"] = "auto"


@router.post("/nightshift/tasks", summary="往定时任务队列里加一个研究任务")
def add_night_task(payload: NightTaskIn) -> dict:
    try:
        return {"task": NS.add_task(_store(), user=_user(), **payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/nightshift/tasks/{tid}", summary="从队列里移除（正在跑的不能移除）")
def remove_night_task(tid: str) -> dict:
    if not NS.remove_task(_store(), tid):
        raise HTTPException(409, "任务不存在或正在运行")
    return {"ok": True}


class RunNowIn(BaseModel):
    wait: bool = False


@router.post("/nightshift/run", summary="立即执行一次定时任务（后台运行）")
def nightshift_run(payload: RunNowIn) -> dict:
    st = _store()
    if NS.running_shift(st):
        raise HTTPException(409, "已经有一个班次在跑")
    if not NS.tasks(st, status="queued"):
        raise HTTPException(400, "队列是空的，先加几个研究任务")
    if payload.wait:
        return {"shift": run_nightshift("manual")}
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(run_nightshift, "manual"), daemon=True, name="nightshift").start()
    for _ in range(40):
        cur = NS.running_shift(st)
        if cur:
            return {"shift": cur}
        time.sleep(0.05)
    return {"shift": None}


@router.post("/nightshift/stop", summary="停止正在执行的定时任务（当前项目保留到最近一版）")
def nightshift_stop() -> dict:
    return {"ok": NS.request_stop(_store())}


@router.get("/nightshift/shifts/{sid}", summary="一次定时任务执行的详情")
def nightshift_detail(sid: str) -> dict:
    rec = NS.shift(_store(), sid)
    if rec is None:
        raise HTTPException(404, "班次不存在")
    return {"shift": rec}


# ---------------------------------------------------------------------------
# AI 晨报
# ---------------------------------------------------------------------------
def _brief_data() -> dict[str, Any]:
    s = get_services()
    st = s.store
    data: dict[str, Any] = {"at": time.time()}
    try:
        from .routes_execution import radar

        r = radar(universe="major10", timeframe="1h", symbols="", source="auto")
        rows = [x for x in r["rows"] if not x.get("error")]
        alerts = [
            f"{x['symbol'].split('/')[0]} {a['text']}"
            for x in rows
            for a in x.get("alerts") or []
            if a["level"] in ("alert", "warn")
        ]
        data["market"] = {"rows": rows, "alerts": alerts[:6], "synthetic": r.get("synthetic")}
    except Exception:
        pass
    fs = st.list("factors")
    data["factors"] = {
        "total": len(fs),
        "active": sum(f.get("status") == "active" for f in fs),
        "candidate": sum(f.get("status") == "candidate" for f in fs),
        "retired": sum(f.get("status") == "retired" for f in fs),
        "decaying": [f["name"] for f in fs if f.get("status") == "decaying"],
    }
    ms = st.list("ml_models")
    flagged = []
    for m in ms:
        h = (m.get("health") or [{}])[-1] if m.get("health") else {}
        if m.get("status") == "decaying":
            flagged.append({"name": m["name"], "why": "样本外 IC 明显走弱"})
        elif (h.get("psi_max") or 0) >= 0.25 and m.get("status") == "active":
            flagged.append({"name": m["name"], "why": f"特征分布漂移（PSI {h['psi_max']:.2f}）"})
    data["models"] = {
        "total": len(ms),
        "active": sum(m.get("status") == "active" for m in ms),
        "candidate": sum(m.get("status") == "candidate" for m in ms),
        "flagged": flagged,
    }
    pfs = [p for p in st.list("pf_schemes") if p.get("status") == "active"]
    data["portfolio"] = {
        "schemes": [
            {
                "name": p["name"],
                **{k: (p.get("summary") or {}).get(k) for k in ("sharpe", "max_drawdown", "ann_vol")},
                "vol_target": ((p.get("config") or {}).get("risk") or {}).get("vol_target"),
            }
            for p in pfs
        ]
    }
    exr = sorted(st.list("ex_runs"), key=lambda r: -(r.get("created_at") or 0))[:20]
    if exr:
        isv = [(r.get("summary") or {}).get("is_bps") for r in exr]
        bt = [(r.get("summary") or {}).get("beat_twap_bps") for r in exr]
        isv = [x for x in isv if x is not None]
        bt = [x for x in bt if x is not None]
        data["execution"] = {
            "runs": len(exr),
            "avg_is_bps": sum(isv) / len(isv) if isv else None,
            "avg_beat_twap": sum(bt) / len(bt) if bt else None,
            "active": [e["name"] for e in st.list("ex_schemes") if e.get("status") == "active"],
        }
    sess = [x for x in st.list("paper_sessions") if x.get("status") in ("running", "queued")]
    paper: dict[str, Any] = {"running": len(sess), "sessions": []}
    for x in sess[:6]:
        pts = x.get("equity_points") or []
        ret = pts[-1]["v"] / pts[0]["v"] - 1 if len(pts) > 1 and pts[0].get("v") else None
        paper["sessions"].append({"name": x.get("strategy_name"), "ret": ret})
    shadows = []
    for sh in SH.items(st):
        if sh.get("status") != "running":
            continue
        b, c = st.get("paper_sessions", sh["baseline_id"]), st.get("paper_sessions", sh["candidate_id"])
        if b and c:
            cmp = SH.compare(b, c, min_days=sh["min_days"])
            shadows.append({"label": sh["label"], "verdict": cmp["verdict"], "ready": cmp["ready"]})
    paper["shadows"] = shadows
    data["paper"] = paper
    try:  # AI 值守员：先把运行中的实验各看一遍（只看不动），结论进晨报
        from ..research import watcher as W
        from .routes_validation import watch_session

        for x in sess[:10]:
            try:
                watch_session(x["id"], use_llm=False)
            except Exception:
                pass
        data["watcher"] = W.brief_section(st, {x["id"] for x in sess})
    except Exception:
        pass
    last = NS.shifts(st, 1)
    if last and time.time() - (last[0].get("started_at") or 0) < 36 * 3600:
        data["shift"] = last[0]
    lg = D.ledger(st, project_titles=_titles())
    day_ago = time.time() - 86400
    warns = [
        f"「{f['label']}」试了 {f['n']} 次，最好成绩修正后可信度 {f['dsr']:.0%}"
        for f in lg["families"]
        if f.get("dsr") is not None and f.get("verdict") == "bad" and (f.get("last_at") or 0) > day_ago
    ][:3]
    warns += [
        f"「{f['label']}」试了 {f['n']} 次，最好的 |t|={abs(f['best_t']):.1f}，低于修正门槛 {f['t_needed']:.1f}"
        for f in lg["families"]
        if f.get("best_t") is not None and f.get("verdict") != "ok" and (f.get("last_at") or 0) > day_ago
    ][:2]
    ho = _holdout_view()
    data["ledger"] = {
        "total": lg["total"],
        "last_24h": sum(1 for r in D.collect_trials(st) if (r.get("created_at") or 0) > day_ago),
        "warnings": warns,
        "holdout": f"紧封测试集：最近 {ho['days']:.0f} 天，已开封 {ho['unseals']} 个成果" if ho.get("sealed") else "",
    }
    aw = []
    lab = _lab()
    for p in lab.store.list():
        if p.get("status") == "awaiting_user" and not p.get("archived"):
            asks = [
                m
                for m in lab.store.messages(p["id"])
                if m.get("kind") == "ask" and (m.get("data") or {}).get("answered") is None
            ]
            aw.append(
                {
                    "id": p["id"],
                    "title": p.get("title") or p["id"],
                    "question": (asks[-1]["content"] if asks else "等你回复")[:80],
                }
            )
    data["awaiting"] = aw[:8]
    return data


class BriefIn(BaseModel):
    push: bool = False
    channel: str = Field("console", max_length=40)
    source: str = Field("manual", max_length=20)


@router.post("/brief", summary="生成一份研究晨报（可选推送到通知渠道）")
def make_brief(payload: BriefIn) -> dict:
    data = _brief_data()
    b = BR.compose(data)
    pushed: list[str] = []
    if payload.push:
        try:
            from ..notify.channels import Notification

            if get_services().hub.send(Notification(title=b["title"], body=b["text"], channel=payload.channel)):
                pushed.append(payload.channel)
        except Exception:
            pass
    rec = BR.save(_store(), b, source=payload.source, pushed=pushed)
    return {"brief": rec}


@router.get("/brief", summary="最新一份晨报")
def latest_brief() -> dict:
    return {"brief": BR.latest(_store()), "history": BR.history(_store())}


@router.get("/brief/{bid}", summary="某一份晨报")
def get_brief(bid: str) -> dict:
    rec = _store().get(BR.BRIEFS, bid)
    if rec is None:
        raise HTTPException(404, "晨报不存在")
    return {"brief": rec}


# ---------------------------------------------------------------------------
# 积木式组装
# ---------------------------------------------------------------------------
@router.get("/assemble/options", summary="组装可选项：标的池、因子、模型、配置方案、执行方案")
def assemble_options() -> dict:
    from ..research.panel import UNIVERSES

    st = _store()
    live = ("active", "candidate", "decaying")
    return {
        "universes": [{"id": k, "name": v["name"], "symbols": v["symbols"]} for k, v in UNIVERSES.items()],
        "factors": [
            {"name": f["name"], "status": f.get("status"), "detail": f.get("expression", "")[:60]}
            for f in st.list("factors")
            if f.get("status") in live
        ],
        "models": [
            {"name": m["name"], "status": m.get("status"), "detail": f"{m['spec']['model']} → {m['spec']['label']}"}
            for m in st.list("ml_models")
            if m.get("status") in live
        ],
        "schemes": [
            {
                "name": p["name"],
                "status": p.get("status"),
                "detail": f"{(p.get('config') or {}).get('optimizer')} · {(p.get('config') or {}).get('risk_model')}",
            }
            for p in st.list("pf_schemes")
            if p.get("status") in ("active", "candidate")
        ],
        "executions": [
            {"name": e["name"], "status": e.get("status"), "detail": str((e.get("config") or {}).get("algo"))}
            for e in st.list("ex_schemes")
            if e.get("status") in ("active", "candidate") and e.get("cost_model")
        ],
        "signals": [{"id": k, "label": v} for k, v in AS.SIGNALS.items()],
        "builders": [{"id": k, "label": v} for k, v in AS.BUILDERS.items()],
        "timeframes": ["15m", "30m", "1h", "4h", "1d"],
    }


class AssembleIn(BaseModel):
    blocks: dict[str, Any]
    create: bool = False


@router.post("/assemble", summary="积木组装：生成策略代码（create=true 时存成新策略）")
def assemble(payload: AssembleIn) -> dict:
    try:
        out = AS.generate(payload.blocks)
    except (AS.AssembleError, ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    out["warnings"] = AS.check_refs(out["config"], _store())
    from ..strategies import lang as L

    out["issues"] = L.issues(L.PYTHON, out["code"])
    if payload.create:
        if out["issues"]:
            raise HTTPException(400, "；".join(out["issues"]))
        from .routes_research import new_strategy
        from .schemas import NewStrategyIn

        c = out["config"]
        tf = c["timeframe"] if is_timeframe(c["timeframe"]) else "4h"
        res = new_strategy(
            NewStrategyIn(
                name=c["name"],
                template_id=None,
                code=out["code"],
                securities=c["symbols"],
                timeframe=tf,
                team="积木组装",
                notes="积木组装：" + " → ".join(f"{x['step']} {x['text']}" for x in out["outline"]),
            )
        )
        out["strategy"] = res["strategy"]
    return out


# ---------------------------------------------------------------------------
# 影子运行
# ---------------------------------------------------------------------------
def _session(sid: str) -> dict[str, Any] | None:
    return _store().get("paper_sessions", sid)


@router.get("/shadows", summary="影子运行列表与对比结论")
def shadows() -> dict:
    out = []
    for sh in SH.items(_store()):
        b, c = _session(sh["baseline_id"]), _session(sh["candidate_id"])
        cmp = SH.compare(b, c, min_days=sh["min_days"]) if b and c else None
        if cmp:
            cmp = {k: v for k, v in cmp.items() if k != "curve"}
        out.append(
            {
                **sh,
                "compare": cmp,
                "baseline_status": (b or {}).get("status"),
                "candidate_status": (c or {}).get("status"),
            }
        )
    return {"shadows": out}


class ShadowIn(BaseModel):
    baseline_session_id: str
    candidate_slug: str
    min_days: int = Field(7, ge=1, le=90)


@router.post("/shadows", summary="开始影子运行：用和线上实验相同的设置并行模拟候选策略")
def create_shadow(payload: ShadowIn) -> dict:
    from .routes_studio import start_paper
    from .schemas import PaperStartIn

    base = _session(payload.baseline_session_id)
    if base is None:
        raise HTTPException(404, "基准实验不存在")
    if base.get("status") not in ("running", "queued", "paused"):
        raise HTTPException(400, "基准实验已经结束，选一个还在运行的实验")
    cost = base.get("cost") or {}
    body: dict[str, Any] = {
        "slug": payload.candidate_slug,
        "securities": base.get("securities") or [base.get("symbol")],
        "timeframe": base.get("timeframe"),
        "initial_cash": base.get("initial_cash") or 10_000,
        "source": base.get("source") or "exchange",
        "exchange": base.get("exchange") or "binance",
    }
    if cost.get("taker_fee") is not None:
        body["taker_fee"] = cost["taker_fee"]
    if cost.get("slippage_bps") is not None:
        body["slippage_bps"] = cost["slippage_bps"]
    try:
        started = start_paper(PaperStartIn(**body))
    except HTTPException:
        raise
    cand = started["session"]
    rec = SH.create(
        _store(),
        baseline_id=base["id"],
        candidate_id=cand["id"],
        baseline_name=base.get("strategy_name") or base["id"],
        candidate_name=cand.get("strategy_name") or cand["id"],
        min_days=payload.min_days,
        user=_user(),
    )
    return {"shadow": rec, "session": cand}


@router.get("/shadows/{sid}", summary="影子运行详情：两条净值、收益差、t 值、替换条件")
def shadow_detail(sid: str) -> dict:
    sh = SH.get(_store(), sid)
    if sh is None:
        raise HTTPException(404, "影子运行不存在")
    b, c = _session(sh["baseline_id"]), _session(sh["candidate_id"])
    return {
        "shadow": sh,
        "compare": SH.compare(b, c, min_days=sh["min_days"]) if b and c else None,
        "baseline": {k: (b or {}).get(k) for k in ("id", "strategy_name", "status", "timeframe", "securities")},
        "candidate": {k: (c or {}).get(k) for k in ("id", "strategy_name", "status", "timeframe", "securities")},
    }


class PromoteIn(BaseModel):
    force: bool = False


@router.post("/shadows/{sid}/promote", summary="替换：停止基准实验，候选成为新的线上版本（仍是模拟，不真实下单）")
def promote_shadow(sid: str, payload: PromoteIn) -> dict:
    from .routes_studio import stop_paper

    sh = SH.get(_store(), sid)
    if sh is None:
        raise HTTPException(404, "影子运行不存在")
    b, c = _session(sh["baseline_id"]), _session(sh["candidate_id"])
    cmp = SH.compare(b, c, min_days=sh["min_days"]) if b and c else None
    if not payload.force and not (cmp and cmp["ready"]):
        raise HTTPException(409, "还没满足替换条件；确实要替换请勾选强制替换")
    try:
        stop_paper(sh["baseline_id"])
    except HTTPException:
        pass
    get_services().audit.record("rd.shadow.promote", object_type="shadow", object_id=sid)
    return {"shadow": SH.mark(_store(), sid, status="promoted", promoted_at=time.time(), forced=bool(payload.force))}


@router.post("/shadows/{sid}/end", summary="结束影子运行：停止候选实验，保留基准")
def end_shadow(sid: str) -> dict:
    from .routes_studio import stop_paper

    sh = SH.get(_store(), sid)
    if sh is None:
        raise HTTPException(404, "影子运行不存在")
    try:
        stop_paper(sh["candidate_id"])
    except HTTPException:
        pass
    return {"shadow": SH.mark(_store(), sid, status="ended")}


# ---------------------------------------------------------------------------
# 研究流水线摘要（总览页一行）
# ---------------------------------------------------------------------------
@router.get("/pipeline", summary="总览页的研究流水线：因子、模型、组合、执行、定时任务与纪律各一格")
def pipeline() -> dict:
    st = _store()
    fs = st.list("factors")
    ms = st.list("ml_models")
    live_ics = [((m.get("health") or [{}])[-1] or {}).get("live_rank_ic") for m in ms if m.get("status") == "active"]
    live_ics = [x for x in live_ics if x is not None]
    pfs = [p for p in st.list("pf_schemes") if p.get("status") == "active"]
    usage = None
    for p in pfs:
        vt = ((p.get("config") or {}).get("risk") or {}).get("vol_target")
        av = (p.get("summary") or {}).get("ann_vol")
        if vt and av:
            usage = max(usage or 0, av / vt)
    exr = sorted(st.list("ex_runs"), key=lambda r: -(r.get("created_at") or 0))[:20]
    isv = [(r.get("summary") or {}).get("is_bps") for r in exr if (r.get("summary") or {}).get("is_bps") is not None]
    last = NS.shifts(st, 1)
    sh = last[0] if last else None
    running = NS.running_shift(st)
    lg_total = sum(int(r.get("count") or 1) for r in D.collect_trials(st))
    return {
        "factors": {
            "active": sum(f.get("status") == "active" for f in fs),
            "decaying": sum(f.get("status") == "decaying" for f in fs),
            "total": len(fs),
        },
        "models": {
            "active": sum(m.get("status") == "active" for m in ms),
            "total": len(ms),
            "live_ic": sum(live_ics) / len(live_ics) if live_ics else None,
        },
        "portfolio": {"active": len(pfs), "risk_usage": usage},
        "execution": {
            "runs": len(exr),
            "avg_is_bps": sum(isv) / len(isv) if isv else None,
            "active": sum(e.get("status") == "active" for e in st.list("ex_schemes")),
        },
        "nightshift": {
            "running": bool(running),
            "queued": len(NS.tasks(st, status="queued")),
            "last": {k: sh.get(k) for k in ("id", "started_at", "status", "stop_reason")} if sh else None,
            "passed": sum(1 for it in (sh or {}).get("items") or [] if it.get("passed")),
        },
        "discipline": {"trials": lg_total, "holdout": _holdout_view().get("sealed", False)},
        "brief": {k: (BR.latest(st) or {}).get(k) for k in ("id", "at", "headline")} if BR.latest(st) else None,
    }


# ---------------------------------------------------------------------------
# 定时：worker 每分钟检查一次每日任务是否到点
# ---------------------------------------------------------------------------
def nightshift_due() -> bool:
    st = _store()
    cfg = NS.config(st)
    if NS.running_shift(st):
        return False
    last = NS.shifts(st, 1)
    return NS.due(cfg, time.localtime(), last[0]["started_at"] if last else None)


__all__ = ["make_brief", "nightshift_due", "router", "run_nightshift"]
