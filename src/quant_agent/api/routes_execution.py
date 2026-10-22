"""执行与监控 API（全部是仿真）：执行方案仿真与 TCA、片段回放监控、盘口录制、成本校准、容量估计、市场雷达。

这里没有任何下单接口：盘口录制只读交易所的公开行情，成本校准只读已导入的历史成交。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import backtest_retention as BR
from ..platform import context
from ..research import execution as EX
from ..research import goals as RG
from ..research.execution_lib import (
    STATUS_LABELS,
    ExecutionLibrary,
    asset_stats,
    fill_refs,
    load_tapes,
    start_recording,
    stop_recording,
)
from ..research.panel import UNIVERSES
from .deps import get_services

router = APIRouter(prefix="/api/execution", tags=["执行与监控"])

Source = Literal["auto", "kline", "synthetic", "recording"]


class SimIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    source: Source = "auto"
    recording_id: str | None = None
    steps: int = Field(3000, ge=300, le=20_000)
    note: str = Field("", max_length=200)


class ReplayIn(SimIn):
    episode: int = Field(-1, ge=-1, le=500)  # -1 = 中间那个片段
    side: Literal["buy", "sell"] = "buy"


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


class RecordIn(BaseModel):
    exchange: Literal["binance", "okx"] = "binance"
    symbol: str = Field("BTC/USDT", max_length=30)
    seconds: int = Field(600, ge=10, le=4 * 3600)
    interval: float = Field(2.0, ge=0.5, le=60)
    depth: int = Field(20, ge=5, le=50)


class FillRow(BaseModel):
    ts: int
    symbol: str
    side: str
    qty: float = Field(..., gt=0)
    price: float = Field(..., gt=0)


class CalibrateIn(BaseModel):
    import_id: str | None = None
    fills: list[FillRow] = Field(default_factory=list, max_length=50_000)
    name: str = Field("", max_length=40)


class CapacityIn(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    weights: dict[str, float] = Field(default_factory=dict)
    turnover: float = Field(20.0, gt=0, le=1000, description="年换手（单边，倍）")
    alpha: float = Field(0.3, gt=0, le=10, description="扣成本前年化收益")
    rebalances: float = Field(365, gt=0, le=100_000)
    fee_bps: float = Field(5.0, ge=0, le=100)
    cost_model_id: str | None = None


def _lib() -> ExecutionLibrary:
    return ExecutionLibrary(get_services().store)


def _tapes(p: SimIn) -> list[EX.Tape]:
    s = get_services()
    syms = [x.strip().upper() for x in p.symbols if x.strip()] or ["BTC/USDT"]
    try:
        return load_tapes(s.market, _lib(), syms, source=p.source, steps=p.steps, recording_id=p.recording_id)
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc


def _cfg(raw: dict[str, Any]) -> dict[str, Any]:
    try:
        return EX.normalize_config(raw)
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------------------------------------------------------------------------
@router.get("/meta", summary="算法菜单、默认方案、市场状态、目标指标、录制与成本模型")
def meta() -> dict:
    lib = _lib()
    return {
        "algos": [{"id": k, **v} for k, v in EX.ALGOS.items()],
        "styles": [{"id": k, "label": v} for k, v in EX.STYLES.items()],
        "states": list(EX.STATES),
        "defaults": EX.DEFAULT_CONFIG,
        "universes": [{"id": k, "name": v["name"], "symbols": v["symbols"]} for k, v in UNIVERSES.items()],
        "statuses": STATUS_LABELS,
        "goal": RG.EXECUTION.meta(),
        "recordings": [r for r in lib.recordings() if r["status"] in ("done", "stopped", "recording")][:20],
        "cost_model": lib.active_cost_model(),
        "trials": lib.trial_count(),
    }


@router.post("/evaluate", summary="仿真一个执行方案：多个片段 + TWAP/市价基准，返回 TCA 报告（记一笔试验）")
def evaluate(p: SimIn) -> dict:
    services = get_services()
    ctx = context.current()
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        ctx.user_id or "",
        ctx.role,
        "execution_run",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), ctx.user_id or "", ctx.role
        ),
    )
    cfg = _cfg(p.config)
    tapes = _tapes(p)
    try:
        report = EX.evaluate_scheme(cfg, tapes)
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    run = _lib().record_run(
        cfg, report, symbols=[t.symbol for t in tapes], source="manual", note=p.note, created_by=ctx.user_id or "user"
    )
    return {"run": run, "report": report}


@router.post("/replay", summary="回放一个执行片段：逐步的计划 / 完成进度、挂单价、市场状态、成交与提示")
def replay(p: ReplayIn) -> dict:
    cfg = _cfg(p.config)
    tape = _tapes(p)[0]
    try:
        starts = EX.episode_starts(tape, cfg)
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    states, info = EX.detect_states(tape, cfg["state_model"], fit_upto=starts[0])
    i = len(starts) // 2 if p.episode < 0 else min(p.episode, len(starts) - 1)
    s = starts[i]
    side = 1 if p.side == "buy" else -1
    Q = EX.order_qty(tape, cfg, s)
    out = EX.run_episode(tape, cfg, s, side, Q, states, record=True)
    twap = EX.run_episode(tape, EX.normalize_config({**cfg, "algo": "twap", "style": "neutral"}), s, side, Q, states)
    H = cfg["horizon"]
    a, b = max(0, s - 30), min(len(tape), s + H + 30)
    ctx = [
        {
            "t": int(tape.t[k]),
            "mid": float(tape.mid[k]),
            "spread_bps": float(tape.spread[k] / tape.mid[k] * 1e4),
            "volume": float(tape.volume[k]),
            "state": int(states[k]),
        }
        for k in range(a, b)
    ]
    return {
        "episode": i,
        "episodes": len(starts),
        "symbol": tape.symbol,
        "source": tape.source,
        "tape": tape.summary(),
        "result": {k: v for k, v in out.items() if k not in ("timeline", "alerts", "fills")},
        "timeline": out["timeline"],
        "alerts": out["alerts"],
        "fills": out["fills"],
        "context": ctx,
        "offset": s - a,
        "twap_is_bps": twap["is_bps"],
        "state_model": info,
        "states": list(EX.STATES),
    }


@router.get("/runs", summary="仿真试验记录")
def runs() -> dict:
    lib = _lib()
    return {"runs": lib.runs(), "trials": lib.trial_count()}


@router.get("/runs/{rid}", summary="一次仿真的完整报告")
def run_detail(rid: str) -> dict:
    lib = _lib()
    run = lib.run(rid)
    if run is None:
        raise HTTPException(404, "试验不存在")
    return {"run": run, "report": lib.run_report(rid)}


@router.delete("/runs/{rid}", summary="删除仿真试验")
def run_delete(rid: str) -> dict:
    return {"ok": _lib().delete_run(rid)}


# ---------------- 盘口录制 ----------------
@router.get("/recordings", summary="盘口录制列表")
def recordings() -> dict:
    return {"recordings": _lib().recordings()}


@router.post("/recordings", summary="开始录制公开盘口与逐笔成交（只读行情，不需要密钥）")
def record(p: RecordIn) -> dict:
    s = get_services()
    adapter = s.market.adapters.get(p.exchange)
    if adapter is None:
        raise HTTPException(400, f"没有配置 {p.exchange} 行情接入")
    try:
        rec = start_recording(
            _lib(),
            adapter,
            exchange=p.exchange,
            symbol=p.symbol.upper(),
            seconds=p.seconds,
            interval=p.interval,
            depth=p.depth,
        )
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"recording": rec}


@router.get("/recordings/{rid}", summary="录制详情与盘口概况")
def recording(rid: str) -> dict:
    lib = _lib()
    rec = lib.recording(rid)
    if rec is None:
        raise HTTPException(404, "录制不存在")
    summary = None
    if rec["status"] != "recording":
        data = lib.recording_data(rid)
        try:
            summary = EX.tape_from_recording(data).summary() if data else None
        except EX.ExecutionError as exc:
            summary = {"error": str(exc)}
    return {"recording": rec, "tape": summary}


@router.post("/recordings/{rid}/stop", summary="提前结束录制")
def recording_stop(rid: str) -> dict:
    return {"ok": stop_recording(rid)}


@router.delete("/recordings/{rid}", summary="删除录制")
def recording_delete(rid: str) -> dict:
    return {"ok": _lib().delete_recording(rid)}


# ---------------- 成本模型 ----------------
@router.get("/costs", summary="成本模型列表（当前启用的那个用于容量估计与回测滑点）")
def costs() -> dict:
    lib = _lib()
    return {"models": lib.cost_models(), "active": lib.active_cost_model()}


@router.post("/costs/calibrate", summary="用已导入的真实成交校准平方根冲击模型")
def calibrate(p: CalibrateIn) -> dict:
    s = get_services()
    rows: list[dict[str, Any]] = [r.model_dump() for r in p.fills]
    source = "manual"
    if p.import_id:
        imp = s.live.get_import(context.current_workspace(), p.import_id)
        if imp.get("kind") != "fills":
            raise HTTPException(400, "这份导入不是成交记录")
        for f in s.live.live_fills(context.current_workspace(), [imp["run_id"]]):
            rows.append(
                {
                    "ts": int(f["ts"]),
                    "symbol": f.get("instrument") or f.get("security"),
                    "side": f["side"],
                    "qty": float(f.get("qty") or f.get("amount")),
                    "price": float(f["price"]),
                }
            )
        source = f"import:{p.import_id}"
    if not rows:
        raise HTTPException(400, "没有成交：选择一份已导入的成交记录，或直接传入 fills")
    try:
        model = EX.calibrate(rows, fill_refs(s.market, rows))
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    rec = _lib().save_cost_model(
        name=p.name or "真实成交校准", model=model, symbols=sorted({r["symbol"] for r in rows}), source=source
    )
    return {"model": {**rec, "points": model["points"]}}


@router.get("/costs/{cid}", summary="成本模型详情（含散点）")
def cost_detail(cid: str) -> dict:
    rec = _lib().cost_model(cid)
    if rec is None:
        raise HTTPException(404, "成本模型不存在")
    return {"model": rec}


@router.post("/costs/{cid}/activate", summary="启用这个成本模型")
def cost_activate(cid: str) -> dict:
    rec = _lib().activate_cost_model(cid)
    if rec is None:
        raise HTTPException(404, "成本模型不存在")
    return {"model": rec}


@router.delete("/costs/{cid}", summary="删除成本模型")
def cost_delete(cid: str) -> dict:
    return {"ok": _lib().delete_cost_model(cid)}


# ---------------- 容量 ----------------
@router.post("/capacity", summary="容量估计：资金规模越大冲击越大，算出成本吃掉一半 / 全部收益时的规模")
def capacity(p: CapacityIn) -> dict:
    s = get_services()
    lib = _lib()
    model = lib.cost_model(p.cost_model_id) if p.cost_model_id else lib.active_cost_model()
    if model is None:
        raise HTTPException(404, "成本模型不存在")
    syms = [x.strip().upper() for x in p.symbols if x.strip()]
    if not syms:
        raise HTTPException(400, "至少需要一个标的")
    stats = asset_stats(s.market, syms)
    w = {k: p.weights.get(k, 1.0 / len(syms)) for k in syms}
    assets = [{"symbol": k, "weight": w[k], **stats[k]} for k in syms]
    try:
        out = EX.capacity(model, assets, turnover=p.turnover, alpha=p.alpha, rebalances=p.rebalances, fee_bps=p.fee_bps)
    except EX.ExecutionError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        **out,
        "assets": assets,
        "model": {k: model.get(k) for k in ("id", "name", "a_bps", "Y")},
        "synthetic": any(a.get("synthetic") for a in assets),
    }


# ---------------- 市场雷达 ----------------
@router.get("/radar", summary="市场雷达：一组标的的波动、量能、价差与状态，异常给出提示")
def radar(
    universe: str = "major10",
    timeframe: Literal["1m", "5m", "15m", "1h"] = "5m",
    symbols: str = "",
    source: Literal["auto", "exchange", "synthetic"] = "auto",
) -> dict:
    # 雷达每次都会取行情并重新计算，不属于冻结历史；停用时必须在取数之前拦截。
    from ..platform.context import current
    from ..workflows.agent import service
    from ..workflows.plugins import require

    if current().workspace_id:
        require(service(), "execution")
    s = get_services()
    syms = [x.strip().upper() for x in symbols.split(",") if x.strip()] or list(
        UNIVERSES.get(universe, UNIVERSES["major10"])["symbols"]
    )
    tf_ms = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000}[timeframe]
    rows, synthetic = [], False
    for sym in syms[:30]:
        try:
            bars, src = s.market.get_bars(sym, timeframe, limit=600, source=source)
            synthetic = synthetic or src == "synthetic"
            rows.append({**EX.radar_row(sym, bars, tf_ms), "synthetic": src == "synthetic"})
        except Exception as exc:
            rows.append({"symbol": sym, "error": str(exc)[:120]})
    rank = {"alert": 0, "warn": 1, "info": 2}
    rows.sort(key=lambda r: (min((rank[a["level"]] for a in r.get("alerts") or []), default=3), r["symbol"]))
    return {
        "rows": rows,
        "timeframe": timeframe,
        "synthetic": synthetic,
        "alerts": sum(len(r.get("alerts") or []) for r in rows),
    }


# ---------------- 执行方案库（放最后：/{sid} 会吞掉其它路径） ----------------
@router.get("", summary="执行方案库")
def schemes(status: str | None = None) -> dict:
    lib = _lib()
    return {"schemes": lib.list(status), "trials": lib.trial_count()}


@router.post("", summary="把一次仿真存为执行方案")
def save(p: SaveIn) -> dict:
    services = get_services()
    ctx = context.current()
    BR.guard_new_record(
        services.store,
        services.auth,
        context.current_workspace(),
        ctx.user_id or "",
        ctx.role,
        "execution",
        max_units=BR.effective_limit(
            services.auth, services.model_hub, context.current_workspace(), ctx.user_id or "", ctx.role
        ),
    )
    lib = _lib()
    run = lib.run(p.run_id)
    rep = lib.run_report(p.run_id)
    if run is None or rep is None:
        raise HTTPException(404, "试验不存在")
    rec = lib.save(
        name=p.name,
        config=rep["config"],
        report=rep,
        symbols=run["symbols"],
        status=p.status,
        note=p.note,
        scheme_id=p.scheme_id,
        run_id=p.run_id,
        created_by=ctx.user_id or "user",
    )
    return {"scheme": rec}


@router.get("/{sid}", summary="执行方案详情")
def scheme(sid: str) -> dict:
    rec = _lib().get(sid)
    if rec is None:
        raise HTTPException(404, "执行方案不存在")
    return {"scheme": rec}


@router.get("/{sid}/report", summary="执行方案的完整报告")
def scheme_report(sid: str) -> dict:
    return {"report": _lib().report(sid)}


@router.patch("/{sid}", summary="修改执行方案的名字、状态、备注")
def scheme_patch(sid: str, p: PatchIn) -> dict:
    rec = _lib().patch(sid, **{k: v for k, v in p.model_dump().items() if v is not None})
    if rec is None:
        raise HTTPException(404, "执行方案不存在")
    return {"scheme": rec}


@router.delete("/{sid}", summary="删除执行方案")
def scheme_delete(sid: str) -> dict:
    return {"ok": _lib().delete(sid)}
