"""毕业闸门：策略从回测 → 纸面模拟 → 交易所模拟盘 → 毕业，每一级都要过数字门槛，并由人确认。

- 回测 → 纸面模拟：缩减夏普（DSR，按试验次数修正）≥ 门槛，最大回撤 ≤ 门槛，当前代码的策略体检没有严重项；
- 纸面模拟 → 模拟盘：纸面模拟实际跑满 7 天，回撤 ≤ 门槛，与回测的真实度偏差 ≤ 门槛；
- 模拟盘 → 毕业：交易所模拟盘实际跑满 14 天，回撤 ≤ 门槛，真实度偏差 ≤ 门槛，期间没有未解除的熔断。

"毕业"本身不下实盘单：毕业的策略只是获得了上「微实盘」的资格（见 live/micro.py），上不上、用多少钱由人确认。
天数按实际经过的时间算，回放和加速不能替代。每次晋级都写审计记录。
"""

from __future__ import annotations

import time
from typing import Any

GRAD = "graduation"
GRAD_CONFIG = "graduation_config"
STAGES = ["backtest", "paper", "demo", "graduated"]
STAGE_LABEL = {"backtest": "回测研究", "paper": "纸面模拟", "demo": "交易所模拟盘", "graduated": "毕业（可上微实盘）"}
DEFAULTS: dict[str, Any] = {
    "min_dsr": 0.95,
    "max_drawdown_backtest": 0.30,
    "paper_days": 7,
    "demo_days": 14,
    "max_drawdown_live": 0.15,
    "max_deviation_paper_bps": 150.0,
    "max_deviation_demo_bps": 300.0,
}
HOUR = 3_600_000


def config(store: Any) -> dict[str, Any]:
    saved = store.get(GRAD_CONFIG, "default") or {}
    return {**DEFAULTS, **{k: saved[k] for k in DEFAULTS if k in saved}}


def save_config(store: Any, values: dict[str, Any]) -> dict[str, Any]:
    cur = config(store)
    for k, v in values.items():
        if k in DEFAULTS and v is not None:
            cur[k] = float(v)
    store.upsert(GRAD_CONFIG, {"id": "default", **cur})
    return cur


def record(store: Any, slug: str) -> dict[str, Any]:
    return store.get(GRAD, slug) or {"id": slug, "stage": "backtest", "history": []}


def _max_dd(points: list[dict[str, Any]]) -> float:
    peak, dd = 0.0, 0.0
    for p in points:
        v = float(p.get("v") or 0)
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd


def _check(label: str, ok: bool | None, value: str, need: str) -> dict[str, Any]:
    return {"label": label, "ok": ok, "value": value, "need": need}


