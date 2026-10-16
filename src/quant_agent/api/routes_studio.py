"""策略工作室：文件化策略仓库 + 实时模拟 + 实盘对照。

对标聚宽的三段式体验：
  研究（写策略）→ 回测/实时模拟（同一份代码）→ 实盘对照（逐笔归因）

关键约定：
- 每个策略是磁盘上的一个目录，含 strategy.py / meta.json / params.json / versions/。
- 规则策略会被编译成**等价的 Python 源码**，可以读、可以改、可以导出。
- 实时模拟与回测共用同一份策略文件，不允许出现"两份代码"。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..engine.paper import (
    PaperSession,
    parse_live_fills_csv,
    reconcile,
)
from ..engine.research import BacktestSpec
from ..exchanges.base import OrderIntent
from ..platform import context
from ..platform.errors import AppError, ErrorCode
from ..sdk import RuntimeCost, StrategyLoadError, check_source, declared_params, load_source
from ..store import Collections
from ..strategies import lang as L
from ..strategies.dsl import DslError
from ..strategies.repo import StrategyRepository, rule_to_python
from ..strategies.rules import RULE_TEMPLATE, RuleError, validate_rule_spec
from . import tasks
from .deps import get_services
from .routes_dsl import register_version
from .routes_runs import apply_inline, enqueue
from .schemas import (
    OrderTicketIn,
    PaperStartIn,
    ReconcileIn,
    StrategyFileIn,
    StrategyFromRuleIn,
    StudioBacktestIn,
)

router = APIRouter(prefix="/api/studio", tags=["策略工作室"])


def _repo() -> StrategyRepository:
    services = get_services()
    root = services.store.root / "strategies"
    return StrategyRepository(root)


# ---------------------------------------------------------------------------
# 策略文件
# ---------------------------------------------------------------------------
@router.get("/strategies")
def list_strategies(kind: str | None = None) -> dict:
    """列出磁盘上的策略目录。每个策略 = 一个 .py 文件 + 元数据。"""
    from .routes_collab import filter_visible

    repo = _repo()
    items = filter_visible("strategy", [r.to_dict(include_source=False) for r in repo.list(kind=kind)], "slug")
    return {
        "strategies": items,
        "root": str(repo.root),
        "layout": [
            "strategies/<slug>/strategy.py —— 策略源码，可直接编辑、可 git diff",
            "strategies/<slug>/meta.json —— 名称、团队、类型、版本、状态、批准人",
            "strategies/<slug>/params.json —— 参数（规则策略的规则也在里面）",
            "strategies/<slug>/versions/ —— 每次保存的历史快照，可回滚对照",
        ],
    }


@router.get("/strategies/{slug}")
def get_strategy(slug: str) -> dict:
    from .routes_collab import guard_visible

    repo = _repo()
    rec = repo.get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    guard_visible("strategy", slug)
    return {
        "strategy": rec.to_dict(),
        "versions": repo.versions(slug),
        "issues": L.issues(L.lang_of(rec.meta.kind), rec.source),
        "lang": L.lang_of(rec.meta.kind),
    }


@router.post("/strategies")
def create_or_update_strategy(payload: StrategyFileIn) -> dict:
    """新建或保存策略源码。保存前做静态检查——越界的调用在这里就被拦下。"""
    repo = _repo()
    issues = check_source(payload.code)
    if issues and not payload.allow_issues:
        raise HTTPException(400, "；".join(issues))

    declared = declared_params(payload.code)
    rec = repo.save(
        name=payload.name,
        source=payload.code,
        params=declared or payload.params,
        team=payload.team,
        kind=payload.kind,
        origin=payload.source,
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        securities=payload.securities,
        notes=payload.notes,
        slug=payload.slug,
        human_approved=payload.human_approved,
    )
    return {"strategy": rec.to_dict(), "issues": issues}


@router.delete("/strategies/{slug}")
def delete_strategy(slug: str) -> dict:
    if not _repo().delete(slug):
        raise HTTPException(404, "策略不存在")
    return {"ok": True}


@router.post("/strategies/{slug}/approve")
def approve_strategy(slug: str) -> dict:
    """人工批准 = 冻结当前版本。只有冻结版本能进入实时模拟；改代码或参数会产生新版本，需要重新批准。"""
    repo = _repo()
    rec = repo.get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    version = register_version(rec)
    version = get_services().versions.freeze(context.current_workspace(), version["id"])
    rec = repo.update_meta(slug, human_approved=True, status="tested")
    get_services().audit.record(
        "strategy.approve",
        object_type="strategy",
        object_id=slug,
        details={"version_id": version["id"], "version_no": version["version_no"]},
    )
    return {"strategy": rec.to_dict(), "version": version}


@router.get("/strategies/{slug}/export", response_class=PlainTextResponse)
def export_strategy(slug: str) -> PlainTextResponse:
    """导出成一份可以独立运行的 .py。"""
    text = _repo().export(slug)
    if text is None:
        raise HTTPException(404, "策略不存在")
    return PlainTextResponse(
        text,
        media_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{slug}.py"'},
    )


# ---------------------------------------------------------------------------
# 策略说明书与策略包（.qstrat）
# ---------------------------------------------------------------------------
@router.get("/strategies/{slug}/spec")
def get_spec(slug: str) -> dict:
    """策略说明书。没写过时给一份从元数据和文档字符串拼出来的初稿（draft=true）。"""
    from ..strategies.package import default_spec

    repo = _repo()
    rec = repo.get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    text = repo.read_spec(slug)
    if text:
        return {"spec": text, "draft": False}
    return {
        "spec": default_spec(
            name=rec.meta.name,
            notes=rec.meta.notes,
            securities=rec.meta.securities,
            timeframe=rec.meta.timeframe,
            params=rec.params,
            source=rec.source,
        ),
        "draft": True,
    }


@router.put("/strategies/{slug}/spec")
def put_spec(slug: str, payload: dict) -> dict:
    text = str(payload.get("spec") or "")
    if len(text) > 200_000:
        raise HTTPException(400, "说明书太长（上限 20 万字）")
    try:
        _repo().write_spec(slug, text)
    except FileNotFoundError as exc:
        raise HTTPException(404, "策略不存在") from exc
    return {"spec": text, "draft": False}


def _lineage_for(slug: str) -> dict[str, Any] | None:
    """研究纪律台账里这个策略的记录（试验次数、DSR、紧封测试）。没有就不放进包里。"""
    try:
        from ..research.discipline import strategy_record

        return strategy_record(get_services().store, slug)
    except Exception:
        return None


@router.get("/strategies/{slug}/package")
def export_package(slug: str) -> Response:
    """导出策略包 .qstrat：代码 + 参数 + 说明书 + 数据片段 + 标准答案（现场在隔离进程里跑出来）。"""
    from ..strategies import package as P

    repo = _repo()
    rec = repo.get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    author = rec.meta.author or context.current().user_name or ""
    try:
        data, _manifest = P.build(
            name=rec.meta.name,
            slug=rec.meta.slug,
            kind=rec.meta.kind,
            source=rec.source,
            params=rec.params,
            securities=rec.meta.securities or [rec.meta.symbol],
            timeframe=rec.meta.timeframe,
            author=author,
            team=rec.meta.team,
            version=rec.meta.version,
            notes=rec.meta.notes,
            spec=repo.read_spec(slug) or None,
            lineage=_lineage_for(slug),
        )
    except Exception as exc:
        raise HTTPException(400, f"打包失败：{type(exc).__name__}: {exc}") from exc
    get_services().audit.record("strategy.package_export", object_type="strategy", object_id=slug)
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}{P.EXTENSION}"'},
    )


async def _package_bytes(request: Request) -> bytes:
    from ..strategies import package as P

    data = await request.body()
    if not data:
        raise HTTPException(400, "请上传 .qstrat 文件（请求体为文件内容）")
    if len(data) > P.MAX_TOTAL_BYTES:
        raise HTTPException(400, "策略包太大")
    return data


@router.post("/packages/inspect")
async def inspect_package(request: Request) -> dict:
    """导入前检查（不入库）：格式、兼容性、安全检查、隔离试跑、标准答案核对。"""
    from starlette.concurrency import run_in_threadpool

    from ..strategies import package as P

    data = await _package_bytes(request)
    try:
        rep = await run_in_threadpool(P.inspect, data)
    except P.PackageError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"report": rep.to_dict(), "source": rep.source}


@router.post("/packages/import")
async def import_package(request: Request, name: str | None = Query(None), slug: str | None = Query(None)) -> dict:
    """导入策略包：先检查；有 error 级问题拒绝入库。导入的策略一律未批准，要你看过代码再批准。"""
    from starlette.concurrency import run_in_threadpool

    from ..strategies import package as P

    data = await _package_bytes(request)
    try:
        rep = await run_in_threadpool(P.inspect, data)
    except P.PackageError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not rep.importable:
        bad = [c for c in rep.checks if not c.ok and c.level == "error"]
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            "策略包没有通过检查：" + "；".join(f"{c.name}：{c.detail}" for c in bad),
            details={"report": rep.to_dict()},
        )
    m = rep.manifest
    repo = _repo()
    target = slug or (m.get("slug") if m.get("slug") and not repo.exists(str(m.get("slug"))) else None)
    target = target or repo.unique_slug(str(m.get("slug") or m.get("name") or "imported"))
    securities = [str(s) for s in (m.get("securities") or [])] or ["BTC/USDT"]
    rec = repo.save(
        name=name or str(m.get("name") or target),
        source=rep.source,
        params=rep.params,
        team=str(m.get("team") or "未标注团队"),
        kind=str(m.get("kind") or "code"),
        origin="import",
        symbol=securities[0],
        timeframe=str(m.get("timeframe") or "1h"),
        securities=securities,
        notes=f"从策略包导入（作者：{m.get('author') or '未署名'}，原版本 {m.get('version') or '未知'}）",
        slug=target,
        human_approved=False,
        author=str(m.get("author") or ""),
        package={
            "format": m.get("format"),
            "imported_at": int(time.time() * 1000),
            "original_version": m.get("version") or "",
            "author": m.get("author") or "",
            "golden": rep.golden,
            "actual": rep.actual,
            "golden_match": rep.golden_match,
            "demo_ready": rep.demo_ready,
            "requires": list(m.get("requires") or []),
            "lineage": rep.lineage,
        },
    )
    if rep.spec:
        repo.write_spec(rec.meta.slug, rep.spec)
    get_services().audit.record(
        "strategy.package_import",
        object_type="strategy",
        object_id=rec.meta.slug,
        details={"golden_match": rep.golden_match, "author": m.get("author") or ""},
    )
    return {"strategy": rec.to_dict(include_source=False), "report": rep.to_dict()}


@router.get("/strategies/{slug}/versions/{filename}", response_class=PlainTextResponse)
def read_version(slug: str, filename: str) -> PlainTextResponse:
    text = _repo().read_version(slug, filename)
    if text is None:
        raise HTTPException(404, "版本不存在")
    return PlainTextResponse(text, media_type="text/x-python; charset=utf-8")


@router.post("/strategies/{slug}/check")
def check_strategy(slug: str) -> dict:
    """静态检查 + 试加载。返回问题列表与调度项，便于在编辑器里即时反馈。"""
    rec = _repo().get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    lang = L.lang_of(rec.meta.kind)
    issues = L.issues(lang, rec.source)
    if issues:
        return {"ok": False, "issues": issues, "schedules": []}
    if lang == L.DSL:
        compiled = L.load(lang, rec.source, name=slug).compiled
        return {
            "ok": True,
            "issues": [],
            "functions": ["initialize", "handle_data"],
            "summary": compiled.summary,
            "warmup_bars": compiled.warmup_bars,
            "warnings": compiled.warnings,
        }
    try:
        module = load_source(rec.source, name=slug, path=rec.path)
    except StrategyLoadError as exc:
        return {"ok": False, "issues": [str(exc)], "schedules": []}
    return {
        "ok": True,
        "issues": [],
        "functions": [
            name
            for name in ("initialize", "handle_data", "before_trading_start", "after_trading_end", "on_order_filled")
            if module.has(name)
        ],
    }


# ---------------------------------------------------------------------------
# 规则 → Python
# ---------------------------------------------------------------------------
@router.post("/strategies/from-rule")
def strategy_from_rule(payload: StrategyFromRuleIn) -> dict:
    """把 JSON 规则编译成一份可读可改的 Python 策略并落盘。"""
    try:
        spec = validate_rule_spec({**RULE_TEMPLATE, **payload.rule})
    except RuleError as exc:
        raise HTTPException(400, str(exc)) from exc
    source = rule_to_python(spec)
    issues = check_source(source)
    if issues:
        raise HTTPException(500, f"规则编译结果未通过自检：{'；'.join(issues)}")

    repo = _repo()
    rec = repo.save(
        name=spec.name,
        source=source,
        params=spec.to_dict(),
        team=spec.team,
        kind="rule",
        symbol=payload.symbol,
        timeframe=payload.timeframe,
        notes=f"由规则编译生成，版本 {spec.version}",
    )
    return {"strategy": rec.to_dict(), "rule_version": spec.version}


@router.post("/rules/preview-python", response_class=PlainTextResponse)
def preview_rule_python(payload: StrategyFromRuleIn) -> PlainTextResponse:
    """只预览编译结果，不落盘。"""
    try:
        spec = validate_rule_spec({**RULE_TEMPLATE, **payload.rule})
    except RuleError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PlainTextResponse(rule_to_python(spec), media_type="text/x-python; charset=utf-8")


# ---------------------------------------------------------------------------
# 跑策略：回测（与平台事件回测同口径，但直接执行策略文件）
# ---------------------------------------------------------------------------
def _bars_for(services, symbol: str, timeframe: str, limit: int, dataset_id: str | None):
    from ..data.quality import DataSet, parse_csv

    if dataset_id:
        saved = services.store.get(Collections.DATASETS, dataset_id)
        if saved is None:
            raise HTTPException(404, "数据集不存在")
        bars = parse_csv(
            saved["content"], symbol=saved.get("symbol", symbol), timeframe=saved.get("timeframe", timeframe)
        )
        return DataSet(saved.get("symbol", symbol), saved.get("timeframe", timeframe), bars, "csv").freeze(
            f"用户 CSV · {saved.get('filename', '')}"
        )
    return services.market.dataset(symbol, timeframe, limit=limit)


@router.post("/backtest")
def studio_backtest(payload: StudioBacktestIn) -> Any:
    """跑一份策略源码（或已保存的策略），返回与平台同口径的结果。执行在 worker 的隔离进程里。"""
    services = get_services()
    if payload.slug:
        rec = _repo().get(payload.slug)
        if rec is None:
            raise HTTPException(404, "策略不存在")
        source, name = rec.source, rec.meta.name
    elif payload.code:
        source, name = payload.code, payload.name or "未命名策略"
    else:
        raise HTTPException(400, "请提供 slug 或 code")
    issues = check_source(source)
    if issues:
        raise HTTPException(400, "；".join(issues))
    spec = BacktestSpec(
        securities=payload.securities or [payload.symbol],
        timeframe=payload.timeframe,
        bars_limit=payload.bars_limit,
        warmup_bars=payload.warmup_bars,
        initial_cash=payload.initial_cash,
        taker_fee=payload.taker_fee,
        slippage_bps=payload.slippage_bps,
        spread_bps=payload.spread_bps,
        rebalance_tolerance=payload.rebalance_tolerance,
    )
    m = {
        "source": source,
        "module_name": "studio",
        "name": name,
        "slug": payload.slug,
        "spec": spec.to_dict(),
        "persist": False,
        "dataset_id": payload.dataset_id,
    }
    run = enqueue(kind="backtest", mode="backtest", title=f"编辑器运行 · {name}", manifest=m)
    run = tasks.dispatch(services, run, wait_seconds=60)
    tasks.raise_if_failed(run)
    if run["state"] != "COMPLETED":
        return JSONResponse(status_code=202, content={"run_id": run["id"], "state": run["state"]})
    body = services.store.get_blob("run_results", run["id"]) or {}
    body["dataset"] = (body.get("datasets") or [{}])[0]
    body["symbols"] = body.get("securities") or spec.securities
    body["strategy_name"] = name
    body["run_id"] = run["id"]
    return body


# ---------------------------------------------------------------------------
# 实时模拟：每个实验是一个 kind=paper 的 run，由 worker 常驻推进并按 bar 写检查点
# ---------------------------------------------------------------------------
PAPER_TICK_SECONDS = tasks.PAPER_TICK_SECONDS
RUN_TO_SESSION = {"QUEUED": "queued", "RECOVERING": "recovering", "FAILED": "failed"}


def _session_view(row: dict[str, Any]) -> dict[str, Any]:
    """会话文档 + 运行状态（run 状态是唯一真相）。"""
    run = get_services().runs.get(row["id"])
    if run:
        row = dict(row)
        row["run_state"] = run["state"]
        if run["state"] in RUN_TO_SESSION:
            row["status"] = RUN_TO_SESSION[run["state"]]
        if run["state"] == "FAILED":
            row["message"] = run.get("error") or row.get("message", "")
        row["lease_owner"] = run.get("lease_owner")
        row["attempts"] = run.get("attempts")
        row["live"] = run["state"] in ("RUNNING", "PAUSED")
    return row


def _paper_or_404(session_id: str) -> dict[str, Any]:
    saved = get_services().store.get(Collections.PAPER_SESSIONS, session_id)
    if saved is None:
        raise HTTPException(404, "实验不存在")
    return saved


@router.get("/paper")
def list_paper_sessions(limit: int = Query(20, ge=1, le=100)) -> dict:
    services = get_services()
    rows = [_session_view(r) for r in services.store.list(Collections.PAPER_SESSIONS, limit=limit)]
    return {
        "sessions": rows,
        "tick_seconds": PAPER_TICK_SECONDS,
        "worker_mode": tasks.worker_mode(),
        "conventions": [
            "实时模拟使用真实行情 + 本地虚拟账户，不代表真实成交能力",
            "由独立 worker 执行：每根新收盘 bar 调用一次策略，委托按下一根 bar 开盘价 ± 成本成交，与回测同一套撮合代码",
            "每处理完一根 bar 写一次检查点；worker 崩溃后从检查点恢复，已处理的事件与信号不会重复成交",
            "停机时段不补成交；恢复后过期信号作废",
            "7 天 / 30 天检查点需要实际经过 168 / 720 小时，回放与加速不能替代",
            "手动停止不会自动平仓；“模拟平仓并结束”会按最新价格 ± 成本卖出全部持仓（仍是模拟成交）",
        ],
    }


@router.post("/paper/start")
def start_paper(payload: PaperStartIn) -> dict:
    """登记一次实时模拟。策略源码在启动时冻结进运行清单，之后改策略文件不影响这次实验。"""
    return start_session(payload)


def start_session(payload: PaperStartIn, *, live: dict[str, Any] | None = None) -> dict:
    """纸面 / 模拟盘 / 微实盘共用的启动流程。live 只由微实盘入口（routes_microlive）在全部确认之后传入。"""
    services = get_services()
    rec = _repo().get(payload.slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    if not rec.meta.human_approved:
        raise HTTPException(400, "该策略版本尚未人工批准。平台红线：未批准的版本不能进入实时模拟。")
    lang = L.lang_of(rec.meta.kind)
    issues = L.issues(lang, rec.source)
    if issues:
        raise HTTPException(400, "；".join(issues))
    try:
        module = L.load(lang, rec.source, name=rec.meta.slug, path=rec.path, params=payload.params or None)
    except (StrategyLoadError, DslError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # 实时模拟只运行冻结版本：批准时冻结的内容 + 本次参数（参数不同 = 新版本，需要该版本也已冻结）
    version = register_version(rec, params=module.params)
    if version["status"] == "VALIDATED" and not payload.params:
        # 旧数据：批准发生在版本表之前——批准绑定的正是这份内容，补冻结
        version = get_services().versions.freeze(context.current_workspace(), version["id"])
    if version["status"] != "FROZEN":
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            f"本次参数产生了新版本 v{version['version_no']}，尚未冻结。"
            "请先在版本列表里批准（冻结）该版本再启动实时模拟",
            details={"version_id": version["id"], "status": version["status"]},
        )
    get_services().versions.require_runnable(version, frozen=True)

    securities = payload.securities or ([payload.symbol] if payload.symbol else rec.meta.securities) or ["BTC/USDT"]
    timeframe = payload.timeframe or rec.meta.timeframe
    cost = RuntimeCost(taker_fee=payload.taker_fee, slippage_bps=payload.slippage_bps)
    demo_conn: dict[str, Any] | None = None
    if payload.execution == "demo":
        demo_conn = _check_demo_start(payload, rec)
    execution = "live" if live else payload.execution
    extra = {"live_connection_id": live["connection"]["id"], "micro": live["micro"]} if live else {}
    risk = payload.risk.model_dump() if getattr(payload, "risk", None) else {}
    if live:
        risk = {**risk, "stop_loss": risk.get("stop_loss") or live["micro"]["stop_loss"]}
    run = enqueue(
        kind="paper",
        mode="micro_live" if live else "paper",
        title=f"{'微实盘' if live else '实时模拟'} · {rec.meta.name}",
        manifest={
            "lang": lang,
            "source": rec.source,
            "module_name": rec.meta.slug,
            "name": rec.meta.name,
            "slug": rec.meta.slug,
            "version": rec.meta.version,
            "params": payload.params or None,
            "cost": cost.to_dict(),
            "risk": risk,
            "securities": securities,
            "timeframe": timeframe,
            "source_kind": payload.source,
            "exchange": payload.exchange,
            "initial_cash": payload.initial_cash,
            "execution": execution,
            "demo_connection_id": demo_conn["id"] if demo_conn else None,
            **extra,
            "oms_limits": {
                "max_order_notional": payload.max_order_notional,
                "max_orders_per_minute": payload.max_orders_per_minute,
                "max_daily_loss": payload.max_daily_loss,
            },
        },
        strategy_version_id=version["id"],
    )
    session = tasks.paper_engine(services, cost).start(
        strategy_id=rec.meta.slug,
        strategy_name=rec.meta.name,
        strategy_version=rec.meta.version,
        symbol=securities[0],
        timeframe=timeframe,
        initial_cash=payload.initial_cash,
        source=payload.source,
    )
    session.id = run["id"]
    session.securities = list(securities)
    session.exchange = payload.exchange
    session.params = dict(module.params)
    session.execution = execution
    if live:
        session.demo = {
            "connection": {k: live["connection"].get(k) for k in ("id", "exchange", "env", "env_label", "key_mask")},
            "live": True,
            "micro": live["micro"],
        }
    if demo_conn:
        session.demo = {
            "connection": {
                k: demo_conn.get(k) for k in ("id", "exchange", "env", "env_label", "key_mask", "realistic", "env_note")
            }
        }
    session.status = "queued"
    session.message = "已登记，等待 worker 认领"
    services.store.upsert(Collections.PAPER_SESSIONS, session.to_dict(max_points=100_000))
    run = tasks.dispatch(services, run)
    tasks.raise_if_failed(run)
    return {
        "session": _session_view(_paper_or_404(run["id"])),
        "run_id": run["id"],
        "strategy": rec.to_dict(include_source=False),
    }


def _check_demo_start(payload: PaperStartIn, rec: Any) -> dict[str, Any]:
    """交易所模拟盘的启动前检查：连接可用、交易所一致、导入的策略通过了标准答案核对。"""
    services = get_services()
    if payload.source != "exchange":
        raise HTTPException(400, "交易所模拟盘只能配真实行情（source=exchange）")
    if not payload.demo_connection_id:
        raise HTTPException(400, "请选择一个模拟盘连接（在「交易所模拟盘」页面先接好模拟盘 Key）")
    conn = services.demo._out(services.demo.get(payload.demo_connection_id))
    if conn.get("status") != "active":
        raise HTTPException(400, f"模拟盘连接状态为 {conn.get('status')}，请先重新测试或重新连接")
    if conn["exchange"] != payload.exchange:
        raise HTTPException(
            400,
            f"行情来自 {payload.exchange}，模拟盘连接是 {conn['exchange']}：两者必须是同一家交易所，"
            "否则成交价和行情对不上",
        )
    pkg = rec.meta.package or {}
    if pkg and not pkg.get("demo_ready"):
        raise HTTPException(
            400,
            "这个策略是从策略包导入的，导入时标准答案没有核对通过（代码被改过或结果不一致），不能上模拟盘。"
            "可以照常回测和纸面模拟",
        )
    return conn


def _command(session_id: str, command: str) -> dict:
    services = get_services()
    _paper_or_404(session_id)
    run = services.runs.require(session_id, context.current_workspace())
    if run["state"] in ("COMPLETED", "CANCELED", "FAILED"):
        return {"session": _session_view(_paper_or_404(session_id)), "applied": False}
    services.runs.command(session_id, context.current_workspace(), context.current().user_id, command)
    services.audit.record(f"paper.{command}", object_type="run", object_id=session_id)
    if tasks.worker_mode() == "inline":
        apply_inline(session_id, force_tick=(command == "tick"))
    return {"session": _session_view(_paper_or_404(session_id)), "applied": True}


@router.post("/paper/{session_id}/tick")
def paper_tick(session_id: str) -> dict:
    """请求立即采集一次（worker 每 30 秒也会自动采集）。"""
    out = _command(session_id, "tick")
    s = out["session"]
    return {**out, "tick": {"new_fills": len(s.get("fills") or []), "status": s.get("status")}}


@router.post("/paper/{session_id}/pause")
def pause_paper(session_id: str) -> dict:
    return _command(session_id, "pause")


@router.post("/paper/{session_id}/resume")
def resume_paper(session_id: str) -> dict:
    return _command(session_id, "resume")


@router.post("/paper/{session_id}/stop")
def stop_paper(session_id: str) -> dict:
    """手动停止：不自动平仓。"""
    return _command(session_id, "cancel")


@router.post("/paper/{session_id}/liquidate")
def liquidate_paper(session_id: str) -> dict:
    """模拟平仓并结束：按最新价格 ± 成本卖出全部持仓（模拟成交）。"""
    return _command(session_id, "liquidate_and_stop")


@router.post("/paper/{session_id}/unhalt")
def paper_unhalt(session_id: str) -> dict:
    """解除交易所模拟盘的熔断（当日亏损超限、对账不一致、一键停止之后需要你手动确认）。"""
    return _command(session_id, "unhalt")


@router.post("/paper/{session_id}/recover")
def recover_paper(session_id: str) -> dict:
    """演示“worker 崩溃”：丢弃内存中的运行时并让租约过期 → RECOVERING。下一次推进从检查点恢复。"""
    services = get_services()
    _paper_or_404(session_id)
    run = services.runs.require(session_id, context.current_workspace())
    if tasks.worker_mode() != "inline":
        raise HTTPException(409, "独立 worker 模式下请直接停止 worker 进程来验证恢复；租约过期后会自动转入 RECOVERING")
    if run["state"] in ("RUNNING", "PAUSED"):
        tasks.inline_worker(services).simulate_crash(session_id)
    return {"session": _session_view(_paper_or_404(session_id))}


@router.get("/paper/{session_id}/report")
def paper_report(session_id: str) -> dict:
    services = get_services()
    session = PaperSession.from_dict(_paper_or_404(session_id))
    report = tasks.paper_engine(services).report(session, timeframe=session.timeframe)
    report["session"] = _session_view(report["session"])
    report["live"] = report["session"].get("live", False)
    return report


@router.get("/paper/{session_id}/terminal")
def paper_terminal(
    session_id: str,
    security: str = Query("", description="看哪个标的；默认第一个"),
    bars: int = Query(300, ge=20, le=1500),
) -> dict:
    """交易终端一次取齐：会话、K 线（含成交标记所需的成交记录）、委托、行情状态。

    K 线取已收盘 bar；正在形成的那根由前端轮询 /api/demo/live 叠加。取不到行情不报错，
    返回空 K 线和原因，终端其余部分照常显示。
    """
    services = get_services()
    row = _session_view(_paper_or_404(session_id))
    session = PaperSession.from_dict(row)
    sec = security or (session.securities or [session.symbol])[0]
    if sec not in (session.securities or [session.symbol]):
        raise HTTPException(400, f"本实验没有标的 {sec}")
    session.securities = [sec]
    kline: list[dict[str, Any]] = []
    kline_error = ""
    try:
        got = tasks.latest_bars(services, session, bars).get(sec) or []
        kline = [
            {"t": b.open_time, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume}
            for b in got
            if b.closed
        ]
    except Exception as exc:  # 行情失败不影响终端其他部分
        kline_error = str(exc)[:200]
    fills = [f for f in row.get("fills") or [] if (f.get("security") or f.get("symbol") or sec) == sec]
    return {
        "session": row,
        "security": sec,
        "securities": row.get("securities") or [row.get("symbol")],
        "bars": kline,
        "bars_error": kline_error,
        "fills": fills[-500:],
        "orders": (row.get("demo") or {}).get("orders") or [],
        "feed": {"kind": row.get("feed") or "rest", "status": row.get("feed_status") or {}},
        "live_endpoint": f"/api/demo/live?exchange={row.get('exchange') or 'binance'}&symbol={sec}"
        f"&timeframe={row.get('timeframe')}"
        if row.get("source") == "exchange"
        else "",
    }


@router.delete("/paper/{session_id}")
def delete_paper(session_id: str) -> dict:
    services = get_services()
    run = services.runs.get(session_id, workspace_id=context.current_workspace())
    if run and run["state"] not in ("COMPLETED", "CANCELED", "FAILED"):
        raise HTTPException(409, "实验仍在运行，请先停止再删除")
    if not services.store.delete(Collections.PAPER_SESSIONS, session_id):
        raise HTTPException(404, "实验不存在")
    return {"ok": True}


# ---------------------------------------------------------------------------
# 实盘对照
# ---------------------------------------------------------------------------
@router.post("/reconcile")
def run_reconcile(payload: ReconcileIn) -> dict:
    """快速逐笔对照（兼容接口）。模拟侧来自一次回测 run（由 worker 执行），实盘侧来自 CSV。

    正式流程请使用 /api/imports + /api/comparisons：真实记录独立入库、归因带置信度。
    """
    services = get_services()
    live_fills: list[dict[str, Any]] = []
    if payload.live_csv:
        try:
            live_fills = [f.to_dict() for f in parse_live_fills_csv(payload.live_csv)]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    simulated: list[dict[str, Any]] = []
    sim_orders: list[dict[str, Any]] | None = None
    sim_equity: list[dict[str, Any]] | None = None
    if payload.slug:
        rec = _repo().get(payload.slug)
        if rec is None:
            raise HTTPException(404, "策略不存在")
        lang = L.lang_of(rec.meta.kind)
        spec = BacktestSpec(
            securities=[payload.symbol],
            timeframe=payload.timeframe,
            bars_limit=payload.bars_limit,
            warmup_bars=payload.warmup_bars,
            initial_cash=payload.initial_cash,
        )
        run = enqueue(
            kind="backtest",
            mode="backtest",
            title=f"对照用回测 · {rec.meta.name}",
            manifest={
                "lang": lang,
                "source": rec.source,
                "module_name": rec.meta.slug,
                "name": rec.meta.name,
                "slug": rec.meta.slug,
                "spec": spec.to_dict(),
                "persist": False,
                "dataset_id": payload.dataset_id,
            },
            strategy_version_id=register_version(rec)["id"],
        )
        run = tasks.dispatch(services, run, wait_seconds=60)
        tasks.raise_if_failed(run)
        simulated = [
            {
                **f,
                "client_order_id": f.get("client_order_id") or f"sim-{i}",
                "order_id": f.get("order_id") or f"sim-{i}",
            }
            for i, f in enumerate(services.runs.fills(run["id"]))
        ]
        # 订单层与权益层的数据来源：信号表与净值表（都来自同一次 run，不是推算出来的）
        # 注意：目标仓位本身不含方向（方向由"当前持仓 → 目标"的差额决定），
        # 所以这里不填 side，方向差异留给成交层去比。
        sim_orders = [
            {
                "order_key": s.get("signal_id") or f"sig-{i}",
                "ts": int(s.get("decision_ts") or 0),
                "security": s.get("instrument_id") or "",
                "status": s.get("status") or "accepted",
                "reason": s.get("reason") or "",
                "target_kind": (s.get("target") or {}).get("kind") or "",
                "target_value": (s.get("target") or {}).get("value") or "",
                "expires_at": int(s.get("expires_at") or 0),
            }
            for i, s in enumerate(services.runs.signal_rows(run["id"]))
        ]
        sim_equity = [{"ts": p["ts"], "value": p["nav"]} for p in services.runs.nav(run["id"])]
    elif payload.simulated_fills:
        simulated = payload.simulated_fills
    else:
        raise HTTPException(400, "请提供 slug（先跑一次回测得到模拟成交）或 simulated_fills")
    if not live_fills:
        raise HTTPException(400, "未接入真实成交：请提供实盘成交 CSV。没有真实数据时不生成对照结果")
    body = reconcile(
        simulated,
        live_fills,
        simulated_orders=sim_orders,
        simulated_equity=sim_equity,
    ).to_dict()
    body["demo"] = False
    return body


# ---------------------------------------------------------------------------
# 交易票据（模拟盘下单联调）
# ---------------------------------------------------------------------------
@router.post("/order")
def submit_order(payload: OrderTicketIn) -> dict:
    """向交易所模拟盘提交一张订单，用于接口联调。

    红线：
    - 只允许模拟盘。请求里 `simulated=false` 直接拒绝；**适配器本身配置成实盘时也拒绝**——
      只看请求标志是不够的，真正的落点由适配器的 `simulated` 决定。
    - 必须带 client_order_id，否则无法与成交逐笔对账。
    """
    services = get_services()
    if not payload.simulated:
        raise HTTPException(
            400,
            "平台默认不启用实盘下单。如需联调请使用交易所模拟盘（simulated=true）。",
        )
    adapter = services.market.adapters.get(payload.exchange)
    if adapter is None:
        raise HTTPException(404, f"未配置交易所适配器：{payload.exchange}")
    # 适配器指向真实环境时一律拒绝：请求标志说了不算，落点由适配器决定
    if not getattr(adapter, "simulated", False):
        raise HTTPException(
            400,
            "该交易所适配器当前配置为实盘（simulated=false）。平台本期不提供真实下单路径，"
            "请在配置里把 exchange.simulated 设为 true，或改用交易所模拟盘。",
        )

    intent = OrderIntent(
        run_id=payload.run_id,
        signal_id=payload.signal_id,
        symbol=payload.symbol,
        side=payload.side,
        qty=str(payload.qty),
        order_type=payload.order_type,
        client_order_id=payload.client_order_id,
        meta={"price": payload.price} if payload.price else {},
    )
    try:
        result = adapter.place_order(intent)
    except Exception as exc:
        raise HTTPException(400, f"{type(exc).__name__}: {exc}") from exc
    return {"order": result, "submitted_at": int(time.time() * 1000)}


@router.get("/exchanges/capabilities")
def exchange_capabilities() -> dict:
    """两个交易所的能力矩阵。不支持的功能显式列出，不做近似替代。"""
    services = get_services()
    out: dict[str, Any] = {}
    for name, adapter in services.market.adapters.items():
        try:
            out[name] = [c.to_dict() for c in adapter.capabilities()]
        except Exception as exc:
            out[name] = {"error": str(exc)}
    return {
        "exchanges": out,
        "note": "不支持的功能显式标注 supported=false；平台不做近似语义替代。",
    }
