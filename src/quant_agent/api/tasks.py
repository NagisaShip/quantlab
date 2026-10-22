"""worker 上执行的任务：回测、参数优化、实时模拟。

控制面（API）只做校验、登记 run、返回 run_id；这里的代码只在 worker（或 inline 模式下的同一套路径）里跑。
Python 策略一律在隔离子进程中执行（见 platform/isolation.py），这里拿到的只是结果。
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from ..data.quality import TIMEFRAME_MS
from ..engine.live import LIVE_WARMUP_BARS, clock_synthetic_bars
from ..engine.paper import PaperSession, PaperTradingEngine
from ..engine.research import BacktestSpec, load_market, manifest, summarize_metrics
from ..platform import backtest_retention as BR
from ..platform import isolation
from ..platform.errors import AppError, ErrorCode
from ..platform.runs import CANCELED, COMPLETED, PAUSED, RUNNING, TERMINAL
from ..platform.sandbox_exec import bars_to_wire
from ..platform.worker import ResidentDriver, TaskCtl, Worker
from ..sdk.runtimes import RuntimeCost
from ..store import Collections
from ..workflows.service import handle_export, handle_workflow

PAPER_TICK_SECONDS = float(os.environ.get("QAGENT_PAPER_TICK_SECONDS", "30"))


def _spec(d: dict[str, Any]) -> BacktestSpec:
    allowed = BacktestSpec.__dataclass_fields__
    return BacktestSpec(**{k: v for k, v in d.items() if k in allowed})


def _check_bar_quota(ctl: TaskCtl, bars: dict[str, list]) -> None:
    ws = ctl.services.auth.workspace(ctl.run["workspace_id"])
    limit = int((ws.get("settings") or {}).get("quotas", {}).get("max_bars_per_run", 1_000_000))
    total = sum(len(v) for v in bars.values())
    if total > limit:
        raise AppError(
            ErrorCode.QUOTA_EXHAUSTED,
            f"本次运行需要 {total} 根 bar，超过工作区上限 {limit}",
            details={"bars": total, "limit": limit},
        )


def _csv_dataset(services: Any, dataset_id: str, spec: BacktestSpec):
    from ..data.quality import DataSet, parse_csv

    saved = services.store.get(Collections.DATASETS, dataset_id)
    if saved is None:
        raise AppError(ErrorCode.NOT_FOUND, "数据集不存在")
    sym = saved.get("symbol") or spec.securities[0]
    tf = saved.get("timeframe") or spec.timeframe
    builtin = bool(saved.get("builtin_teaching"))
    ds = DataSet(
        sym,
        tf,
        parse_csv(saved["content"], symbol=sym, timeframe=tf),
        "synthetic" if builtin else "csv",
    ).freeze("内置教学合成数据" if builtin else f"用户 CSV · {saved.get('filename', '')}")

    spec.securities = [sym]
    spec.timeframe = tf
    return {sym: ds.bars}, [ds.summary()], []


def _prepare_research(
    ctl: TaskCtl, m: dict[str, Any], spec: BacktestSpec, bars: dict[str, list]
) -> tuple[dict[str, Any], list[str]]:
    """策略里用到 factor() / predict() / optimize() 时，在 worker 里按时间点算好（沙箱没有数据权限）。"""
    source = m.get("source") or ""
    if (m.get("lang") or "python") != "python" or not any(f"{k}(" in source for k in ("factor", "predict", "optimize")):
        return {}, []
    try:
        from ..research.datahub import DataHub
        from ..research.signals import prepare
    except ImportError:
        return {}, ["策略用到了因子 / 模型 / 组合方案，但服务器没有安装 numpy，这些函数会返回空"]
    ctl.event("phase", {"phase": "preparing_research"})
    services = ctl.services
    hub = DataHub(services.store, secrets_fn=getattr(services, "read_secrets", None))
    try:
        return prepare(
            source,
            bars,
            spec.timeframe,
            store=services.store,
            hub=hub,
            data_source=spec.source,
            stop=lambda: ctl.beat(),
        )
    except Exception as exc:  # 研究数据失败不应该让整个回测失败
        return {}, [f"研究数据准备失败：{type(exc).__name__}: {str(exc)[:200]}"]


def _delistings(services: Any, source: str) -> dict[str, Any]:
    """用了动态标的池的策略：把已存的下架公告交给内核，按公告可见时刻移出标的池。"""
    from ..research.events import delisting_rows, uses_pairlist

    if not uses_pairlist(source):
        return {}
    try:
        rows = delisting_rows(services.store)
    except Exception:
        return {}
    return {"delistings": rows} if rows else {}


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------
def handle_backtest(ctl: TaskCtl) -> dict[str, Any]:
    m = ctl.manifest
    services = ctl.services
    spec = _spec(m["spec"])
    ctl.event("phase", {"phase": "loading_data"})
    holdout_note = None
    if not m.get("holdout_bypass") and not m.get("dataset_id"):
        from ..research.discipline import clamp_end

        end, holdout_note = clamp_end(services.store, spec.end)
        if holdout_note:
            spec.end = end
            if spec.start and spec.start >= end:
                raise ValueError("回测区间完全落在紧封测试集里。需要的话请到「研究纪律 → 紧封测试集」开封这个策略")
    if m.get("dataset_id"):
        bars, datasets, warnings = _csv_dataset(services, m["dataset_id"], spec)
    else:
        bars, datasets, warnings = load_market(services.market, spec)
    if holdout_note:
        warnings = [holdout_note, *warnings]
    _check_bar_quota(ctl, bars)
    ctl.beat(force=True)
    research, research_warnings = _prepare_research(ctl, m, spec, bars)
    warnings = [*warnings, *research_warnings]
    research = {**research, **_delistings(services, m.get("source") or "")}
    funding_sets: list[dict[str, Any]] = []
    if any(sec.upper().endswith(":PERP") for sec in bars):
        from ..engine.research import load_funding

        funding, funding_sets, funding_warnings = load_funding(services.market, spec, bars, datasets)
        research = {**research, **funding}
        warnings = [*warnings, *funding_warnings]
    ctl.event("phase", {"phase": "running", "isolation": isolation.probe()})
    t0 = time.perf_counter()
    body = isolation.run_batch(
        {
            "op": "backtest",
            "research": research,
            "lang": m.get("lang") or "python",
            "source": m["source"],
            "name": m.get("module_name") or "strategy",
            "spec": spec.to_dict(),
            "bars": bars_to_wire(bars),
            "params": m.get("params"),
            "run_id": ctl.run_id,
            "strategy_version_id": ctl.run.get("strategy_version_id") or "",
            "time_limit": 60.0,
        },
        timeout=180,
    )
    duration = int((time.perf_counter() - t0) * 1000)
    status = "failed" if body.get("timed_out") or (body.get("errors") and not body.get("fills")) else "done"
    body.update(
        {
            "strategy_name": m.get("name"),
            "datasets": datasets,
            "funding_data": funding_sets,
            "warnings": warnings,
            "spec": spec.to_dict(),
            "manifest": {
                **manifest(m["source"], spec, datasets, body),
                "run_id": ctl.run_id,
                "strategy_version_id": ctl.run.get("strategy_version_id"),
                "lang": m.get("lang") or "python",
                "contract_specs": body.get("contract_specs") or {},
                "funding_data": funding_sets,
                "isolation": isolation.probe(),
            },
            "duration_ms": duration,
            "status": status,
            "run_id": ctl.run_id,
        }
    )
    store = services.store
    # Research Review Gate：在落盘前生成可复核的审查卡；高严重度问题会阻断保存/晋级。
    try:
        from ..agents import redteam
        from ..research.discipline import strategy_record
        from ..research.review import build_review

        rec = strategy_record(store, m.get("slug") or "") or {}
        points = redteam.rule_points(m.get("source") or "", {}, body.get("metrics") or {})
        review = build_review(
            metrics=body.get("metrics") or {},
            source=m.get("source") or "",
            datasets=datasets,
            funding=funding_sets,
            contract_specs=body.get("contract_specs") or {},
            simple_benchmark=body.get("simple_benchmark_metrics") or {},
            trial_count=int(rec.get("n") or 0) or None,
            dsr=rec.get("dsr"),
            redteam_points=points,
        )
        body["review"] = review
        body.setdefault("manifest", {})["review"] = {
            "id": review["id"],
            "verdict": review["verdict"],
            "blocks_promotion": review["blocks_promotion"],
            "summary": review["summary"],
        }
    except Exception:
        logging.getLogger("quantlab.review").exception("研究审查生成失败：%s", ctl.run_id)
    # 成交与信号进数据库（按 fill_key / signal_id 去重，重放也只有一份）
    services.runs.save_fills(ctl.run_id, ctl.run["workspace_id"], body.get("fills") or [])
    services.runs.save_signals(ctl.run_id, ctl.run["workspace_id"], body.get("signals") or [])
    # T06：大数组（逐 bar 净值、信号、成交）落到 <数据目录>/results/<工作区>/<run>.<格式>，
    # blob 里只留摘要与引用。落盘发生在 worker 里（这里就是 worker 的执行体），
    # 不占 API 请求线程。注意：本地的 `body` 仍保留完整大数组，下面的 bt 行还要用
    # timestamps；只有**存进 blob 的那份**被换成摘要。
    store.put_result("run_results", ctl.run_id, body, workspace_id=ctl.run["workspace_id"])
    summary: dict[str, Any] = {
        "status": status,
        "metrics": summarize_metrics(body.get("metrics") or {}),
        "duration_ms": duration,
        "fills": len(body.get("fills") or []),
    }
    # T07：把本次运行的实际消耗记进配额账。记在这里（worker 执行体）而不是 API 里，
    # 因为 API 只负责登记运行，真正消耗资源的是 worker；而且 worker 挂了就不该计费。
    # 计量失败绝不能影响运行结果——用户要的是回测报告，不是一条配额写入报错，
    # 所以整段包在 try 里，失败只记日志。
    try:
        services.quota.add("backtest_runs", 1, workspace_id=ctl.run["workspace_id"])
        services.quota.add(
            "backtest_cpu_seconds",
            max(0, int(duration)) / 1000.0,
            workspace_id=ctl.run["workspace_id"],
        )
    except Exception:
        logging.getLogger("quantlab.quota").exception("用量计量失败：%s", ctl.run_id)
    if m.get("persist"):
        bt_id = m.get("bt_id") or f"bt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        row = {
            "id": bt_id,
            "run_id": ctl.run_id,
            "slug": m.get("slug"),
            "strategy_name": m.get("name"),
            "version": m.get("version") or "未保存的草稿",
            "label": m.get("label"),
            "securities": spec.securities,
            "timeframe": spec.timeframe,
            "start_ts": body["timestamps"][0] if body.get("timestamps") else 0,
            "end_ts": body["timestamps"][-1] if body.get("timestamps") else 0,
            "initial_cash": spec.initial_cash,
            "data_sources": sorted({d["source"] for d in datasets}),
            "review": body.get("review"),
            "review_verdict": (body.get("review") or {}).get("verdict"),
            "review_blocks": bool((body.get("review") or {}).get("blocks_promotion")),
            "metrics": summary["metrics"],
            "status": status,
            "errors": len(body.get("errors") or []),
            "duration_ms": duration,
            "created_at": time.time(),
            "strategy_version_id": ctl.run.get("strategy_version_id"),
            "lang": m.get("lang") or "python",
            # 谁跑的这次回测：策略血缘图上按版本列出回测与执行人
            "created_by": ctl.run.get("created_by"),
            "pinned": False,
            # 来自 AI 研究室对话的回测：记下对话，回测记录页可以按对话分组
            "lab_project_id": m.get("lab_project_id"),
            "lab_title": m.get("lab_title"),
        }
        store.upsert(Collections.BACKTESTS, row)
        try:
            BR.enforce_after_persist(
                store,
                services.auth,
                ctl.run["workspace_id"],
                str(ctl.run.get("created_by") or ""),
                row,
            )
            BR.enforce_total_after_persist(
                store,
                services.auth,
                ctl.run["workspace_id"],
                str(ctl.run.get("created_by") or ""),
                None,
            )
        except Exception:
            # 入队前已经做过硬校验；这里失败不牺牲刚完成的结果，只记录并留待下次清理。
            logging.getLogger("quantlab.backtest_retention").exception("回测记录自动清理失败：%s", bt_id)
        store.put_blob("backtests", bt_id, {**body, "id": bt_id, "code": m["source"], "row": row})
        # 研究包快照：原始 K 线 + 资金费/标记价 + 源码规格。数据与结果分开放，避免详情接口返回大数组。
        store.put_blob(
            "research_snapshots",
            bt_id,
            {
                "bars": bars_to_wire(bars),
                "funding": {k: v for k, v in research.items() if k.startswith("funding:")},
                "source": m["source"],
                "spec": spec.to_dict(),
                "datasets": datasets,
                "created_at": time.time(),
            },
        )
        summary["bt_id"] = bt_id
        summary["row"] = row
        if m.get("slug") and status == "done" and m.get("mark_backtested"):
            from ..strategies.repo import StrategyRepository

            repo = StrategyRepository(store.root / "strategies")
            rec = repo.get(m["slug"])
            if rec and rec.meta.status in ("draft", "tested"):
                repo.update_meta(m["slug"], status="backtested")
    return summary


# ---------------------------------------------------------------------------
# 参数优化（检查点：已算完的组合在恢复后不重算）
# ---------------------------------------------------------------------------
def handle_optimize(ctl: TaskCtl) -> dict[str, Any]:
    m = ctl.manifest
    services = ctl.services
    spec = _spec(m["spec"])
    bars, datasets, warnings = load_market(services.market, spec)
    _check_bar_quota(ctl, bars)
    cp = ctl.checkpoint_data or {}
    rows: list[dict[str, Any]] = list(cp.get("rows") or [])
    if rows:
        ctl.event("recovered", {"rows_done": len(rows)})

    def on_line(msg: dict[str, Any]) -> None:
        if "progress" in msg:
            d, t = msg["progress"]
            ctl.progress(d, t)
        if "row" in msg:
            rows.append(msg["row"])
            ctl.checkpoint({"rows": rows}, progress=(len(rows), m.get("combinations") or len(rows)))

    out = isolation.run_batch(
        {
            "op": "optimize",
            "lang": m.get("lang") or "python",
            "source": m["source"],
            "name": m.get("module_name") or "strategy",
            "spec": spec.to_dict(),
            "bars": bars_to_wire(bars),
            "grid": m["grid"],
            "split": m.get("split", 0.7),
            "objective": m.get("objective", "sharpe"),
            "embargo_bars": m.get("embargo_bars", 0),
            "holdout": m.get("holdout", 0.0),
            "walk_forward_folds": m.get("walk_forward_folds", 0),
            "method": m.get("method") or "grid",
            "space": m.get("space"),
            "n_trials": m.get("n_trials") or 0,
            "multi_objective": bool(m.get("multi_objective")),
            "done_rows": rows,
        },
        on_line=on_line,
        timeout=1800,
    )
    # 排名是在 optimize() 全部算完后才附加的；把带 in_rank/out_rank 的最终行写回检查点，
    # 否则 worker 中断后恢复出来的行会缺少排名，与直接跑完的结果不一致。
    ctl.checkpoint({"rows": out.get("rows") or rows})
    opt_id = f"opt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    row = {
        "id": opt_id,
        "run_id": ctl.run_id,
        "slug": m.get("slug"),
        "strategy_name": m.get("name"),
        "version": m.get("version"),
        "objective": m.get("objective"),
        "combinations": m.get("combinations"),
        "split": m.get("split"),
        "best_params": (out.get("best") or {}).get("params"),
        "diagnostics": out.get("diagnostics"),
        "method": out.get("method") or "grid",
        # 每一组试验的样本内成绩都进试验台账（DSR 按试验次数修正，网格和智能调参一视同仁）
        "trial_sharpes": [(r.get("in_sample") or {}).get("sharpe") for r in out.get("rows") or [] if "in_sample" in r][
            :500
        ],
        "timeframe": spec.timeframe,
        "start_ts": min((b[0].open_time for b in bars.values() if b), default=0),
        "end_ts": max((b[-1].close_time for b in bars.values() if b), default=0),
        "created_at": time.time(),
        "created_by": ctl.run.get("created_by"),
        "pinned": False,
    }
    services.store.upsert(Collections.OPTIMIZATIONS, row)
    services.store.put_blob(
        "optimizations",
        opt_id,
        {**out, "id": opt_id, "row": row, "datasets": datasets, "warnings": warnings, "spec": spec.to_dict()},
    )
    return {"id": opt_id, "best_params": row["best_params"]}


# ---------------------------------------------------------------------------
# 平台内置模板回测（含压力场景；不执行用户代码，在 worker 进程内运行）
# ---------------------------------------------------------------------------
def handle_template_backtest(ctl: TaskCtl) -> dict[str, Any]:
    from ..engine.backtest import CostModel, stress_scenarios

    p = ctl.manifest["payload"]
    s = ctl.services
    common = dict(
        strategy_name=p["strategy"],
        params=p.get("params") or {},
        symbol=p["symbol"],
        timeframe=p["timeframe"],
        initial_cash=p["initial_cash"],
        bars_limit=p["bars_limit"],
        exchange=p["exchange"],
    )
    result = s.run_backtest(**common, cost_scale=p.get("cost_scale", 1.0), scenario=p.get("scenario"))
    body: dict[str, Any] = {"run": result.to_dict(max_points=1200), "run_id": ctl.run_id}
    if p.get("compare_scenarios"):
        body["scenarios"] = {}
        for i, label in enumerate(stress_scenarios(CostModel())):
            ctl.progress(i + 1, 4)
            r = s.run_backtest(**common, scenario=label, persist=False)
            body["scenarios"][label] = {
                "total_return": r.metrics.total_return,
                "max_drawdown": r.metrics.max_drawdown,
                "sharpe": r.metrics.sharpe,
                "trades": r.metrics.trades,
                "final_equity": r.metrics.final_equity,
            }
    # T06：模板回测的大数组嵌在 body["run"] 里，同样落盘；blob 只留摘要与引用。
    s.store.put_result("run_results", ctl.run_id, body, workspace_id=ctl.run["workspace_id"], nested="run")
    return {"template_run_id": result.run_id, "total_return": result.metrics.total_return}


# ---------------------------------------------------------------------------
# 实时模拟（常驻）
# ---------------------------------------------------------------------------
def paper_engine(services: Any, cost: RuntimeCost | None = None) -> PaperTradingEngine:
    if cost is None:
        cost_cfg = services.config.exchange.get("paper_cost") or {}
        cost = RuntimeCost(
            taker_fee=float(cost_cfg.get("taker_fee", 0.001)), slippage_bps=float(cost_cfg.get("slippage_bps", 5))
        )
    return PaperTradingEngine(cost)


def latest_bars(services: Any, session: PaperSession, count: int) -> dict[str, list]:
    """各标的最近 count 根已收盘 bar。真实行情失败直接报错，不换成合成数据。"""
    step = TIMEFRAME_MS.get(session.timeframe, 3_600_000)
    now = int(time.time() * 1000)
    out: dict[str, list] = {}
    for sec in session.securities or [session.symbol]:
        if session.source == "demo":
            out[sec] = clock_synthetic_bars(sec, session.timeframe, now - count * step, now)
        else:
            bars, _ = services.market.get_bars(
                sec, session.timeframe, limit=count, exchange=session.exchange or "binance", source="exchange"
            )
            out[sec] = bars
    return out


#: 有 WebSocket 推送时的节拍：新 bar 收盘后几秒内进策略
STREAM_TICK_SECONDS = float(os.environ.get("QAGENT_STREAM_TICK_SECONDS", "2"))
#: 交易所模拟盘对账间隔
RECONCILE_EVERY_MS = 5 * 60_000


class PaperDriver(ResidentDriver):
    """实时模拟驱动器。三件事可以独立组合：

    - 行情：WebSocket 推送（网关健康时）或 REST 轮询（退路）；
    - 成交：平台撮合（paper）或交易所模拟盘（demo，经 OMS）；
    - 策略：始终在隔离子进程里，拿不到密钥。
    """

    tick_seconds = PAPER_TICK_SECONDS

    def __init__(self) -> None:
        self.sbx: isolation.IsolatedSession | None = None
        self.streams: list[Any] = []
        self.stream_key: tuple[str, list[str], str] | None = None
        self.last_rest = 0.0
        self.last_save = 0.0
        self.oms: Any = None
        self.connection: dict[str, Any] | None = None
        self.last_reconcile = 0
        self.ledger_view: dict[str, Any] = {}
        self.last_funding = 0.0

    # 会话文档（界面读取）与 run 状态保持一致；run 状态是唯一真相
    def _load(self, ctl: TaskCtl) -> PaperSession:
        saved = ctl.services.store.get(Collections.PAPER_SESSIONS, ctl.run_id)
        if saved is None:
            raise AppError(ErrorCode.NOT_FOUND, "实验记录不存在")
        return PaperSession.from_dict(saved)

    def _save(self, ctl: TaskCtl, session: PaperSession) -> dict[str, Any]:
        if self.oms is not None:
            session.demo = {**session.demo, **self._demo_view()}
        self._feed_view(session)
        self.last_save = time.monotonic()
        return ctl.services.store.upsert(Collections.PAPER_SESSIONS, session.to_dict(max_points=100_000))

    # ------------------------------------------------------------------ 行情
    def _open_streams(self, session: PaperSession) -> None:
        from ..data import stream

        if session.source != "exchange" or not stream.enabled():
            return
        gw = stream.gateway(lambda ex: _backfill_for(ex))
        secs = session.securities or [session.symbol]
        ex = session.exchange or "binance"
        try:
            self.streams = [gw.subscribe(ex, s, session.timeframe) for s in secs]
        except ValueError:
            self.streams = []
            return
        self.stream_key = (ex, secs, session.timeframe)
        self.tick_seconds = STREAM_TICK_SECONDS

    def _streams_healthy(self) -> bool:
        return bool(self.streams) and all(s.stats.healthy() for s in self.streams)

    def _feed_view(self, session: PaperSession) -> None:
        if not self.streams:
            session.feed = "rest"
            session.feed_status = {"mode": "rest", "interval_s": PAPER_TICK_SECONDS}
            return
        snaps = [s.snapshot() for s in self.streams]
        healthy = all(x["healthy"] for x in snaps)
        session.feed = "ws" if healthy else "rest"
        session.feed_status = {
            "mode": session.feed,
            "healthy": healthy,
            "lag_ms": max((x["lag_ms"] for x in snaps), default=0),
            "reconnects": sum(x["reconnects"] for x in snaps),
            "backfilled": sum(x["backfilled"] for x in snaps),
            "error": next((x["error"] for x in snaps if x["error"]), ""),
            "events": sorted((e for x in snaps for e in x["events"]), key=lambda e: e["ts"])[-10:],
        }

    def _latest(self, ctl: TaskCtl, session: PaperSession) -> tuple[dict[str, list] | None, dict[str, float], bool]:
        """返回 (最新已收盘 bar, 盘中价格, 是否有新 bar)。None 表示这一拍不用推进。"""
        if self._streams_healthy():
            latest = {s.symbol: list(s.closed)[-12:] for s in self.streams}
            marks = {s.symbol: p for s in self.streams if (p := s.last_price()) is not None}
            new = any(b.close_time > session.last_bar_ts for bars in latest.values() for b in bars)
            if not new and time.monotonic() - self.last_save < PAPER_TICK_SECONDS:
                return None, marks, False
            if all(latest.values()):
                return latest, marks, new
        # 推送不可用：按原来的节拍 REST 轮询
        if self.streams and time.monotonic() - self.last_rest < max(PAPER_TICK_SECONDS, 1.0):
            return None, {}, False
        self.last_rest = time.monotonic()
        latest = latest_bars(ctl.services, session, 12)
        marks: dict[str, float] = {}
        if session.source == "exchange":
            for sec in session.securities or [session.symbol]:
                try:
                    marks[sec] = float(ctl.services.market.ticker(sec, session.exchange or "binance")["price"])
                except Exception:
                    pass
        return latest, marks, True

    def _delist(self, ctl: TaskCtl) -> list[dict[str, Any]] | None:
        """下架公告每分钟查一次；公告一发布，下一拍就移出标的池（不等新 bar 收盘）。"""
        if time.monotonic() - getattr(self, "last_delist", -1e9) < 60:
            return None
        self.last_delist = time.monotonic()
        return _delistings(ctl.services, ctl.manifest.get("source") or "").get("delistings")

    def _funding(self, ctl: TaskCtl, session: PaperSession) -> dict[str, list[Any]] | None:
        """永续合约最近几天的已结算资金费率，每 10 分钟取一次交给内核（内核按结算时刻去重结算）。"""
        perps = [s for s in (session.securities or [session.symbol]) if s.upper().endswith(":PERP")]
        if not perps or time.monotonic() - self.last_funding < 600:
            return None
        self.last_funding = time.monotonic()
        now = int(time.time() * 1000)
        out: dict[str, list[Any]] = {}
        for sec in perps:
            if session.source == "demo":
                from ..data.service import synthetic_funding

                out[sec] = [list(r) for r in synthetic_funding(sec, now - 3 * 86_400_000, now)]
                continue
            rows, _src, err = ctl.services.market.funding_rates(
                sec, exchange=session.exchange or "binance", start=now - 3 * 86_400_000, end=now, source="exchange"
            )
            if rows:
                out[sec] = [list(r) for r in rows]
            elif err and not any("资金费率" in e.message for e in session.events[-20:]):
                session.note("warning", f"{sec} 资金费率取不到（{err}），暂按 0 计")
        return out or None

    # ------------------------------------------------------------------ 交易所模拟盘
    def _open_demo(self, ctl: TaskCtl, session: PaperSession, cp: dict[str, Any]) -> None:
        from ..live.oms import DemoOMS, OmsLimits, OmsState

        m = ctl.manifest
        quote = (session.securities or [session.symbol])[0].split("/")[-1].split(":")[0]
        limits = OmsLimits.from_dict(m.get("oms_limits"))
        if m.get("execution") == "live_realperp":
            from ..live import real_perp as RP

            if not RP.server_enabled():
                raise AppError(
                    ErrorCode.PERMISSION_DENIED, f"服务器没有打开真实永续（{RP.SERVER_FLAG}=1），不会向实盘下单"
                )
            conn, broker = RP.connections(ctl.services).broker(m["live_connection_id"])
            store, home = ctl.services.store, ctl.services.home
            self.oms = RP.RealPerpOMS(
                broker,
                limits,
                OmsState.from_dict(cp.get("oms")),
                initial_cash=session.initial_cash,
                cap=float((m.get("real_perp") or {}).get("cap") or 0),
                quote=quote,
                max_leverage=float((m.get("real_perp") or {}).get("max_leverage") or 10),
                margin_mode=session.margin_mode,
                position_mode=session.position_mode,
                blocked_fn=lambda: RP.blocked(store, home),
            )
        elif m.get("execution") == "live":
            from ..live import micro

            if not micro.server_enabled():
                raise AppError(
                    ErrorCode.PERMISSION_DENIED, f"服务器没有打开微实盘（{micro.SERVER_FLAG}=1），不会向实盘下单"
                )
            conn, broker = micro.connections(ctl.services).broker(m["live_connection_id"])
            store, home = ctl.services.store, ctl.services.home
            self.oms = micro.LiveOMS(
                broker,
                limits,
                OmsState.from_dict(cp.get("oms")),
                initial_cash=session.initial_cash,
                cap=float((m.get("micro") or {}).get("cap") or 0),
                quote=quote,
                blocked_fn=lambda: micro.blocked(store, home),
            )
        else:
            conn, broker = ctl.services.demo.broker(m["demo_connection_id"])
            if any(s.upper().endswith(":PERP") for s in (session.securities or [session.symbol])):
                broker.use_futures = True
            self.oms = DemoOMS(broker, limits, OmsState.from_dict(cp.get("oms")), initial_cash=session.initial_cash)
        self.connection = conn
        if not self.oms.state.baseline:
            bal = self.oms.snapshot_baseline()
            have = float(bal.get(quote) or 0)
            if have < session.initial_cash:
                raise AppError(
                    ErrorCode.INVALID_PARAMETER,
                    f"模拟盘账户 {quote} 余额 {have:.2f}，少于本次实验的初始资金 {session.initial_cash:.2f}。"
                    "请调低初始资金，或在交易所重置模拟资金",
                )
            session.note(
                "info",
                f"已连接 {conn['env_label']}（{conn['key_mask']}），{quote} 余额 {have:.2f}；"
                f"本实验只动用其中 {session.initial_cash:.2f}",
            )
            if not conn.get("realistic"):
                session.note("warning", f"{conn['env_label']}：{conn['env_note']}")

    def _demo_view(self) -> dict[str, Any]:
        st = self.oms.state
        orders = st.orders[-100:]
        slips = [o["slippage_bps"] for o in st.orders if o.get("slippage_bps") is not None]
        return {
            "connection": {
                k: self.connection.get(k)
                for k in ("id", "exchange", "env", "env_label", "key_mask", "realistic", "env_note")
            }
            if self.connection
            else {},
            "limits": self.oms.limits.to_dict(),
            "halted": st.halted,
            "halt_reason": st.halt_reason,
            "orders": orders,
            "stats": st.stats,
            "reconcile": st.reconcile,
            "slippage": {
                "count": len(slips),
                "avg_bps": round(sum(slips) / len(slips), 2) if slips else None,
                "max_bps": max(slips) if slips else None,
            },
            "ledger": self.ledger_view,
        }

    def _route(
        self,
        ctl: TaskCtl,
        session: PaperSession,
        res: dict[str, Any],
        marks: dict[str, float],
        awaiting: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """把内核 outbox 送交易所，回报交回内核。循环到没有新委托为止（on_trade 里可能再下单）。"""
        assert self.sbx is not None
        out = res
        protects = res.get("protects") or []  # 交易所上挂着的保护止损：每根 bar 查一次有没有被触发
        for _ in range(3):
            outbox = out.get("outbox") or []
            pending = awaiting if awaiting is not None else []
            awaiting = None
            if not outbox and not pending and not protects:
                break
            reports = self.oms.process(outbox, pending, protects)
            protects = []
            if not reports:
                break
            out = self.sbx.call("apply", reports=reports, marks=marks or None)
            res.setdefault("fills", []).extend(out["fills"])
            res.setdefault("rejected", []).extend(out.get("rejected") or [])
            res.setdefault("errors", []).extend(out.get("errors") or [])
            for k in ("equity", "portfolio", "snapshot", "nav", "awaiting"):
                res[k] = out[k]
            self.ledger_view = out.get("ledger") or self.ledger_view
        for o in self.oms.state.orders[-20:]:
            if o["status"] == "rejected" and not o.get("noted"):
                o["noted"] = True
                session.note("warning", f"模拟盘拒单 {o['side']} {o['security']} {o['qty']}：{o['message']}")
        return res

    def _risk_checks(self, ctl: TaskCtl, session: PaperSession, res: dict[str, Any]) -> None:
        """熔断与对账。都只会让平台更保守：暂停开新仓，不会替你下单。"""
        if self.oms is None:
            return
        if hasattr(self.oms, "watchdog"):
            reason = self.oms.watchdog()
            if reason:
                session.note("error", f"真实永续自动急停：{reason}")
                ctl.event("halt", {"reason": reason})
        reason = self.oms.observe_equity(float(res.get("equity") or 0))
        if reason:
            session.note("error", reason)
            ctl.event("halt", {"reason": reason})
        now = int(time.time() * 1000)
        if now - self.last_reconcile < RECONCILE_EVERY_MS or res.get("awaiting") or not self.ledger_view:
            return
        self.last_reconcile = now
        from decimal import Decimal

        try:
            quote = (session.securities or [session.symbol])[0].split("/")[-1].split(":")[0]
            has_perp = any(s.upper().endswith(":PERP") for s in (session.securities or [session.symbol]))
            rec = self.oms.reconcile(
                Decimal(self.ledger_view.get("cash") or "0"),
                {k: Decimal(v) for k, v in (self.ledger_view.get("holdings") or {}).items()},
                quote=quote,
                perps={k: Decimal(v) for k, v in (self.ledger_view.get("perps") or {}).items()} if has_perp else None,
                margin=Decimal(self.ledger_view.get("margin") or "0"),
            )
        except Exception as exc:
            session.note("warning", f"对账失败（下次再试）：{exc}")
            return
        soft = [r for r in rec["rows"] if r.get("soft") and not r["ok"]]
        if soft and rec["ok"]:
            session.note(
                "warning",
                "；".join(
                    f"{r['asset']} 余额与账本差 {float(r['diff']):.4g}（有永续合约时只告警：资金费按公开费率估算记账）"
                    for r in soft
                ),
            )
        if not rec["ok"] and not self.oms.state.halted:
            bad = "；".join(
                f"{r['asset']} 应有 {float(r['expected']):.6g}，交易所 {float(r['actual']):.6g}"
                for r in rec["rows"]
                if not r["ok"] and not r.get("soft")
            )
            msg = f"对账不一致，已暂停开新仓：{bad}。可能是你在交易所手动交易过，或有在途订单"
            self.oms.halt(msg)
            session.note("error", msg)
            ctl.event("halt", {"reason": msg})

    # ------------------------------------------------------------------ 生命周期
    def start(self, ctl: TaskCtl) -> None:
        m = ctl.manifest
        services = ctl.services
        session = self._load(ctl)
        history = latest_bars(services, session, LIVE_WARMUP_BARS)
        cp = ctl.checkpoint_data or {}
        demo = m.get("execution") in ("demo", "live", "live_realperp")
        if demo:
            self._open_demo(ctl, session, cp)
        self.sbx = isolation.IsolatedSession(lang=m.get("lang") or "python")
        res = self.sbx.call(
            "start",
            lang=m.get("lang") or "python",
            source=m["source"],
            name=m.get("module_name") or "strategy",
            params=m.get("params") or None,
            history=bars_to_wire(history),
            timeframe=session.timeframe,
            initial_cash=session.initial_cash,
            cost=m.get("cost") or {},
            risk=m.get("risk") or {},
            run_id=ctl.run_id,
            strategy_version_id=ctl.run.get("strategy_version_id") or "",
            started_at=session.started_at,
            snapshot=cp.get("runtime"),
            fill_mode="external" if demo else "internal",
            initial_currency=session.initial_currency,
            margin_mode=session.margin_mode,
            position_mode=session.position_mode,
            contract_type=session.contract_type,
            expiry_ts=session.expiry_ts,
            ledger_v2=session.ledger_v2,
        )
        resumed = bool(cp.get("runtime"))
        state = ctl.runs.get(ctl.run_id)["state"]
        session.status = "paused" if state == PAUSED else "running"
        session.portfolio = res["portfolio"]
        session.last_bar_ts = max(session.last_bar_ts, int(res.get("last_bar_ts") or 0))
        session.params = res.get("params") or session.params
        session.execution = (m.get("execution") or "demo") if demo else "paper"
        self._open_streams(session)
        if resumed:
            session.note(
                "warning",
                f"已从检查点恢复（第 {ctl.run.get('attempts')} 次认领）：账户、委托与已处理信号按检查点还原；"
                "停机期间的行情只推进数据，不补成交",
            )
            if demo and (res.get("awaiting") or res.get("protects")):
                # 崩溃前送出、没拿到回报的订单：按客户端订单号查清楚再说，不重下；
                # 交易所上的保护止损停机期间可能已经触发：查到成交就补记账
                if res.get("awaiting"):
                    session.note("warning", f"有 {len(res['awaiting'])} 笔订单在停机前已送出，正在向交易所查单")
                out = self._route(
                    ctl,
                    session,
                    {"outbox": [], "snapshot": res["snapshot"], "protects": res.get("protects") or []},
                    {},
                    res.get("awaiting") or [],
                )
                res["snapshot"] = out.get("snapshot") or res["snapshot"]
                session.portfolio = out.get("portfolio") or session.portfolio
        else:
            session.warm_until = int(res.get("last_bar_ts") or 0) or session.warm_until
            session.note("info", f"已加载 {sum(len(v) for v in history.values())} 根历史 bar 用于预热；预热期不交易")
            if session.source == "demo":
                session.note("warning", "演示行情：价格由时间函数生成，只用于体验流程，不能用于判断策略好坏")
        feed = "WebSocket 推送" if self.streams else "REST 轮询"
        where = f"成交走 {self.connection['env_label']}" if demo and self.connection else "平台撮合"
        session.message = f"运行中（{feed}；{where}；策略在隔离进程里运行）"
        session_dict = self._save(ctl, session)
        ctl.checkpoint(
            {"runtime": res["snapshot"], **({"oms": self.oms.state.to_dict()} if self.oms else {})},
            last_seq=session.last_bar_ts,
            summary={"status": session_dict["status"], "isolation": isolation.probe()},
        )

    def tick(self, ctl: TaskCtl) -> dict[str, Any] | None:
        services = ctl.services
        session = self._load(ctl)
        run = ctl.runs.get(ctl.run_id)
        if run["state"] == PAUSED:
            if time.monotonic() - self.last_save >= PAPER_TICK_SECONDS:
                session.last_update = int(time.time() * 1000)
                self._save(ctl, session)
            return None
        engine = paper_engine(services)
        try:
            latest, marks, _new = self._latest(ctl, session)
        except Exception as exc:
            session.note("error", f"行情采集失败：{exc}")
            session.last_update = int(time.time() * 1000)
            self._save(ctl, session)
            ctl.event("data_error", {"message": str(exc)[:300]})
            return None
        if latest is None:
            return None
        assert self.sbx is not None
        step = self.sbx.call(
            "step",
            latest=bars_to_wire(latest),
            marks=marks or None,
            funding=self._funding(ctl, session),
            delistings=self._delist(ctl),
        )
        if self.oms is not None:
            step = self._route(ctl, session, step, marks)
            self._risk_checks(ctl, session, step)
        first = (session.securities or [session.symbol])[0]
        sample = engine.on_sample(session, bars=latest.get(first, []), equity=step["equity"])
        if step.get("skipped_bars"):
            session.note("info", f"一次到来 {step['new_bars']} 根新 bar：只在最新一根上运行策略，过期信号不补成交")
        session.fills.extend(step["fills"])
        session.logs.extend(step.get("logs") or [])
        session.logs = session.logs[-500:]
        for err in step.get("errors") or []:
            session.note("error", err)
        session.portfolio = step["portfolio"]
        session.pending = step.get("pending", 0)
        session.last_bar_ts = max(session.last_bar_ts, step.get("last_bar_ts") or 0)
        ws = ctl.run["workspace_id"]
        # T09：风控层拦下新开仓 → 告警。按**原因码**判定而不是按文案，
        # 文案会随措辞调整而漂移，原因码是稳定的契约。
        from ..notify import alerts

        for rej in step.get("rejected") or []:
            if rej.get("code") in alerts.RISK_PAUSE_CODES:
                alerts.raise_for_paper_risk_pause(
                    services.db,
                    workspace_id=ws,
                    session_id=ctl.run_id,
                    reason_code=rej["code"],
                    reason=rej.get("reason") or rej.get("message") or "",
                )
        ctl.services.runs.save_fills(ctl.run_id, ws, step["fills"])
        ctl.services.runs.save_signals(ctl.run_id, ws, step.get("signals") or [])
        if step.get("new_bars") and step.get("nav"):
            ctl.services.runs.save_nav(ctl.run_id, [step["nav"]])
        for f in step["fills"]:
            ctl.event("fill", f)
        if step.get("new_bars"):
            ctl.event(
                "bar",
                {
                    "last_bar_ts": step["last_bar_ts"],
                    "triggered": step["triggered"],
                    "equity": step["equity"],
                    "new_fills": len(step["fills"]),
                    "sample": sample,
                },
            )
        saved = self._save(ctl, session)
        ctl.checkpoint(
            {"runtime": step["snapshot"], **({"oms": self.oms.state.to_dict()} if self.oms else {})},
            last_seq=session.last_bar_ts,
            summary={"status": saved["status"], "equity": step["equity"], "fills": len(session.fills)},
        )
        if session.status == "completed":
            return {"status": "completed", "equity": step["equity"]}
        return None

    def command(self, ctl: TaskCtl, command: str) -> str | None:
        session = self._load(ctl)
        if command == "pause":
            session.status = "paused"
            session.message = "已暂停：不调用策略、不新增成交"
            session.note("info", "已暂停：不再调用策略、不新增成交；持仓继续按行情估值")
            self._save(ctl, session)
            return PAUSED
        if command == "resume":
            session.status = "running"
            session.message = "运行中（每根新收盘 bar 调用一次策略）"
            session.note("info", "已恢复运行：暂停期间的信号不补成交")
            self._save(ctl, session)
            return RUNNING
        if command == "unhalt" and self.oms is not None:
            self.oms.resume()
            session.note("info", "已手动解除熔断：允许开新仓")
            self._save(ctl, session)
            return None
        if command == "cancel":
            session.status = "stopped"
            session.message = "已手动停止：持仓按最后行情估值，未自动平仓"
            session.note("info", "实验已手动停止；持仓按最后行情估值，未自动平仓")
            session.last_update = int(time.time() * 1000)
            self._save(ctl, session)
            return CANCELED
        if command == "liquidate_and_stop":
            assert self.sbx is not None
            res = self.sbx.call("liquidate", reason="liquidate")
            demo = self.oms is not None
            if demo:
                # 一键停止：先熔断（不再开仓），再把平仓单送到模拟盘
                self.oms.halt("一键停止")
                res = self._route(ctl, session, res, {})
            session.fills.extend(res["fills"])
            session.portfolio = res["portfolio"]
            session.status = "stopped"
            if demo:
                session.message = "已一键停止：平仓单已送到交易所模拟盘"
                session.note("info", "一键停止：熔断开仓，并把全部持仓的卖单送到交易所模拟盘")
            else:
                session.message = "已模拟平仓并结束（模拟成交，不是真实下单）"
                session.note("info", "模拟平仓并结束：按最新收盘价 ± 成本卖出全部持仓（模拟成交，不是真实下单）")
            session.last_update = int(time.time() * 1000)
            ctl.services.runs.save_fills(ctl.run_id, ctl.run["workspace_id"], res["fills"])
            for f in res["fills"]:
                ctl.event("fill", f)
            self._save(ctl, session)
            ctl.checkpoint(
                {"runtime": res["snapshot"], **({"oms": self.oms.state.to_dict()} if demo else {})},
                summary={"status": "stopped", "equity": res["equity"]},
            )
            return COMPLETED
        return None

    def close(self) -> None:
        if self.sbx is not None:
            self.sbx.close()
            self.sbx = None
        if self.stream_key is not None:
            from ..data import stream

            ex, secs, tf = self.stream_key
            for sec in secs:
                stream.gateway().release(ex, sec, tf)
            self.stream_key = None
            self.streams = []
        if self.oms is not None:
            try:
                self.oms.broker.close()
            except Exception:
                pass


def _backfill_for(exchange: str):
    """网关重连后补数用的 REST 拉取函数（公共行情，无需密钥）。"""
    from ..exchanges import public_adapter

    adapter = public_adapter(exchange)
    return lambda sym, tf, start, end: adapter.fetch_bars(sym, tf, start, end)


# ---------------------------------------------------------------------------
# 定时 Agent 任务（每日简报 / 策略维护）
# ---------------------------------------------------------------------------
def handle_agent_job(ctl: TaskCtl) -> dict[str, Any]:
    from .routes_ops import run_digest, run_maintainer
    from .schemas import DigestRunIn, MaintainerRunIn

    job = ctl.manifest["job"]
    if job["kind"] in ("nightshift", "brief"):
        from .routes_rd import BriefIn, make_brief, run_nightshift

        if job["kind"] == "nightshift":
            sh = run_nightshift("schedule")
            return {"job_id": job["id"], "kind": "nightshift", "status": sh.get("status"), "record_id": sh.get("id")}
        b = make_brief(BriefIn(push=True, channel=job.get("channel", "console"), source="schedule"))["brief"]
        return {"job_id": job["id"], "kind": "brief", "status": "ok", "record_id": b["id"]}
    if job["kind"] == "digest":
        out = run_digest(DigestRunIn(channel=job.get("channel", "console"), dry_run=False))
    else:
        out = run_maintainer(MaintainerRunIn(channel=job.get("channel", "console"), dry_run=False))
    rec = out.get("record") or {}
    return {"job_id": job["id"], "kind": job["kind"], "status": rec.get("status"), "record_id": rec.get("id")}


# ---------------------------------------------------------------------------
# 只读账户同步 / 实盘对照
# ---------------------------------------------------------------------------
def handle_account_sync(ctl: TaskCtl) -> dict[str, Any]:
    """只读同步：余额快照 + 成交增量拉取（T01 分页 + 水位）。

    水位在这里读、在这里写，中间夹着真正的拉取：
      1) 读该连接每个交易对的水位（`fills_sync_watermarks`）；
      2) 把水位作为游标交给客户端翻页拉取（币安 fromId / OKX after）；
      3) 落库（`save_sync` 内部按 fill_key 去重，重复拉到不会产生重复行）；
      4) 只有**落库成功之后**才推进水位。

    顺序很关键：先推水位后落库的话，一旦落库失败（数据库抖动、进程被杀），
    这批成交就永远补不回来了——水位已经越过它们，下一轮会从更后面开始拉。
    宁可重复拉一次（去重兜住）也不能漏，所以水位写在最后。
    """
    from ..live.accounts import fetch_fills

    m = ctl.manifest
    s = ctl.services
    ws = ctl.run["workspace_id"]
    cid = m["connection_id"]
    symbols = m.get("symbols") or []
    row, client = s.accounts.client(ws, cid)
    try:
        balances = client.balances()
        ctl.beat(force=True)
        wm = s.accounts.watermarks(ws, cid)
        after_ids = {sym: (wm.get(sym) or {}).get("last_fill_id") for sym in symbols}
        # since 只在**该交易对还没有水位**时生效（首次同步的起点 / 用户给的窗口下界）。
        # 有水位时一律以成交号游标为准：把 since 也带上会让客户端退回时间游标，
        # 而时间边界是闭区间，会把上一笔重复拉一遍——验收要求的"第二次只请求水位之后"
        # 就会退化成"每次把整段历史重拉一遍"。
        since = m.get("since")
        since = since if any(v is None for v in after_ids.values()) else None
        fills, pages, seen = fetch_fills(client, symbols, after_ids=after_ids, since=since)
    except AppError as exc:
        # 失败时把错误写进水位表，但**不动游标**：下次同步从同一个位置重来。
        for sym in symbols:
            try:
                s.accounts.save_watermark(
                    workspace_id=ws,
                    connection_id=cid,
                    symbol=sym,
                    exchange=row["exchange"],
                    environment=row["environment"],
                    fill_id=None,
                    ts=None,
                    pages=0,
                    error=exc.message[:500],
                )
            except Exception:  # 记错误本身失败不该盖住原始错误
                logging.getLogger("quantlab.accounts").exception("写同步水位失败：%s", sym)
        s.accounts.mark_synced(cid, ctl.run_id, error=exc.message)
        if exc.code == ErrorCode.CREDENTIAL_REVOKED:
            s.db.execute("UPDATE account_connections SET status='expired' WHERE id=?", (cid,))
        # T09：只读连接失效必须主动告警。只读同步是后台静默跑的，
        # 不告警的话凭证被吊销后用户会一直以为"同步成功、只是没数据"。
        from ..notify import alerts

        alerts.raise_for_readonly_connection_invalid(
            s.db,
            workspace_id=ws,
            connection_id=cid,
            exchange=row["exchange"],
            environment=row["environment"],
            reason=exc.message,
        )
        raise
    out = s.live.save_sync(
        run_id=ctl.run_id,
        workspace_id=ws,
        mode=ctl.run["mode"],
        balances=balances,
        fills=fills,
        account_id=cid,
    )
    # 水位：游标取"本次拉到的最大成交号"，时间取"本次拉到的最大成交时间"。
    # 拉不到任何成交时两者都是 None，save_watermark 会保留旧水位不后退。
    per_symbol: dict[str, dict[str, Any]] = {sym: {"new": 0, "ts": None} for sym in symbols}
    for f in fills:
        item = per_symbol.get(f["instrument"])
        if item is None:
            continue
        item["new"] += 1
        item["ts"] = max(item["ts"] or 0, int(f["ts"] or 0)) or None
    for sym in symbols:
        s.accounts.save_watermark(
            workspace_id=ws,
            connection_id=cid,
            symbol=sym,
            exchange=row["exchange"],
            environment=row["environment"],
            fill_id=seen.get(sym),
            ts=per_symbol[sym]["ts"],
            new_fills=per_symbol[sym]["new"],
            pages=pages if len(symbols) == 1 else 0,
        )
    s.accounts.mark_synced(cid, ctl.run_id)
    return {
        **out,
        "connection_id": cid,
        "exchange": row["exchange"],
        "environment": row["environment"],
        "pages": pages,
        "watermarks": {sym: seen.get(sym) for sym in symbols},
    }


def handle_compare(ctl: TaskCtl) -> dict[str, Any]:
    m = ctl.manifest
    rep = ctl.services.live.compare(
        workspace_id=ctl.run["workspace_id"],
        user_id=ctl.run["created_by"],
        sim_run_id=m["sim_run_id"],
        live_run_ids=m["live_run_ids"],
        mapping=m.get("mapping"),
        window_ms=int(m.get("window_ms") or 7_200_000),
    )
    summary = {k: v for k, v in rep["summary"].items() if k != "attributed_result"}
    return {"comparison_id": rep["id"], "summary": summary}


def probe_network(services: Any) -> None:
    """worker 周期钩子：探测行情接口可达性（72 小时网络实测）。"""
    from ..notify import alerts
    from ..platform import netprobe

    netprobe.probe_adapters(services.db, services.market.adapters)
    # T09：判定变成 UNREACHABLE 就按订阅规则发告警。
    # 只读 netprobe 已经算好的 verdict，不自己重算一遍——否则"界面显示的判定"
    # 与"告警依据的判定"会漂移成两套逻辑。
    alerts.report_unreachable_network(services.db)


def poll_bots(services: Any) -> None:
    """worker 周期钩子：Telegram 机器人长轮询（飞书 / 企业微信走回调，不用轮询）。"""
    from ..notify import bot

    bot.poll_telegram(services)


def check_clock(services: Any) -> None:
    """worker 周期钩子：测交易所时间偏差，签名按交易所时间（见 platform/watchdog.py）。"""
    from ..platform import watchdog

    watchdog.check_clock(services)


def deliver_alerts(services: Any) -> dict[str, Any]:
    """worker 周期钩子：投递告警队列（T09）。

    告警必须在 worker 里发而不是在 API 请求线程里发：发通知是网络 I/O，
    第三方 webhook 慢一点就会把请求拖住，而"发通知"失败也绝不该让业务请求失败。
    队列没人 drain 时 `/api/alerts` 会一直显示 pending——这是刻意保留的信号，
    让"worker 没在跑"这件事在界面上看得见。

    返回投递计数（sent/retry/failed），让 worker 日志与运维能直接看出
    "有告警一直发不出去"，而不是只能看到一行"钩子跑过了"。
    """
    from ..notify import alerts

    return alerts.deliver_pending(services)


def storage_auto_cleanup(services: Any) -> dict[str, Any]:
    """worker 周期钩子：磁盘达到管理员设置的阈值时，按分类清理最早的数据。"""
    from ..platform import storage

    return storage.auto_cleanup(services)


def reload_config(services: Any) -> None:
    """worker 周期钩子：模型服务配置在 API 进程里修改，worker 定期重新加载。"""
    services.apply_runtime_config()


def scan_schedules(services: Any) -> None:
    """worker 周期钩子：把到期的定时任务登记为 run；每日研究计划到点也登记一次（每天一次，幂等）。"""
    from .routes_ops import due_schedules

    due_schedules(services)
    try:
        scan_nightshift(services)
    except Exception:
        logging.getLogger("quantlab.nightshift").exception("定时任务检查失败")


def scan_nightshift(services: Any) -> list[str]:
    from ..platform import context as _ctx

    try:
        from .routes_rd import nightshift_due
    except ImportError:  # numpy 没装
        return []
    created = []
    day = time.strftime("%Y-%m-%d")
    for row in services.db.fetchall("SELECT id FROM workspaces"):
        with _ctx.bind(row["id"]):
            if not nightshift_due():
                continue
            run = services.runs.create(
                workspace_id=row["id"],
                user_id="scheduler",
                kind="agent_job",
                mode="agent",
                title="定时任务执行",
                manifest={"job": {"id": "nightshift", "kind": "nightshift"}, "fired_at": day},
                idempotency_key=f"nightshift:{row['id']}:{day}",
            )
            created.append(run["id"])
    return created


# ---------------------------------------------------------------------------
# 组装与派发
# ---------------------------------------------------------------------------
def scan_datahub(services: Any) -> dict[str, Any]:
    """worker 周期钩子（每 10 分钟）：开启了自动同步、且到了间隔的工作区，把已有研究数据补到最新。"""
    from ..platform import context as _ctx

    try:
        from ..research.datahub import DataHub
    except ImportError:  # numpy 没装
        return {}
    done: dict[str, Any] = {}
    for row in services.db.fetchall("SELECT id FROM workspaces"):
        ws = row["id"]
        with _ctx.bind(ws):
            try:
                hub = DataHub(services.store, secrets_fn=services.read_secrets)
                if hub.auto_due():
                    done[ws] = hub.auto_sync()
            except Exception:
                logging.getLogger("quantlab.datahub").exception("研究数据自动同步失败")
    return done


def scan_lifecycle(services: Any) -> dict[str, Any]:
    """worker 周期钩子（每小时）：运行中的实验做衰减判定；到期的模型登记重训任务（每个模型每天最多一次）。"""
    from ..platform import context as _ctx
    from ..research import retrain as RT
    from ..research.model_lib import ModelLibrary
    from .routes_lifecycle import check_all

    checked, queued = 0, []
    day = time.strftime("%Y-%m-%d")
    for row in services.db.fetchall("SELECT id FROM workspaces"):
        ws = row["id"]
        with _ctx.bind(ws):
            try:
                checked += len(check_all(services, ws))
            except Exception:
                logging.getLogger("quantlab.lifecycle").exception("衰减判定失败")
            for m in ModelLibrary(services.store.for_workspace(ws)).list():
                if RT.due(m):
                    run = services.runs.create(
                        workspace_id=ws,
                        user_id="scheduler",
                        kind="model_retrain",
                        mode="analysis",
                        title=f"模型重训 · {m['name']}",
                        manifest={"model_id": m["id"]},
                        idempotency_key=f"retrain:{ws}:{m['id']}:{day}",
                    )
                    queued.append(run["id"])
    return {"checked": checked, "queued": queued}


def handle_model_retrain(ctl: TaskCtl) -> dict[str, Any]:
    from ..platform import context as _ctx
    from ..research import retrain as RT
    from ..research.model_lib import ModelLibrary
    from .routes_models import _panel

    ws = ctl.run["workspace_id"]
    with _ctx.bind(ws, user_id=ctl.run.get("created_by") or "scheduler"):
        lib = ModelLibrary(ctl.services.store.for_workspace(ws))
        rec = lib.get(ctl.manifest["model_id"])
        if rec is None:
            return {"skipped": "模型已删除"}
        asof = int(time.time() * 1000)
        spec = {**rec["spec"], "start": None, "end": asof, "source": rec["spec"].get("source") or "auto"}
        out = RT.retrain(lib, rec["id"], _panel(spec), asof_ms=asof, reason="scheduled")
        return {
            "model_id": rec["id"],
            **{k: out["retrain"].get(k) for k in ("accepted", "why", "oos_rank_ic", "run_id")},
        }


BATCH = {
    "workflow": handle_workflow,
    "workflow_export": handle_export,
    "backtest": handle_backtest,
    "optimize": handle_optimize,
    "template_backtest": handle_template_backtest,
    "agent_job": handle_agent_job,
    "account_sync": handle_account_sync,
    "compare": handle_compare,
    "model_retrain": handle_model_retrain,
}
RESIDENT = {"paper": PaperDriver}

_INLINE: Worker | None = None


def worker_mode() -> str:
    return os.environ.get("QAGENT_WORKER", "external")


def build_worker(services: Any, **kw: Any) -> Worker:
    return Worker(
        services,
        batch=dict(BATCH),
        resident=dict(RESIDENT),
        periodic=[
            (30.0, deliver_alerts),
            (60.0, scan_schedules),
            (300.0, probe_network),
            (60.0, reload_config),
            (600.0, check_clock),
            (5.0, poll_bots),
            (3600.0, scan_lifecycle),
            (600.0, scan_datahub),
            (600.0, storage_auto_cleanup),
        ],
        **kw,
    )


def inline_worker(services: Any) -> Worker:
    global _INLINE
    if _INLINE is None or _INLINE.services is not services:
        _INLINE = build_worker(services, owner=f"inline:{os.getpid()}")
    return _INLINE


def dispatch(services: Any, run: dict[str, Any], *, wait_seconds: float = 0.0) -> dict[str, Any]:
    """登记之后：inline 模式立即在当前线程执行；否则（可选）等待独立 worker 完成。"""
    if worker_mode() == "inline":
        return inline_worker(services).execute_now(run["id"]) or run
    if wait_seconds <= 0:
        return run
    deadline = time.monotonic() + wait_seconds
    cur = run
    while time.monotonic() < deadline:
        cur = services.runs.get(run["id"]) or cur
        if cur["state"] in TERMINAL:
            return cur
        time.sleep(0.25)
    return cur


def raise_if_failed(run: dict[str, Any]) -> None:
    if run.get("state") == "FAILED":
        code = run.get("error_code") or ErrorCode.TASK_FAILED
        raise AppError(code, run.get("error") or "任务失败", details={"run_id": run["id"]})
