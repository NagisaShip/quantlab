"""平台工具目录：把平台功能变成「问 AI」可以调用的工具，统一管理说明与提示词注入。

每个工具写清楚：做什么、参数、什么时候可用、风险级别。
- read：只读查询，直接执行；
- run：会新建一条可撤销的记录（回测、报告、演练），直接执行；
- confirm：有后果的操作（向交易所模拟盘下单的实验、一键停止、晋级、改成本假设）——模型调用时**不执行**，
  只生成一张待确认卡片，用户在面板里点「确认」后才真正执行。实盘下单不在目录里，也不可能被调用。

工具按场景分组（策略、研究数据、模拟交易、上线验证），按当前页面和用户在「更多」菜单里勾选的场景按需加载：
只把相关工具的定义和一段统一生成的说明放进对话，避免把几十个工具一股脑塞给模型。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .calculator import calculate

ACTIONS = "ai_actions"
SCENES: dict[str, dict[str, str]] = {
    "strategy": {"label": "策略与回测", "hint": "查策略、说明书、回测成绩、交易分解与保护规则；跑一次回测"},
    "data": {"label": "研究数据", "hint": "信息源数据集、事件流（公告、新闻、宏观）"},
    "trading": {"label": "模拟交易", "hint": "纸面模拟与交易所模拟盘：查看、启动、停止"},
    "validation": {"label": "上线验证", "hint": "晋级关卡、真实度报告、压力演练、值守员"},
    "checkup": {"label": "策略体检与调参", "hint": "未来函数、预热、成本翻倍、参数平台、红队意见；智能调参"},
    "library": {
        "label": "研究库与台账",
        "hint": "因子库、模型库、组合与执行方案、试验台账、实盘对照；删除记录（需审批）",
    },
    "collab": {"label": "协作与广场", "hint": "发帖到策略广场、在群组里发消息、查看与更新指派给我的任务"},
}
# 页面 → 默认加载的场景
PAGE_SCENES: dict[str, list[str]] = {
    "strategies": ["strategy"],
    "editor": ["strategy"],
    "backtests": ["strategy"],
    "backtest": ["strategy"],
    "strategy": ["strategy"],
    "optimize": ["strategy", "checkup"],
    "data": ["data"],
    "factors": ["data", "library"],
    "modellib": ["library"],
    "portfolio": ["library"],
    "execution": ["library"],
    "discipline": ["library"],
    "paper": ["trading"],
    "demo": ["trading"],
    "live": ["trading", "library"],
    "validation": ["validation", "trading"],
    "checkup": ["checkup", "strategy"],
    "home": ["strategy", "trading"],
    "square": ["collab", "strategy"],
    "post": ["collab", "strategy"],
    "groups": ["collab", "strategy"],
}
RISK_LABEL = {"read": "只读", "run": "新建记录", "confirm": "需要你确认"}

#: 审批类别：研究室的权限模式按类别决定哪些操作先弹审批
APPROVAL_CATEGORIES: dict[str, dict[str, str]] = {
    "delete": {"label": "删除记录", "hint": "删除策略、回测、因子等记录（不可恢复）"},
    "trading": {"label": "模拟交易与晋级", "hint": "启动 / 停止纸面模拟与交易所模拟盘、晋级、微实盘急停"},
    "config": {"label": "改平台配置", "hint": "校准回测成本等会影响以后所有回测的设置"},
    "save": {"label": "保存到库", "hint": "把成果保存为平台策略、入因子库 / 模型库 / 方案库"},
    "edit": {"label": "改代码与目标", "hint": "提交新版本代码 / 规格、修改目标与标的"},
    "run": {"label": "运行计算", "hint": "回测、评估、训练、团队评审、自动迭代"},
}
#: 「自动」权限下规则要求必须先审批的类别
RULE_REQUIRED = ("delete", "trading", "config")


@dataclass
class PlatformTool:
    name: str
    title: str
    scene: str
    description: str
    when: str
    risk: str  # read | run | confirm
    params: dict[str, Any]
    handler: Callable[..., Any]
    summary: Callable[[dict[str, Any]], str] | None = None  # confirm 类：卡片上的一句话
    required: list[str] = field(default_factory=list)
    category: str = ""  # 审批类别；空 = 按 risk 推断（confirm → trading，run → run，read → 不审批）

    @property
    def approval(self) -> str:
        if self.category:
            return self.category
        return {"confirm": "trading", "run": "run"}.get(self.risk, "")

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self.params, "required": self.required}

    def view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "scene": self.scene,
            "description": self.description,
            "when": self.when,
            "risk": self.risk,
            "risk_label": RISK_LABEL[self.risk],
            "approval": self.approval,
            "params": list(self.params),
        }


# ---------------------------------------------------------------------------
# 工具实现：直接复用各页面的接口函数，口径和界面一致
# ---------------------------------------------------------------------------
def _svc() -> Any:
    from ..api.deps import get_services

    return get_services()


def _repo() -> Any:
    from ..strategies.repo import StrategyRepository

    return StrategyRepository(_svc().store.root / "strategies")


def _call(fn: Callable[[], Any]) -> Any:
    from fastapi import HTTPException

    from ..platform.errors import AppError

    try:
        return fn()
    except HTTPException as exc:
        return {"error": str(exc.detail)}
    except AppError as exc:
        return {"error": exc.message if hasattr(exc, "message") else str(exc)}
    except ValueError as exc:
        return {"error": str(exc)}


def _metrics(m: dict[str, Any] | None) -> dict[str, Any]:
    keys = ("total_return", "annualized_return", "sharpe", "max_drawdown", "trades", "win_rate")
    return {k: (m or {}).get(k) for k in keys}


def t_list_strategies(query: str = "") -> Any:
    rows = []
    for r in _repo().list():
        m = r.meta
        if query and query.lower() not in f"{m.name} {m.slug} {m.notes}".lower():
            continue
        rows.append(
            {
                "slug": m.slug,
                "name": m.name,
                "kind": m.kind,
                "status": m.status,
                "securities": m.securities,
                "timeframe": m.timeframe,
                "approved": m.human_approved,
            }
        )
    return rows[:40]


def t_get_strategy(slug: str) -> Any:
    rec = _repo().get(slug)
    if rec is None:
        return {"error": "策略不存在"}
    return {
        "slug": slug,
        "name": rec.meta.name,
        "status": rec.meta.status,
        "securities": rec.meta.securities,
        "timeframe": rec.meta.timeframe,
        "spec": (_repo().read_spec(slug) or "（没有说明书）")[:4000],
        "source": rec.source[:6000],
    }


def t_list_backtests(slug: str = "") -> Any:
    from ..store import Collections

    rows = [r for r in _svc().store.list(Collections.BACKTESTS) if not slug or r.get("slug") == slug]
    rows.sort(key=lambda r: -(r.get("created_at") or 0))
    return [
        {
            "id": r["id"],
            "strategy": r.get("strategy_name"),
            "slug": r.get("slug"),
            "timeframe": r.get("timeframe"),
            "securities": r.get("securities"),
            "status": r.get("status"),
            "metrics": _metrics(r.get("metrics")),
        }
        for r in rows[:20]
    ]


def t_run_backtest(slug: str, symbol: str = "", timeframe: str = "", bars_limit: int = 1500) -> Any:
    from ..api.routes_studio import studio_backtest
    from ..api.schemas import StudioBacktestIn

    rec = _repo().get(slug)
    if rec is None:
        return {"error": "策略不存在"}
    secs = [symbol] if symbol else (rec.meta.securities or ["BTC/USDT"])
    body = _call(
        lambda: studio_backtest(
            StudioBacktestIn(
                slug=slug,
                securities=secs,
                symbol=secs[0],
                timeframe=timeframe or rec.meta.timeframe or "1h",
                bars_limit=max(102, min(int(bars_limit or 1500), 5000)),
            )
        )
    )
    if not isinstance(body, dict) or body.get("error"):
        return body if isinstance(body, dict) else {"error": "回测还在排队，稍后在「回测」页查看"}
    return {
        "run_id": body.get("run_id"),
        "metrics": _metrics(body.get("metrics")),
        "warnings": (body.get("warnings") or [])[:5],
        "errors": (body.get("errors") or [])[:3],
        "data_sources": sorted({d.get("source") for d in body.get("datasets") or []}),
    }


def t_list_datasets(field_name: str = "") -> Any:
    from ..research.datahub import DataHub

    rows = DataHub(_svc().store).datasets()
    if field_name:
        rows = [r for r in rows if r.get("field") == field_name]
    return [
        {k: r.get(k) for k in ("key", "field", "symbol", "source", "rows", "start", "end", "lag_ms", "status")}
        for r in rows[:60]
    ]


def t_list_events(kind: str = "", coin: str = "", limit: int = 30) -> Any:
    from ..research.datahub import DataHub
    from ..research.events import EventHub

    s = _svc()
    rows = EventHub(s.store, DataHub(s.store)).events(
        kind=kind or None, coin=coin or None, limit=max(1, min(int(limit or 30), 100))
    )
    return [
        {
            k: r.get(k)
            for k in (
                "ts",
                "available_at",
                "kind",
                "type",
                "title",
                "coins",
                "direction",
                "strength",
                "source",
                "hindsight_risk",
            )
        }
        | {"tagger": (r.get("tagger") or {}).get("kind")}
        for r in rows
    ]


def _sessions() -> list[dict[str, Any]]:
    from ..store import Collections

    return _svc().store.list(Collections.PAPER_SESSIONS)


def t_list_sessions(status: str = "") -> Any:
    out = []
    for r in _sessions():
        if status and r.get("status") != status:
            continue
        pts = r.get("equity_points") or []
        ret = pts[-1]["v"] / pts[0]["v"] - 1 if len(pts) > 1 and pts[0].get("v") else None
        out.append(
            {
                "id": r["id"],
                "strategy": r.get("strategy_name"),
                "slug": r.get("strategy_id"),
                "execution": r.get("execution") or "paper",
                "status": r.get("status"),
                "source": r.get("source"),
                "exchange": r.get("exchange"),
                "securities": r.get("securities"),
                "fills": len(r.get("fills") or []),
                "return": ret,
                "days": round(((r.get("last_update") or 0) - (r.get("started_at") or 0)) / 86_400_000, 2),
                "halted": bool((r.get("demo") or {}).get("halted")),
            }
        )
    return out[:30]


def t_get_session(session_id: str) -> Any:
    from ..store import Collections

    r = _svc().store.get(Collections.PAPER_SESSIONS, session_id)
    if r is None:
        return {"error": "实验不存在"}
    demo = r.get("demo") or {}
    return {
        "id": r["id"],
        "strategy": r.get("strategy_name"),
        "status": r.get("status"),
        "execution": r.get("execution"),
        "portfolio": r.get("portfolio"),
        "recent_fills": (r.get("fills") or [])[-10:],
        "recent_events": (r.get("events") or [])[-10:],
        "demo": {k: demo.get(k) for k in ("halted", "halt_reason", "slippage", "reconcile")} if demo else None,
    }


def t_start_session(
    slug: str,
    execution: str = "paper",
    demo_connection_id: str = "",
    exchange: str = "binance",
    initial_cash: float = 10_000,
    symbol: str = "",
) -> Any:
    from ..api.routes_studio import start_paper
    from ..api.schemas import PaperStartIn

    body = _call(
        lambda: start_paper(
            PaperStartIn(
                slug=slug,
                execution=execution,
                demo_connection_id=demo_connection_id or None,
                exchange=exchange,
                initial_cash=float(initial_cash or 10_000),
                securities=[symbol] if symbol else [],
                source="exchange",
            )
        )
    )
    if isinstance(body, dict) and body.get("session"):
        s = body["session"]
        return {"ok": True, "session_id": s["id"], "status": s.get("status"), "execution": s.get("execution")}
    if isinstance(body, dict) and "人工批准" in str(body.get("error") or ""):
        # 被红线拦下：把晋级关卡里没通过的项一起交回去，研究者和 AI 都能看到卡在哪一步
        grad = t_graduation(slug)
        failing = []
        if isinstance(grad, dict):
            failing = [
                {k: c.get(k) for k in ("label", "value", "need")}
                for c in grad.get("checks") or []
                if isinstance(c, dict) and c.get("ok") is not True
            ]
        return {
            "ok": False,
            "error": body["error"],
            "blocked_at": "策略未人工批准（approved=false）",
            "graduation_passed": grad.get("passed") if isinstance(grad, dict) else None,
            "graduation_failing": failing,
            "hint": "先让晋级关卡全部通过（graduation），再由研究者在策略页批准，之后才能启动实时模拟",
        }
    return body


def t_stop_session(session_id: str, close_positions: bool = True) -> Any:
    from ..api.routes_studio import liquidate_paper, stop_paper

    body = _call(lambda: (liquidate_paper if close_positions else stop_paper)(session_id))
    if isinstance(body, dict) and body.get("session"):
        return {"ok": True, "status": body["session"].get("status")}
    return body


def t_graduation(slug: str = "") -> Any:
    from ..api import routes_validation as V

    return _call(lambda: V.graduation_detail(slug) if slug else V.graduation_board()["board"])


def t_promote(slug: str, note: str = "") -> Any:
    from ..api import routes_validation as V

    return _call(lambda: V.graduation_promote(slug, V.PromoteIn(confirm=True, note=note or "经「问 AI」确认")))


def t_fidelity(session_ids: list[str]) -> Any:
    from ..api import routes_validation as V

    out = _call(lambda: V.make_fidelity(V.FidelityIn(session_ids=list(session_ids)[:3])))
    if isinstance(out, dict) and out.get("report"):
        rep = out["report"]
        return {
            "report_id": rep["id"],
            "parts": [
                {
                    "execution": p["execution"],
                    "total": p["total"],
                    "total_bps": p["total_bps"],
                    "components": p["components"],
                    "matched": p["matched"],
                }
                for p in rep["parts"]
            ],
            "calibration": rep["calibration"],
            "notes": rep["notes"],
        }
    return out


def t_apply_calibration(report_id: str) -> Any:
    from ..api import routes_validation as V

    return _call(lambda: V.apply_calibration(report_id, V.CalibIn(confirm=True)))


def t_stress(slug: str, scenarios: list[str] | None = None) -> Any:
    from ..api import routes_validation as V

    out = _call(lambda: V.stress_start(V.StressIn(slug=slug, scenarios=scenarios or None)))
    if isinstance(out, dict) and out.get("run"):
        return {"ok": True, "run_id": out["run"]["id"], "note": "演练在后台进行，到「上线验证 → 压力演练」看结果"}
    return out


def t_stress_result(run_id: str = "") -> Any:
    from ..api import routes_validation as V

    if not run_id:
        return V.stress_list()["runs"][:10]
    run = _call(lambda: V.stress_get(run_id))
    if isinstance(run, dict) and run.get("run"):
        r = run["run"]
        rep = r.get("report") or {}
        return {
            "status": r["status"],
            "summary": rep.get("summary"),
            "scenarios": [
                {
                    k: s.get(k)
                    for k in (
                        "name",
                        "data_source",
                        "return_pct",
                        "max_drawdown",
                        "liquidations",
                        "rejections",
                        "orders",
                        "fills",
                        "verdict",
                    )
                }
                for s in rep.get("scenarios") or []
            ],
        }
    return run


def t_watch(session_id: str) -> Any:
    from ..api import routes_validation as V

    try:
        rv = V.watch_session(session_id, use_llm=False)
    except ValueError as exc:
        return {"error": str(exc)}
    return {"rules": rv["rules"], "flags": rv["flags"][-15:], "recent_trades": rv["trades"][-10:]}


def t_brief() -> Any:
    from ..research import brief as BR

    b = BR.latest(_svc().store)
    return {k: b.get(k) for k in ("title", "headline", "focus", "sections")} if b else {"error": "还没有晨报"}


def t_checkup(slug: str, bars_limit: int = 1500) -> Any:
    from ..api import routes_validation as V

    out = _call(
        lambda: V.checkup_start(V.CheckupIn(slug=slug, bars_limit=max(300, min(int(bars_limit or 1500), 5000))))
    )
    if isinstance(out, dict) and out.get("run"):
        return {
            "ok": True,
            "run_id": out["run"]["id"],
            "note": "体检在后台进行，几十秒到几分钟；用 checkup_result 查结果",
        }
    return out


def t_checkup_result(run_id: str = "", slug: str = "") -> Any:
    from ..api import routes_validation as V

    if not run_id:
        rows = V.checkup_list(slug or None)["runs"][:10]
        return [
            {
                k: r.get(k)
                for k in ("id", "strategy_id", "status", "overall", "sample", "progress", "created_at", "items")
            }
            for r in rows
        ]
    got = _call(lambda: V.checkup_get(run_id))
    if not isinstance(got, dict) or not got.get("run"):
        return got
    r = got["run"]
    rep, red = r.get("report") or {}, r.get("redteam") or {}
    return {
        "status": r["status"],
        "overall": r.get("overall"),
        "sample": r.get("sample"),
        "sample_note": "体检成绩只对这段样本成立；与 graduation 引用的回测（evidence.backtest_sample）区间不同时不能互相印证",
        "metrics": _metrics(rep.get("metrics")),
        "stale": got.get("stale"),
        "error": r.get("error"),
        "items": [{k: i.get(k) for k in ("title", "verdict", "summary")} for i in rep.get("items") or []],
        "redteam": {
            "mode": red.get("mode"),
            "points": (red.get("points") or [])[:8],
            "content": (red.get("content") or "")[:2000],
        },
    }


def t_start_tuning(
    slug: str,
    n_trials: int = 50,
    objective: str = "sharpe",
    multi_objective: bool = False,
    space: dict[str, Any] | None = None,
) -> Any:
    from ..api import routes_research as R
    from ..api.schemas import OptimizeIn

    rec = _repo().get(slug)
    if rec is None:
        return {"error": "策略不存在"}
    body = _call(
        lambda: R.start_optimize(
            OptimizeIn(
                slug=slug,
                method="bayes",
                n_trials=max(5, min(int(n_trials or 50), 150)),
                objective=objective,
                multi_objective=bool(multi_objective),
                space=space or {},
                securities=rec.meta.securities or [],
                timeframe=rec.meta.timeframe or "1h",
            )
        )
    )
    if isinstance(body, dict) and body.get("run_id"):
        return {
            "ok": True,
            "run_id": body["run_id"],
            "trials": body.get("combinations"),
            "space": body.get("space"),
            "space_source": body.get("space_source"),
            "note": "调参在后台进行；用 tuning_result(opt_id=run_id) 查进度与结果（失败会返回原因），每一组试验都会记进试验台账",
        }
    return body


def _tuning_runs(slug: str = "") -> list[dict[str, Any]]:
    """还没产出优化记录的调参任务（排队 / 运行中 / 失败）：不列出来的话，失败的调参会「静默消失」。"""
    from ..api.deps import get_services
    from ..platform import context

    try:
        runs = get_services().runs.list(context.current_workspace(), kind="optimize") or []
    except Exception:
        return []
    out = []
    for r in runs[:20]:
        man = r.get("manifest") or {}
        if slug and man.get("slug") not in (None, slug):
            continue
        if r.get("state") == "COMPLETED" and (r.get("summary") or {}).get("id"):
            continue  # 已完成的在优化记录里
        out.append(_run_view(r))
    return out


def _run_view(r: dict[str, Any]) -> dict[str, Any]:
    state = r.get("state")
    view = {
        "run_id": r.get("id"),
        "state": state,
        "progress": r.get("progress"),
        "title": r.get("title"),
    }
    if state == "FAILED":
        view["error"] = r.get("error") or "任务失败（没有错误信息）"
        view["hint"] = "调参没有产出结果：按 error 修正后重新 start_tuning，不要反复查询"
    elif state in ("QUEUED", "RUNNING", "PAUSED", "RECOVERING"):
        view["hint"] = "仍在进行，稍后再查；不要重复发起"
    elif state == "CANCELED":
        view["hint"] = "任务已取消"
    return view


def t_tuning_result(opt_id: str = "", slug: str = "") -> Any:
    from ..api import routes_research as R
    from ..api.deps import get_services
    from ..platform import context

    if opt_id.startswith("run-") or (opt_id and not opt_id.startswith("opt-")):
        # 传的是 start_tuning 返回的 run_id：任务完成就换成优化记录，否则如实返回任务状态 / 失败原因
        run = get_services().runs.get(opt_id, workspace_id=context.current_workspace())
        if run is None:
            return {"error": f"找不到调参任务 {opt_id}"}
        done_id = (run.get("summary") or {}).get("id")
        if run.get("state") != "COMPLETED" or not done_id:
            return _run_view(run)
        opt_id = done_id
    if not opt_id:
        rows = sorted(R.list_optimizations(slug or None)["optimizations"], key=lambda r: -(r.get("created_at") or 0))
        done = [
            {
                k: r.get(k)
                for k in ("id", "run_id", "slug", "method", "objective", "combinations", "best_params", "created_at")
            }
            for r in rows[:10]
        ]
        pending = _tuning_runs(slug)
        if not done and not pending:
            return {"results": [], "note": "还没有任何调参记录：先用 start_tuning 发起"}
        return {"results": done, "unfinished": pending}
    body = _call(lambda: R.get_optimization(opt_id))
    if not isinstance(body, dict) or body.get("error"):
        return body
    best = body.get("best") or {}
    return {
        "method": body.get("method"),
        "trials": body.get("n_trials") or len(body.get("rows") or []),
        "best_params": best.get("params"),
        "in_sample": best.get("in_sample"),
        "out_sample": best.get("out_sample"),
        "warnings": (body.get("diagnostics") or {}).get("warnings"),
    }


def t_trade_breakdown(backtest_id: str, by: str = "") -> Any:
    """一次回测按入场标签、出场原因、标的、星期、小时的分解。"""
    from ..store import Collections

    rec = _svc().store.get(Collections.BACKTESTS, backtest_id)
    if rec is None:
        return {"error": "回测记录不存在"}
    # 索引行里的 metrics 是摘要（不含分解），完整指标在回测详情 blob 里
    bd = (rec.get("metrics") or {}).get("breakdown")
    if bd is None:
        blob = _svc().store.get_blob("backtests", backtest_id) or {}
        bd = (blob.get("metrics") or {}).get("breakdown")
    if bd is None:
        return {"error": "这次回测的详情里没有交易分解：请用当前平台重新回测一次后再查（返回的新 backtest_id）"}
    if not any(bd.values()):
        return {"backtest_id": backtest_id, "note": "这次回测没有完成的往返交易（开仓后平仓），没有可分解的出场原因"}
    if by:
        if by not in bd:
            return {"error": f"by 只能是 {', '.join(bd)}"}
        return {"backtest_id": backtest_id, "by": by, "rows": bd[by]}
    return {"backtest_id": backtest_id, **{k: v[:12] for k, v in bd.items()}}


def t_list_trades(backtest_id: str, limit: int = 50, offset: int = 0) -> Any:
    """一次回测的完整交易（round_trips）与逐笔成交（fills）明细。"""
    from ..store import Collections

    rec = _svc().store.get(Collections.BACKTESTS, backtest_id)
    if rec is None:
        return {"error": "回测记录不存在"}
    blob = _svc().store.get_blob("backtests", backtest_id) or {}
    trips = list(blob.get("round_trips") or [])
    fills = list(blob.get("fills") or [])
    if not trips and not fills:
        return {"backtest_id": backtest_id, "note": "这次回测没有成交明细；可能是零交易或旧记录未保存明细"}
    n = max(1, min(int(limit or 50), 200))
    off = max(0, int(offset or 0))
    return {
        "backtest_id": backtest_id,
        "totals": {"round_trips": len(trips), "fills": len(fills)},
        "offset": off,
        "limit": n,
        "has_more": off + n < max(len(trips), len(fills)),
        "round_trips": trips[off : off + n],
        "fills": fills[off : off + n],
        "note": "round_trips 是开仓到平仓的完整交易；fills 是逐笔成交，fees/realized 已是该回测的实际口径",
    }


def t_guards_report(backtest_id: str) -> Any:
    """一次回测里保护规则的锁定记录、被拦开仓次数和标的池变化。"""
    from ..store import Collections

    rec = _svc().store.get(Collections.BACKTESTS, backtest_id)
    if rec is None:
        return {"error": "回测记录不存在"}
    g = (rec.get("metrics") or {}).get("guards") or (
        (_svc().store.get_blob("backtests", backtest_id) or {}).get("metrics") or {}
    ).get("guards")
    if not g:
        return {"backtest_id": backtest_id, "note": "这个策略没有设置 PROTECTIONS 或 PAIRLIST"}
    return {
        "backtest_id": backtest_id,
        "protections": g.get("protections"),
        "blocked": g.get("blocked"),
        "locks": (g.get("locks") or [])[:40],
        "universe": g.get("universe"),
        "universe_changes": [h for h in g.get("universe_history") or [] if h.get("dropped")][:30],
        "delisted": g.get("delisted"),
    }


def t_node_health() -> Any:
    """运行节点：worker 心跳、失联实验、交易所时钟偏差、限频状态。"""
    from ..platform import watchdog

    h = watchdog.health(_svc())
    return {k: h[k] for k in ("workers_alive", "stale_runs", "clock", "rate_limits")}


def t_freqtrade_check(source: str) -> Any:
    """检查一个 Freqtrade 策略能不能导入：用到的写法、会被忽略的部分、跑不了的原因、试跑成交数。"""
    from ..strategies import freqtrade_import as FI

    if not FI.is_freqtrade(source):
        return {"error": "没找到继承 IStrategy 的策略类"}
    r = FI.convert(source).to_dict()
    return {k: r[k] for k in ("ok", "class_name", "timeframe", "errors", "warnings", "features", "params", "smoke")}


def t_decay_report() -> Any:
    """运行中实验的衰减判定、模型重训情况、当前市场状态和建议权重。"""
    from ..api.routes_lifecycle import overview

    o = overview()
    return {
        "decay": [
            {k: r.get(k) for k in ("id", "strategy_name", "label", "days", "reasons", "stage", "suggest_demote_to")}
            for r in o["decay"]
        ][:20],
        "models": [{k: m.get(k) for k in ("name", "plan", "due", "last")} for m in o["models"]][:20],
        "regime": o["regime"].get("label"),
        "weights": o["weights"][:10],
    }


def t_microlive_status() -> Any:
    """微实盘：能不能启动（卡在哪）、急停状态、各毕业策略的资金上限与升档条件、运行中的实验。"""
    from ..api.routes_microlive import overview

    o = overview()
    st = o["status"]
    return {
        "ready": st["ready"],
        "blockers": st["blockers"],
        "killed": st["killed"],
        "tiers": st["tiers"],
        "strategies": [
            {k: r.get(k) for k in ("name", "cap", "next_cap", "can_raise", "checks")} for r in o["strategies"]
        ],
        "sessions": [
            {k: x.get(k) for k in ("id", "strategy_name", "run_state", "initial_cash")} for x in o["sessions"]
        ],
    }


def t_microlive_kill() -> Any:
    from ..api.routes_microlive import kill

    out = _call(kill)
    return {k: out.get(k) for k in ("stopped", "failed")} if isinstance(out, dict) else out


def _lib_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{k: r.get(k) for k in keys if r.get(k) is not None} for r in rows[:40]]


def t_list_factors(status: str = "", query: str = "") -> Any:
    from ..research.factor_lib import FactorLibrary

    lib = FactorLibrary(_svc().store)
    rows = lib.search(query) if query else lib.list(status or None)
    return _lib_rows(rows, ("id", "name", "expr", "status", "category", "summary", "updated_at"))


def t_list_models(status: str = "") -> Any:
    from ..research.model_lib import ModelLibrary

    lib = ModelLibrary(_svc().store)
    return {
        "models": _lib_rows(lib.list(status or None), ("id", "name", "status", "learner", "summary", "updated_at")),
        "recent_runs": _lib_rows(lib.runs(12), ("id", "name", "learner", "summary", "created_at")),
    }


def t_list_portfolios(status: str = "") -> Any:
    from ..research.portfolio_lib import PortfolioLibrary

    lib = PortfolioLibrary(_svc().store)
    return {
        "schemes": _lib_rows(lib.list(status or None), ("id", "name", "status", "method", "summary", "updated_at")),
        "recent_runs": _lib_rows(lib.runs(12), ("id", "name", "summary", "created_at")),
    }


def t_list_executions(status: str = "") -> Any:
    from ..research.execution_lib import ExecutionLibrary

    lib = ExecutionLibrary(_svc().store)
    return {
        "schemes": _lib_rows(lib.list(status or None), ("id", "name", "status", "algo", "summary", "updated_at")),
        "recent_runs": _lib_rows(lib.runs(12), ("id", "name", "summary", "created_at")),
    }


def t_trial_ledger(domain: str = "") -> Any:
    from ..api.routes_rd import ledger

    out = _call(lambda: ledger(domain or None))
    if isinstance(out, dict) and "families" in out:
        out = {**out, "families": out["families"][:30], "recent": (out.get("recent") or [])[:20]}
    return out


def t_fill_comparisons() -> Any:
    from ..api.routes_live import list_comparisons

    out = _call(list_comparisons)
    if isinstance(out, dict) and isinstance(out.get("items"), list):
        out = {"items": out["items"][:20]}
    return out


def t_delete_strategy(slug: str) -> Any:
    from ..api.routes_studio import delete_strategy

    return _call(lambda: delete_strategy(slug))


def t_delete_backtests(ids: list[str]) -> Any:
    from ..api.routes_research import BatchDeleteIn, batch_delete_backtests

    return _call(lambda: batch_delete_backtests(BatchDeleteIn(ids=[str(i) for i in ids or []][:200])))


def t_delete_factor(fid: str) -> Any:
    from ..research.factor_lib import FactorLibrary

    return {"ok": FactorLibrary(_svc().store).delete(fid)}


# ---------------------------------------------------------------------------
# 协作：Agent 发帖、群消息、任务
# ---------------------------------------------------------------------------
AGENT_NAME = "QuantBot"


def _agent_source(conversation: str = "") -> dict[str, Any]:
    from ..platform import context

    ctx = context.current()
    return {
        "kind": "agent",
        "label": AGENT_NAME,
        "on_behalf_of": ctx.user_name or "",
        "user_id": ctx.user_id or "",
        "conversation": conversation,
    }


def _agent_name() -> str:
    from ..platform import context

    return f"{AGENT_NAME}（代 {context.current().user_name}）"


def t_community_post(
    title: str,
    body: str,
    type: str = "article",
    tags: list[str] | None = None,
    strategy_slug: str = "",
    backtest_id: str = "",
    refs: list[dict[str, Any]] | None = None,
) -> Any:
    from ..api.routes_community import publish
    from ..api.schemas import PostIn

    def go() -> Any:
        payload = PostIn(
            type=type if type in ("article", "question", "strategy") else "article",
            title=title,
            body=body,
            tags=list(tags or [])[:8],
            strategy_slug=strategy_slug or None,
            backtest_id=backtest_id or None,
            refs=list(refs or []),
        )
        post = publish(payload, _agent_source())
        return {"post_id": post["id"], "title": post["title"], "link": f"#/post/{post['id']}"}

    return _call(go)


def t_list_groups() -> Any:
    from ..platform import context

    ctx = context.current()
    groups = _svc().collab.list_groups(context.current_workspace(), ctx.user_id or "")
    return [
        {
            "id": g["id"],
            "name": g["name"],
            "joined": bool(g["my_role"]),
            "members": g["member_count"],
            "open_tasks": g["open_tasks"],
            "unread": g["unread"],
        }
        for g in groups
    ]


def t_group_message(group_id: str, body: str, refs: list[dict[str, Any]] | None = None) -> Any:
    from ..platform import context

    def go() -> Any:
        from ..api.routes_collab import _resolve_refs

        ctx = context.current()
        c = _svc().collab
        c.require_group_member(group_id, ctx.user_id or "", ws=context.current_workspace())
        msg = c.post_message(
            group_id,
            author_id=ctx.user_id or "",
            author_name=_agent_name(),
            body=body,
            refs=_resolve_refs(list(refs or [])),
            author_kind="agent",
        )
        return {"message_id": msg["id"], "group_id": group_id}

    return _call(go)


def t_my_tasks(include_closed: bool = False) -> Any:
    from ..platform import context

    ctx = context.current()
    rows = _svc().collab.tasks(
        assignee_id=ctx.user_id or "", ws=context.current_workspace(), include_closed=include_closed
    )
    keys = ("id", "title", "status", "priority", "due_at", "group_id", "creator_name", "description", "overdue")
    return [{k: t[k] for k in keys} for t in rows]


def t_update_task(task_id: str, status: str = "", note: str = "") -> Any:
    from ..platform import context

    def go() -> Any:
        ctx = context.current()
        c = _svc().collab
        task = c.task(task_id)
        mod = c.group_role(task["group_id"], ctx.user_id or "") in ("owner", "admin")
        out = c.update_task(
            task_id, {"status": status} if status else {}, by=ctx.user_id or "", by_name=_agent_name(), moderator=mod
        )
        if note:
            c.post_message(
                task["group_id"],
                author_id=ctx.user_id or "",
                author_name=_agent_name(),
                body=note,
                refs=[{"type": "task", "id": task_id, "title": task["title"]}],
                author_kind="agent",
            )
        return {"id": out["id"], "status": out["status"]}

    return _call(go)


def t_workspace_capabilities() -> dict[str, Any]:
    """Return the effective workspace switches and whether each feature is real."""
    from ..platform.features import current, registry

    try:
        enabled = current()
    except Exception:
        enabled = {}
    return {
        "enabled": enabled,
        "registry": registry(),
        "hard_scope": {
            "perp_contracts": ["linear", "inverse", "delivery"],
            "perp_exchanges": ["binance", "okx", "bybit"],
            "margin_modes": ["isolated", "cross"],
            "position_modes": ["one-way", "hedge"],
            "cross_margin": True,
            "hedge_mode": True,
            "inverse": True,
            "delivery": True,
            "real_money_perp": "linear USDT-M on Binance/OKX, separate server flag and approval",
        },
        "rule": "回答必须同时说明合约类型、保证金模式、持仓模式、结算币种和数据来源；研究近似不能描述成交易所生产规则。",
    }


def capability_note() -> str:
    """A short, dynamic boundary block appended to every research prompt."""
    from ..platform.features import FEATURES, current

    try:
        enabled = current()
    except Exception:
        enabled = {k: bool(v["default"]) for k, v in FEATURES.items()}
    disabled = [str(v["label"]) for k, v in FEATURES.items() if not enabled.get(k)]
    base = (
        "【市场能力边界】回测账本 v2 支持线性 USDT-M、币本位反向与交割合约，支持逐仓/全仓、单向/对冲；"
        "反向与交割的交易所规则仍是研究近似，必须用账单核验；真实资金永续只支持 Binance/OKX 线性 USDT-M，"
        "且必须通过独立服务器开关、管理员确认码、期货 Key 自检、止损和自动急停。"
        "引用回测结果时必须同时说明交易所、合约类型、保证金/持仓模式、结算币种和数据来源。"
    )
    if disabled:
        base += " 当前工作区关闭或不可用的能力：" + "、".join(disabled) + "。"
    return base


def t_contract_bill_check(
    connection_id: str, symbol: str = "BTC/USDT:PERP", start: int = 0, end: int = 0
) -> dict[str, Any]:
    """Pull demo-account bills and compare them with the contract assumptions."""
    from ..exchanges.contract_verification import verify_bills
    from ..exchanges.contracts import is_derivative, spec_for

    if not is_derivative(str(symbol)):
        return {"error": "只接受衍生品标的（:PERP / :INVERSE / :DELIVERY）"}
    got = _svc().demo.bills(connection_id, symbol, int(start) or None, int(end) or None)
    conn = got.get("connection") or {}
    spec = spec_for(str(conn.get("exchange") or ""), symbol)
    report = verify_bills(exchange=str(conn.get("exchange") or ""), spec=spec, rows=got.get("rows") or [])
    return {**report, "connection": conn, "fetched_rows": len(got.get("rows") or [])}


def t_realperp_status() -> dict[str, Any]:
    """Read real-perpetual safety state without exposing credentials."""
    from ..live import real_perp as RP
    from ..platform import context

    s = _svc()
    st = RP.status(s.store, s.home)
    return {
        "status": {k: v for k, v in st.items() if k not in ("notice", "notice_en")},
        "connections": [
            {k: c.get(k) for k in ("id", "exchange", "label", "key_mask", "status", "last_test")}
            for c in RP.connections(s).list()
        ],
        "workspace": context.current_workspace(),
        "rule": "Agent 只能查看；真实永续启动必须由管理员生成确认码并输入，Agent 不能代替用户下单或解除急停。",
    }


def _S(t: str, d: str, **kw: Any) -> dict[str, Any]:
    return {"type": t, "description": d, **kw}


def build() -> dict[str, PlatformTool]:
    tools = [
        PlatformTool(
            "list_strategies",
            "列出策略",
            "strategy",
            "列出我的策略（名字、标的、周期、是否已批准）。",
            "随时",
            "read",
            {"query": _S("string", "按名字过滤，可空")},
            t_list_strategies,
        ),
        PlatformTool(
            "get_strategy",
            "读策略",
            "strategy",
            "读一个策略的说明书（SPEC.md）和源码。",
            "知道策略 slug 时",
            "read",
            {"slug": _S("string", "策略 slug")},
            t_get_strategy,
            required=["slug"],
        ),
        PlatformTool(
            "list_backtests",
            "回测记录",
            "strategy",
            "最近的回测记录与主要成绩。",
            "随时",
            "read",
            {"slug": _S("string", "只看这个策略，可空")},
            t_list_backtests,
        ),
        PlatformTool(
            "run_backtest",
            "跑回测",
            "strategy",
            "用已保存的策略跑一次回测，返回主要成绩。",
            "策略已保存",
            "run",
            {
                "slug": _S("string", "策略 slug"),
                "symbol": _S("string", "标的，如 BTC/USDT，可空"),
                "timeframe": _S("string", "周期，如 1h，可空"),
                "bars_limit": _S("integer", "K 线根数，102–5000"),
            },
            t_run_backtest,
            required=["slug"],
        ),
        PlatformTool(
            "contract_bill_check",
            "用交易所账单核验永续规则",
            "validation",
            "从已接入的 OKX / Bybit 模拟盘拉取账户账单，核验资金费结算、标记价、手续费、保证金模式、杠杆和强平字段。"
            "账单没有出现的 MMR/强平场景会明确标为 unknown。",
            "研究者要求核验合约规则，或准备扩大实盘/模拟盘规模时",
            "read",
            {
                "connection_id": _S("string", "模拟盘连接 id"),
                "symbol": _S("string", "永续标的，如 BTC/USDT:PERP"),
                "start": _S("integer", "起始毫秒时间戳，可空"),
                "end": _S("integer", "结束毫秒时间戳，可空"),
            },
            t_contract_bill_check,
            required=["connection_id"],
        ),
        PlatformTool(
            "realperp_status",
            "真实永续安全状态",
            "validation",
            "读取真实永续的独立服务器开关、工作区确认、急停、连接状态与最后期货账户自检结果；不返回密钥。"
            "Agent 不得代替用户启动或急停真实资金操作。",
            "涉及真实资金永续、扩大规模或排查启动阻断时",
            "read",
            {},
            t_realperp_status,
        ),
        PlatformTool(
            "workspace_capabilities",
            "查看功能开关与能力边界",
            "strategy",
            "读取当前工作区的现货/永续/真实资金/导入导出开关，以及每项能力的实现状态。"
            "在承诺永续、做空、杠杆或真实资金操作前必须先查。",
            "涉及市场类型、杠杆或真实资金时",
            "read",
            {},
            t_workspace_capabilities,
        ),
        PlatformTool(
            "calculator",
            "计算器",
            "strategy",
            "安全计算数学表达式：四则运算、幂、括号、sqrt/log/exp 等函数，以及 percent(18, 80) 这样的百分比换算。"
            "用于核对成本、收益、仓位、额度和复利，不执行代码。",
            "需要做算术或百分比换算时",
            "read",
            {"expression": _S("string", "数学表达式，例如 (1+0.08)^5 或 percent(18, 80)")},
            calculate,
            required=["expression"],
        ),
        PlatformTool(
            "trade_breakdown",
            "交易分解",
            "strategy",
            "一次回测的已完成交易按入场标签、出场原因（止损 / 移动止损 / 分段止盈 / 自定义…）、标的、星期、小时分组："
            "交易数、胜率、平均收益、盈亏、持仓时长。用来找哪类信号在赚钱、哪类在亏。",
            "知道回测 id 时（list_backtests 可查）",
            "read",
            {
                "backtest_id": _S("string", "回测 id"),
                "by": _S(
                    "string", "只看一个维度，可空", enum=["entry_tag", "exit_reason", "security", "weekday", "hour"]
                ),
            },
            t_trade_breakdown,
            required=["backtest_id"],
        ),
        PlatformTool(
            "list_trades",
            "逐笔交易",
            "strategy",
            "一次回测的完整交易（round_trips）与逐笔成交（fills）明细：时间、方向、数量、价格、手续费、盈亏、"
            "入场标签与出场原因。用来核对团队评审所说的每一笔交易，而不是只看汇总指标。",
            "知道回测 id 时（list_backtests 可查）",
            "read",
            {
                "backtest_id": _S("string", "回测 id"),
                "limit": _S("integer", "每次返回多少条，默认 50，最多 200"),
                "offset": _S("integer", "从第几条开始，默认 0"),
            },
            t_list_trades,
            required=["backtest_id"],
        ),
        PlatformTool(
            "guards_report",
            "保护与标的池",
            "strategy",
            "一次回测里组合保护（连续止损暂停、冷却、回撤暂停、低收益标的暂停）的每次锁定、被拦下的开仓次数，"
            "以及动态标的池每次重选的结果和因下架公告移出的币。用来判断保护规则是在省钱还是在错过行情。",
            "策略设置了 PROTECTIONS 或 PAIRLIST，且知道回测 id",
            "read",
            {"backtest_id": _S("string", "回测 id")},
            t_guards_report,
            required=["backtest_id"],
        ),
        PlatformTool(
            "freqtrade_check",
            "检查 Freqtrade 策略",
            "strategy",
            "检查一段 Freqtrade（IStrategy）策略代码能否导入平台：识别用到的功能（ROI、追踪止损、custom_stoploss、"
            "大周期 informative、hyperopt 参数），列出会被忽略的部分和跑不了的原因（做空、杠杆、DCA、不支持的指标），"
            "并在合成行情上试跑。导入本身在「我的策略 → 导入 Freqtrade」里做。",
            "用户贴了 Freqtrade 策略，或问能不能把 Freqtrade 策略搬过来",
            "read",
            {"source": _S("string", "Freqtrade 策略的完整代码")},
            t_freqtrade_check,
            required=["source"],
        ),
        PlatformTool(
            "list_datasets",
            "研究数据集",
            "data",
            "行情与数据里已同步的数据集（字段、标的、覆盖区间、发布滞后）。",
            "随时",
            "read",
            {"field_name": _S("string", "字段名，如 funding，可空")},
            t_list_datasets,
        ),
        PlatformTool(
            "list_events",
            "事件流",
            "data",
            "交易所公告、新闻（含方向与强度标注）、宏观日程，按发布时间倒序。",
            "同步过公告 / 新闻 / 经济日历后",
            "read",
            {
                "kind": _S("string", "announcement / news / macro，可空"),
                "coin": _S("string", "币种，如 SOL，可空"),
                "limit": _S("integer", "条数，最多 100"),
            },
            t_list_events,
        ),
        PlatformTool(
            "list_sessions",
            "实验列表",
            "trading",
            "纸面模拟与交易所模拟盘实验：状态、成交数、收益、是否熔断。",
            "随时",
            "read",
            {"status": _S("string", "running / stopped …，可空")},
            t_list_sessions,
        ),
        PlatformTool(
            "node_health",
            "节点状态",
            "trading",
            "运行节点的健康状况：在线 worker、超过 2 分钟没心跳的实验、交易所时钟偏差、限频暂停情况。",
            "实验不动了、告警说失联、怀疑服务器有问题时",
            "read",
            {},
            t_node_health,
        ),
        PlatformTool(
            "microlive_status",
            "微实盘状态",
            "trading",
            "微实盘（真实资金、小额上限）能不能启动、卡在哪一步、是否急停，各毕业策略当前的资金上限和升档条件，运行中的微实盘实验。"
            "启动和升档只能在微实盘页面由管理员输入确认码完成。",
            "问能不能上实盘、实盘上限多少、为什么启动不了时",
            "read",
            {},
            t_microlive_status,
        ),
        PlatformTool(
            "microlive_kill",
            "微实盘一键停止",
            "trading",
            "把所有微实盘实验平仓并停止（真实成交），之后拒绝新的启动，直到管理员解除。",
            "用户明确要停掉全部实盘时",
            "confirm",
            {},
            t_microlive_kill,
            summary=lambda a: "一键停止全部微实盘：平掉所有真实持仓并停止",
        ),
        PlatformTool(
            "get_session",
            "实验详情",
            "trading",
            "一个实验的持仓、最近成交、事件和模拟盘状态。",
            "知道实验 id 时",
            "read",
            {"session_id": _S("string", "实验 id")},
            t_get_session,
            required=["session_id"],
        ),
        PlatformTool(
            "start_session",
            "启动实验",
            "trading",
            "启动纸面模拟。execution=paper 用平台撮合；execution=demo 会向交易所模拟盘下单（模拟资金）。",
            "策略已批准；demo 需要先接好模拟盘连接",
            "confirm",
            {
                "slug": _S("string", "策略 slug"),
                "execution": _S("string", "paper 或 demo", enum=["paper", "demo"]),
                "demo_connection_id": _S("string", "模拟盘连接 id（demo 时必填）"),
                "exchange": _S("string", "binance / okx / bybit"),
                "initial_cash": _S("number", "初始资金"),
                "symbol": _S("string", "标的，可空"),
            },
            t_start_session,
            required=["slug"],
            summary=lambda a: (
                f"启动「{a.get('slug')}」的"
                f"{'交易所模拟盘实验（会向模拟盘下单，模拟资金）' if a.get('execution') == 'demo' else '纸面模拟'}"
            ),
        ),
        PlatformTool(
            "stop_session",
            "停止实验",
            "trading",
            "一键停止实验；close_positions=true 时先平掉全部持仓（模拟成交）。",
            "实验在运行",
            "confirm",
            {"session_id": _S("string", "实验 id"), "close_positions": _S("boolean", "是否平仓，默认是")},
            t_stop_session,
            required=["session_id"],
            summary=lambda a: (
                f"停止实验 {a.get('session_id')}" + ("并平掉全部持仓" if a.get("close_positions", True) else "")
            ),
        ),
        PlatformTool(
            "decay_report",
            "衰减与重训",
            "validation",
            "运行中的纸面模拟 / 模拟盘实验是否跑出了回测的正常区间（衰减判定、原因、降级建议），"
            "模型定期重训的计划和上次结果，当前市场状态（趋势 / 震荡 / 高波动）和各实验的建议权重。",
            "问策略是不是失效了、为什么最近一直亏、模型要不要重训、该给哪个策略多分资金时",
            "read",
            {},
            t_decay_report,
        ),
        PlatformTool(
            "graduation",
            "晋级关卡",
            "validation",
            "策略所处阶段（回测 → 纸面 → 模拟盘 → 晋级）和晋级检查。",
            "随时",
            "read",
            {"slug": _S("string", "策略 slug；空则列出全部")},
            t_graduation,
        ),
        PlatformTool(
            "promote_strategy",
            "晋级策略",
            "validation",
            "把策略晋级到下一阶段（全部检查通过才可以）。",
            "graduation 显示 passed=true",
            "confirm",
            {"slug": _S("string", "策略 slug"), "note": _S("string", "备注")},
            t_promote,
            required=["slug"],
            summary=lambda a: f"把策略「{a.get('slug')}」晋级到下一阶段（记入审计；毕业后可以上微实盘，要人另外确认）",
        ),
        PlatformTool(
            "fidelity_report",
            "真实度报告",
            "validation",
            "对同一策略的纸面 / 模拟盘实验生成真实度报告：收益差拆成延迟、滑点、手续费、拒单、部分成交，"
            "并给成本校准建议。",
            "实验已有成交",
            "run",
            {"session_ids": _S("array", "1–3 个实验 id", items={"type": "string"})},
            t_fidelity,
            required=["session_ids"],
        ),
        PlatformTool(
            "apply_calibration",
            "应用成本校准",
            "validation",
            "把真实度报告里的建议滑点和手续费设为这家交易所的回测成本。",
            "报告里有校准建议",
            "confirm",
            {"report_id": _S("string", "报告 id")},
            t_apply_calibration,
            required=["report_id"],
            summary=lambda a: f"用报告 {a.get('report_id')} 的校准值替换回测成本假设",
            category="config",
        ),
        PlatformTool(
            "start_stress_drill",
            "压力演练",
            "validation",
            "在 4 个历史极端日（3·12、5·19、FTX、日元套息）上加速回放，注入断线、拒单、延迟。后台运行。",
            "策略已保存",
            "run",
            {
                "slug": _S("string", "策略 slug"),
                "scenarios": _S("array", "场景 id，可空表示全部", items={"type": "string"}),
            },
            t_stress,
            required=["slug"],
        ),
        PlatformTool(
            "stress_result",
            "演练结果",
            "validation",
            "压力演练的结果；不传 run_id 列出最近记录。",
            "演练开始后",
            "read",
            {"run_id": _S("string", "演练 id，可空")},
            t_stress_result,
        ),
        PlatformTool(
            "watch_session",
            "值守员检查",
            "validation",
            "逐笔解释成交，并标出和策略说明书矛盾的行为。",
            "实验有成交",
            "run",
            {"session_id": _S("string", "实验 id")},
            t_watch,
            required=["session_id"],
        ),
        PlatformTool(
            "run_checkup",
            "策略体检",
            "checkup",
            "给策略做上线前体检：截断数据重算查未来函数、不同预热对比、成本翻倍、参数上下挪 10–20% 看是否孤峰，"
            "最后由红队列出最可能亏钱的原因。后台运行。晋级关卡要求当前代码体检没有严重项。",
            "策略已保存",
            "run",
            {"slug": _S("string", "策略 slug"), "bars_limit": _S("integer", "K 线根数，300–5000")},
            t_checkup,
            required=["slug"],
        ),
        PlatformTool(
            "checkup_result",
            "体检结果",
            "checkup",
            "体检单：四项检查的结论（ok / warn / bad）和红队意见；不传 run_id 列出最近记录。",
            "体检开始后",
            "read",
            {"run_id": _S("string", "体检 id，可空"), "slug": _S("string", "只看这个策略，可空")},
            t_checkup_result,
        ),
        PlatformTool(
            "start_tuning",
            "智能调参",
            "checkup",
            "用 optuna 贝叶斯搜索调参数（范围读策略里的 PARAM_SPACE，或传 space）；只用样本内选参，样本外验证。"
            "multi_objective=true 同时压低最大回撤。每组试验都记进试验台账（DSR 会相应变严）。",
            "策略声明了 PARAMS",
            "run",
            {
                "slug": _S("string", "策略 slug"),
                "n_trials": _S("integer", "试验次数 5–150"),
                "objective": _S(
                    "string", "优化目标", enum=["sharpe", "total_return", "calmar", "sortino", "excess_return"]
                ),
                "multi_objective": _S("boolean", "是否同时最小化回撤"),
                "space": _S("object", "参数范围 {名字: {low, high, step?} | {choices: [...]}}，可空"),
            },
            t_start_tuning,
            required=["slug"],
        ),
        PlatformTool(
            "tuning_result",
            "调参结果",
            "checkup",
            "参数优化 / 智能调参的结果：最优参数、样本内外成绩、过拟合提示。",
            "调参完成后",
            "read",
            {"opt_id": _S("string", "优化记录 id，可空"), "slug": _S("string", "策略 slug，可空")},
            t_tuning_result,
        ),
        PlatformTool(
            "list_factors",
            "因子库",
            "library",
            "因子库里的因子（表达式、状态、评估摘要）；query 按名字 / 表达式搜索。",
            "要复用已有因子、避免重复研究时",
            "read",
            {"status": _S("string", "按状态过滤，可空"), "query": _S("string", "搜索关键词，可空")},
            t_list_factors,
        ),
        PlatformTool(
            "list_models",
            "模型库",
            "library",
            "预测模型库与最近训练记录（学习器、样本外成绩）。",
            "要复用或比较已训练的模型时",
            "read",
            {"status": _S("string", "按状态过滤，可空")},
            t_list_models,
        ),
        PlatformTool(
            "list_portfolios",
            "组合方案",
            "library",
            "组合与风控方案库与最近评估记录。",
            "设计或复用组合方案时",
            "read",
            {"status": _S("string", "按状态过滤，可空")},
            t_list_portfolios,
        ),
        PlatformTool(
            "list_executions",
            "执行方案",
            "library",
            "下单执行方案库（拆单算法）与最近仿真记录。",
            "设计或复用执行方案时",
            "read",
            {"status": _S("string", "按状态过滤，可空")},
            t_list_executions,
        ),
        PlatformTool(
            "trial_ledger",
            "试验台账",
            "library",
            "各研究类型的试验次数，以及按试验次数修正后的最好成绩（DSR / t 门槛）。报告结论前用它说明多重检验的影响。",
            "评价「最好成绩」是否可信时",
            "read",
            {"domain": _S("string", "研究类型，可空", enum=["strategy", "factor", "model", "portfolio", "execution"])},
            t_trial_ledger,
        ),
        PlatformTool(
            "fill_comparisons",
            "实盘对照",
            "library",
            "真实成交与模拟结果的对照记录（逐笔归因）。",
            "要核对模拟与真实成交差异时",
            "read",
            {},
            t_fill_comparisons,
        ),
        PlatformTool(
            "delete_strategy",
            "删除策略",
            "library",
            "删除一个平台策略（回测记录保留）。",
            "研究者明确要清理时",
            "confirm",
            {"slug": _S("string", "策略 slug")},
            t_delete_strategy,
            summary=lambda a: f"删除策略 {a.get('slug')}",
            required=["slug"],
            category="delete",
        ),
        PlatformTool(
            "delete_backtests",
            "删除回测记录",
            "library",
            "批量删除回测记录（不可恢复）。",
            "研究者明确要清理时",
            "confirm",
            {"ids": _S("array", "回测 id 列表", items={"type": "string"})},
            t_delete_backtests,
            summary=lambda a: f"删除 {len(a.get('ids') or [])} 条回测记录",
            required=["ids"],
            category="delete",
        ),
        PlatformTool(
            "delete_factor",
            "删除因子",
            "library",
            "从因子库删除一个因子（试验台账保留）。",
            "研究者明确要清理时",
            "confirm",
            {"fid": _S("string", "因子 id")},
            t_delete_factor,
            summary=lambda a: f"删除因子 {a.get('fid')}",
            required=["fid"],
            category="delete",
        ),
        PlatformTool(
            "community_post",
            "发帖到策略广场",
            "collab",
            "以 Agent 身份把研究结论发到策略广场（帖子标记为「Agent 发布 · 代某人」），可附策略、回测与引用。"
            "正文用 Markdown；引用写成单独一行 {{ref:类型:id}}（类型：backtest / strategy / post / file），并在 refs 里列出。",
            "研究者明确要求分享到广场时",
            "confirm",
            {
                "title": _S("string", "标题（2–120 字）"),
                "body": _S("string", "正文 Markdown"),
                "type": _S("string", "article / question / strategy"),
                "tags": _S("array", "标签（最多 8 个）", items={"type": "string"}),
                "strategy_slug": _S("string", "附带的策略 slug（策略分享时）"),
                "backtest_id": _S("string", "附带的回测 id（推荐）"),
                "refs": _S("array", "正文引用 [{type, id}]", items={"type": "object"}),
            },
            t_community_post,
            summary=lambda a: f"以 Agent 身份在策略广场发布「{a.get('title')}」",
            required=["title", "body"],
            category="save",
        ),
        PlatformTool(
            "list_groups",
            "我的群组",
            "collab",
            "列出工作区里我加入的与公开的群组（id、成员数、未读、进行中任务数）。",
            "要在群里发消息或查任务前",
            "read",
            {},
            t_list_groups,
        ),
        PlatformTool(
            "group_message",
            "在群组里发消息",
            "collab",
            "以 Agent 身份（代当前用户）在群组里发一条消息，可引用回测 / 策略 / 任务。",
            "研究者要求把结果同步给群组时",
            "confirm",
            {
                "group_id": _S("string", "群组 id（先用 list_groups 查）"),
                "body": _S("string", "消息 Markdown"),
                "refs": _S("array", "引用 [{type, id}]", items={"type": "object"}),
            },
            t_group_message,
            summary=lambda a: f"在群组里发消息：{str(a.get('body') or '')[:40]}",
            required=["group_id", "body"],
            category="save",
        ),
        PlatformTool(
            "my_tasks",
            "我的任务",
            "collab",
            "指派给当前用户的群组任务（标题、状态、优先级、截止时间、是否逾期）。",
            "研究者问「我有什么任务」或开始干活前",
            "read",
            {"include_closed": _S("boolean", "是否包含已完成 / 已取消")},
            t_my_tasks,
        ),
        PlatformTool(
            "update_task",
            "更新任务状态",
            "collab",
            "把任务标为进行中 / 待验收 / 完成，并可在群里附一句进展说明。",
            "完成了指派的研究任务后",
            "confirm",
            {
                "task_id": _S("string", "任务 id"),
                "status": _S("string", "todo / doing / review / done / cancelled"),
                "note": _S("string", "进展说明（会发到群里）"),
            },
            t_update_task,
            summary=lambda a: f"把任务 {a.get('task_id')} 标为 {a.get('status')}",
            required=["task_id"],
            category="save",
        ),
        PlatformTool(
            "morning_brief",
            "晨报",
            "validation",
            "最近一份 AI 晨报（市场、因子、实验、值守员结论、等你决定的事）。",
            "生成过晨报后",
            "read",
            {},
            t_brief,
        ),
        PlatformTool(
            "list_members",
            "工作区成员",
            "collab",
            "按名字 / 用户名搜索工作区成员（user_id、是否在线），私聊或分享前先查。",
            "要私聊、分享给某人或 @ 某人前",
            "read",
            {"q": _S("string", "搜索词，可空")},
            t_list_members,
        ),
        PlatformTool(
            "share_to",
            "分享",
            "collab",
            "把平台里的内容分享到群组、私聊或策略广场。ref_type：backtest / strategy / post / project / report / link"
            "（link 的 ref_id 是站内路由，如 factors/<id>；report 的 ref_id 是 <对话 id>/<报告 id>）。",
            "研究者要求「发给某人 / 发到群里 / 发到广场」时",
            "confirm",
            {
                "target": _S("string", "group / dm / square", enum=["group", "dm", "square"]),
                "target_id": _S("string", "群组 id（list_groups）或成员 user_id（list_members）；广场不填"),
                "ref_type": _S("string", "被分享内容的类型"),
                "ref_id": _S("string", "被分享内容的 id"),
                "title": _S("string", "标题（分享到广场时作帖子标题）"),
                "note": _S("string", "附言 Markdown"),
            },
            t_share_to,
            summary=lambda a: (
                f"分享 {a.get('ref_type')}:{a.get('ref_id')} 到{ {'group': '群组', 'dm': '私聊', 'square': '策略广场'}.get(str(a.get('target')), a.get('target')) }"
            ),
            required=["target", "ref_type", "ref_id"],
            category="save",
        ),
        PlatformTool(
            "notify_user",
            "提醒研究者",
            "collab",
            "给当前研究者发一条站内通知（头像会出现红点），用于长任务完成、发现需要研究者决定的事。不要用来闲聊。",
            "需要研究者注意，但对方可能不在这个页面时",
            "read",
            {
                "title": _S("string", "一句话标题"),
                "body": _S("string", "详情，可空"),
                "link": _S("string", "站内路由，可空，如 lab/<id>"),
            },
            t_notify_user,
            required=["title"],
        ),
    ]
    return {t.name: t for t in tools}


def t_list_members(q: str = "") -> Any:
    from ..api import routes_collab as C

    return [{k: m.get(k) for k in ("user_id", "name", "username", "online")} for m in C.members(q)["members"]][:30]


def t_share_to(target: str, ref_type: str, ref_id: str, target_id: str = "", title: str = "", note: str = "") -> Any:
    from ..api import routes_collab as C

    body = _call(
        lambda: C.share_to(
            C.ShareToIn(
                target=target,
                target_id=target_id or "",
                ref={"type": ref_type, "id": ref_id, "title": title},
                note=note or "",
                title=title or "",
            )
        )
    )
    if isinstance(body, dict) and body.get("ok"):
        return {"ok": True, "link": body.get("link")}
    return body


def t_notify_user(title: str, body: str = "", link: str = "") -> Any:
    from ..platform import context

    ctx = context.current()
    n = _svc().collab.notify(
        [ctx.user_id],
        kind="system",
        title=f"QuantBot：{title}"[:200],
        body=body,
        link=link,
        actor_id="agent",
        actor_name="QuantBot",
        workspace_id=ctx.workspace_id,
    )
    return {"ok": bool(n)}


_REGISTRY: dict[str, PlatformTool] | None = None


def registry() -> dict[str, PlatformTool]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = build()
    return _REGISTRY


def scenes_for(page: str, chosen: list[str] | None) -> list[str]:
    if chosen is not None:
        return [s for s in chosen if s in SCENES]
    return PAGE_SCENES.get(page, [])


def tools_for(scenes: list[str]) -> list[PlatformTool]:
    return [t for t in registry().values() if t.scene in scenes]


def prompt_block(scenes: list[str]) -> str:
    """统一生成的工具说明，追加到系统提示末尾。只列本轮加载的工具。"""
    tools = tools_for(scenes)
    if not tools:
        return ""
    lines = [
        "【平台工具】本轮可以调用下列平台工具（场景：" + "、".join(SCENES[s]["label"] for s in scenes) + "）。",
        "用工具拿真实数据再回答，不要编造数字；需要 id 时先用列表类工具查。",
        "风险级别为「需要你确认」的工具不会立即执行：调用后会给用户一张确认卡片，你要在回答里说明要做什么、为什么，等用户确认。",
        "平台没有实盘下单能力，也不要建议绕过模拟盘。",
        capability_note(),
    ]
    for t in tools:
        lines.append(f"- {t.name}（{t.title}，{RISK_LABEL[t.risk]}）：{t.description} 可用时机：{t.when}。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 执行与确认
# ---------------------------------------------------------------------------
def invoke(name: str, args: dict[str, Any], *, user: str, conversation: str = "") -> dict[str, Any]:
    """模型调用工具。confirm 类只登记待确认动作。返回 {result, record}，record 给前端显示。"""
    tool = registry().get(name)
    if tool is None:
        return {"result": {"error": f"未知工具 {name}"}, "record": {"tool": name, "status": "error"}}
    args = {k: v for k, v in (args or {}).items() if k in tool.params}
    rec: dict[str, Any] = {"tool": name, "title": tool.title, "risk": tool.risk, "args": args}
    if tool.risk == "confirm":
        missing = [k for k in tool.required if not args.get(k)]
        if missing:
            return {"result": {"error": f"缺少参数：{'、'.join(missing)}"}, "record": {**rec, "status": "error"}}
        action = {
            "id": f"act_{uuid.uuid4().hex[:10]}",
            "tool": name,
            "title": tool.title,
            "args": args,
            "summary": tool.summary(args) if tool.summary else tool.title,
            "status": "pending",
            "created_by": user,
            "created_at": int(time.time() * 1000),
            "conversation": conversation,
        }
        _svc().store.upsert(ACTIONS, action)
        rec.update(status="pending", action_id=action["id"], summary=action["summary"])
        return {
            "result": {
                "status": "needs_confirmation",
                "action_id": action["id"],
                "summary": action["summary"],
                "note": "已生成确认卡片，等用户确认后才执行",
            },
            "record": rec,
        }
    t0 = time.perf_counter()
    try:
        out = tool.handler(**args)
    except TypeError as exc:
        out = {"error": f"参数不对：{exc}"}
    except Exception as exc:
        out = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    ok = not (isinstance(out, dict) and out.get("error"))
    rec.update(status="done" if ok else "error", ms=int((time.perf_counter() - t0) * 1000), preview=_preview(out))
    return {"result": out, "record": rec}


def _preview(out: Any) -> str:
    if isinstance(out, dict) and out.get("error"):
        return str(out["error"])[:160]
    if isinstance(out, list):
        return f"{len(out)} 条"
    text = json.dumps(out, ensure_ascii=False, default=str)
    return text[:160] + ("…" if len(text) > 160 else "")


def confirm(action_id: str, *, user: str, approve: bool) -> dict[str, Any]:
    store = _svc().store
    act = store.get(ACTIONS, action_id)
    if act is None:
        raise ValueError("这个操作不存在")
    if act["status"] != "pending":
        raise ValueError(
            f"这个操作已经{ {'done': '执行过', 'canceled': '取消了', 'error': '失败过'}.get(act['status'], '处理过') }"
        )
    if act.get("created_by") and act["created_by"] != user:
        raise ValueError("只能由发起对话的人确认")
    if not approve:
        act.update(status="canceled", decided_at=int(time.time() * 1000))
        store.upsert(ACTIONS, act)
        return act
    tool = registry()[act["tool"]]
    try:
        out = tool.handler(**act["args"])
    except Exception as exc:
        out = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    ok = not (isinstance(out, dict) and out.get("error"))
    act.update(status="done" if ok else "error", result=out, decided_at=int(time.time() * 1000))
    store.upsert(ACTIONS, act)
    _svc().audit.record(
        "ai.action",
        object_type="ai_action",
        object_id=action_id,
        details={"tool": act["tool"], "args": act["args"], "ok": ok},
    )
    return act


def catalog() -> dict[str, Any]:
    return {
        "scenes": [{"id": k, **v} for k, v in SCENES.items()],
        "page_scenes": PAGE_SCENES,
        "tools": [t.view() for t in registry().values()],
        "risk_labels": RISK_LABEL,
    }


__all__ = [
    "ACTIONS",
    "APPROVAL_CATEGORIES",
    "RULE_REQUIRED",
    "SCENES",
    "catalog",
    "confirm",
    "invoke",
    "prompt_block",
    "registry",
    "scenes_for",
    "tools_for",
]