def _backtest_checks(store: Any, slug: str, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from ..store import Collections
    from .discipline import deflated_sharpe, strategy_record

    rows = [r for r in store.list(Collections.BACKTESTS) if r.get("slug") == slug and r.get("status") == "done"]
    rows.sort(key=lambda r: r.get("created_at") or 0)
    if not rows:
        return [_check("有一次成功的回测", False, "没有", "至少 1 次")], {}
    last = rows[-1]
    m = last.get("metrics") or {}
    rec = strategy_record(store, slug) or {}
    dsr = rec.get("dsr")
    basis = f"按 {rec.get('n')} 次试验修正" if dsr is not None else ""
    if dsr is None and m.get("sharpe") is not None:
        hours = max(1.0, ((last.get("end_ts") or 0) - (last.get("start_ts") or 0)) / HOUR)
        dsr = deflated_sharpe(
            float(m["sharpe"]), n_trials=1, sr_ann_var=0.0, periods=int(hours), periods_per_year=8760
        )["dsr"]
        basis = "只有 1 次试验，等同概率夏普"
    dd = m.get("max_drawdown")
    checks = [
        _check(
            "缩减夏普（DSR）",
            None if dsr is None else dsr >= cfg["min_dsr"],
            "—" if dsr is None else f"{dsr:.2f}（{basis}）",
            f"≥ {cfg['min_dsr']:.2f}",
        ),
        _check(
            "回测最大回撤",
            None if dd is None else float(dd) <= cfg["max_drawdown_backtest"],
            "—" if dd is None else f"{float(dd):.1%}",
            f"≤ {cfg['max_drawdown_backtest']:.0%}",
        ),
    ]
    ck, ck_ev = _checkup_check(store, slug)
    checks.append(ck)
    return checks, {"backtest_id": last.get("id"), "sharpe": m.get("sharpe"), "dsr": dsr, **ck_ev}


def _checkup_check(store: Any, slug: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """策略体检：当前这版代码要体检过，且没有"严重"项（未来函数、预热不够、成本一翻倍就亏、孤峰参数）。"""
    import hashlib

    rows = [r for r in store.list("checkups") if r.get("strategy_id") == slug and r.get("status") == "done"]
    if not rows:
        return _check("策略体检没有严重问题", False, "还没体检", "体检一次，没有严重项"), {}
    last = max(rows, key=lambda r: r.get("created_at") or 0)
    try:
        from ..strategies.repo import StrategyRepository

        rec = StrategyRepository(store.root / "strategies").get(slug)
        cur = hashlib.sha256(rec.source.encode()).hexdigest()[:16] if rec else None
    except Exception:
        cur = None
    items = (last.get("report") or {}).get("items") or []
    bad = [i["title"] for i in items if i.get("verdict") == "bad"]
    ev = {"checkup_id": last["id"]}
    if cur and last.get("source_hash") and cur != last["source_hash"]:
        return _check("策略体检没有严重问题", False, "代码改过了，体检结果已过期", "对当前代码重新体检"), ev
    if bad:
        return _check("策略体检没有严重问题", False, "严重：" + "、".join(bad), "没有严重项"), ev
    warn = sum(1 for i in items if i.get("verdict") == "warn")
    return _check("策略体检没有严重问题", True, f"通过（{warn} 项需注意）" if warn else "通过", "没有严重项"), ev


def _latest_fidelity(store: Any, slug: str, execution: str) -> dict[str, Any] | None:
    from ..engine.fidelity import REPORTS

    reps = [r for r in store.list(REPORTS) if r.get("strategy_id") == slug and execution in (r.get("deviation") or {})]
    reps.sort(key=lambda r: r.get("created_at") or 0)
    return reps[-1]["deviation"][execution] | {"report_id": reps[-1]["id"]} if reps else None


def _live_checks(
    store: Any, slug: str, cfg: dict[str, Any], execution: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from ..store import Collections

    days = cfg["paper_days"] if execution == "paper" else cfg["demo_days"]
    max_dev = cfg["max_deviation_paper_bps"] if execution == "paper" else cfg["max_deviation_demo_bps"]
    name = "纸面模拟" if execution == "paper" else "交易所模拟盘"
    sessions = [
        s
        for s in store.list(Collections.PAPER_SESSIONS)
        if s.get("strategy_id") == slug and (s.get("execution") or "paper") == execution and s.get("source") != "demo"
    ]
    if not sessions:
        return [_check(f"跑过{name}（真实行情）", False, "没有", f"至少 {days:g} 天")], {}
    best = max(sessions, key=lambda s: (s.get("last_update") or 0) - (s.get("started_at") or 0))
    hours = max(0.0, ((best.get("last_update") or 0) - (best.get("started_at") or 0)) / HOUR)
    dd = _max_dd(best.get("equity_points") or [])
    fid = _latest_fidelity(store, slug, execution)
    dev = fid.get("deviation_bps") if fid else None
    checks = [
        _check(f"{name}实际运行", hours >= days * 24, f"{hours / 24:.1f} 天", f"≥ {days:g} 天"),
        _check(f"{name}最大回撤", dd <= cfg["max_drawdown_live"], f"{dd:.1%}", f"≤ {cfg['max_drawdown_live']:.0%}"),
        _check(
            "与回测的真实度偏差",
            None if dev is None else dev <= max_dev,
            "还没生成真实度报告" if dev is None else f"{dev:.0f} bp",
            f"≤ {max_dev:.0f} bp",
        ),
    ]
    if execution == "demo":
        halted = bool((best.get("demo") or {}).get("halted"))
        checks.append(_check("没有未解除的熔断", not halted, "熔断中" if halted else "正常", "正常"))
    return checks, {
        "session_id": best["id"],
        "days": round(hours / 24, 2),
        "drawdown": dd,
        "fidelity_report": fid.get("report_id") if fid else None,
    }


def evaluate(store: Any, slug: str) -> dict[str, Any]:
    """当前阶段和晋级到下一阶段要过的检查。ok=None 表示数据还不够，算未通过。"""
    cfg = config(store)
    rec = record(store, slug)
    stage = rec.get("stage") or "backtest"
    nxt = STAGES[STAGES.index(stage) + 1] if stage != "graduated" else None
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    if nxt == "paper":
        checks, evidence = _backtest_checks(store, slug, cfg)
    elif nxt == "demo":
        checks, evidence = _live_checks(store, slug, cfg, "paper")
    elif nxt == "graduated":
        checks, evidence = _live_checks(store, slug, cfg, "demo")
    passed = bool(checks) and all(c["ok"] is True for c in checks)
    return {
        "strategy": slug,
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "next": nxt,
        "next_label": STAGE_LABEL.get(nxt or "", ""),
        "checks": checks,
        "passed": passed,
        "evidence": evidence,
        "history": rec.get("history") or [],
        "config": cfg,
        "live_trading": "closed",
    }


def promote(store: Any, slug: str, *, confirm: bool, user: str, note: str = "") -> dict[str, Any]:
    """晋级一级。必须 confirm=True（人点了确认），且全部检查通过。"""
    ev = evaluate(store, slug)
    if not ev["next"]:
        raise ValueError("已经是最后一级")
    if not ev["passed"]:
        bad = [c["label"] for c in ev["checks"] if c["ok"] is not True]
        raise ValueError("还有检查没通过：" + "、".join(bad))
    if not confirm:
        raise ValueError("晋级需要你确认")
    rec = record(store, slug)
    rec["history"] = [
        *(rec.get("history") or []),
        {
            "from": ev["stage"],
            "to": ev["next"],
            "at": int(time.time() * 1000),
            "by": user,
            "checks": ev["checks"],
            "evidence": ev["evidence"],
            "note": note,
        },
    ]
    rec["stage"] = ev["next"]
    store.upsert(GRAD, rec)
    return evaluate(store, slug)


def demote(store: Any, slug: str, *, to: str, user: str, note: str = "") -> dict[str, Any]:
    if to not in STAGES:
        raise ValueError("未知阶段")
    rec = record(store, slug)
    if STAGES.index(to) >= STAGES.index(rec.get("stage") or "backtest"):
        raise ValueError("只能退回到更早的阶段")
    rec["history"] = [
        *(rec.get("history") or []),
        {"from": rec.get("stage"), "to": to, "at": int(time.time() * 1000), "by": user, "note": note or "退回"},
    ]
    rec["stage"] = to
    store.upsert(GRAD, rec)
    return evaluate(store, slug)


def board(store: Any, slugs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """所有策略的阶段一览：(slug, 名字) 列表 → 每个策略的阶段、下一阶段、是否可以晋级。"""
    out = []
    for slug, name in slugs:
        ev = evaluate(store, slug)
        done = sum(1 for c in ev["checks"] if c["ok"] is True)
        out.append(
            {
                "strategy": slug,
                "name": name,
                "stage": ev["stage"],
                "stage_label": ev["stage_label"],
                "next": ev["next"],
                "next_label": ev["next_label"],
                "passed": ev["passed"],
                "progress": f"{done}/{len(ev['checks'])}" if ev["checks"] else "—",
            }
        )
    out.sort(key=lambda r: (-STAGES.index(r["stage"]), not r["passed"], r["name"]))
    return out


__all__ = ["DEFAULTS", "STAGES", "STAGE_LABEL", "board", "config", "demote", "evaluate", "promote", "save_config"]
