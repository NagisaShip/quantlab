"""AI 值守员：盯着运行中的实时实验，逐笔解释成交，并标出和策略说明书（SPEC.md）对不上的行为。

两层：
1. 规则层（总是运行）：从说明书里读出明确的约束——只做多、杠杆上限、仓位上限、每天最多几笔、
   回撤超过多少该停、连续亏损几次该停、标的范围——逐条对照实际成交和账户；强平、熔断、风控拒单也会标出来。
2. 模型层（配置了模型时）：把说明书和最近的成交交给模型，请它用一句话解释每笔成交，
   并指出规则层读不出来的矛盾（例如说明书说"趋势行情才入场"，却在横盘里频繁开仓）。
   模型名、提示词版本和摘要记在结果里，可以追溯。

值守员只看、只说，不会替你下单或停策略；它发现的问题进 AI 晨报，由你决定怎么处理。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

REVIEWS = "watcher_reviews"
PROMPT_VERSION = "watcher-v1"
SYSTEM_PROMPT = (
    "你是量化交易平台的值守员。给你一份策略说明书和这个策略最近的成交记录（模拟盘，非真实资金）。"
    "请做两件事：1) 用一句中文解释每笔成交最可能的原因（依据说明书里的规则和成交里的原因字段，不要编造行情）；"
    "2) 找出和说明书矛盾的行为。只输出 JSON："
    '{"explanations": [{"fill_id": "...", "text": "..."}], '
    '"contradictions": [{"fill_id": "... 或空", "text": "..."}]}。'
    "没有矛盾就给空数组。不要预测行情，不要给投资建议。"
)
PROMPT_SHA = hashlib.sha1(SYSTEM_PROMPT.encode()).hexdigest()[:10]
SIDE = {"buy": "买入", "sell": "卖出"}
REASON = {
    "signal": "策略信号",
    "stop_loss": "止损",
    "take_profit": "止盈",
    "liquidation": "强平",
    "liquidate": "手动平仓",
    "rebalance": "调仓",
    "": "策略信号",
}


def _px(p: float) -> str:
    return f"{p:,.2f}" if abs(p) >= 1 else f"{p:.6g}"


def _num(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def parse_rules(spec: str) -> dict[str, Any]:
    """从说明书里读出能机器核对的约束。读不出来的就不管，交给模型层。"""
    t = spec or ""
    low = t.lower()
    rules: dict[str, Any] = {}
    if re.search(r"只做多|不做空|仅做多|long[- ]only|no short", t, re.I):
        rules["long_only"] = True
    if re.search(r"不(加|用|使用)杠杆|无杠杆|no leverage", t, re.I):
        rules["max_leverage"] = 1.0
    m = re.search(r"杠杆[^。\n，,；;]{0,8}?(?:不超过|最多|上限|≤|<=)\s*(\d+(?:\.\d+)?)", t)
    if m:
        rules["max_leverage"] = float(m.group(1))
    m = re.search(r"(?:单(?:个)?(?:标的)?)?仓位[^。\n，,；;]{0,10}?(?:不超过|最多|上限|≤|<=)\s*(\d+(?:\.\d+)?)\s*%", t)
    if m:
        rules["max_weight"] = float(m.group(1)) / 100
    m = re.search(r"每天[^。\n，,；;]{0,6}?(?:最多|不超过)\s*(\d+)\s*(?:笔|次)", t)
    if m:
        rules["max_trades_per_day"] = int(m.group(1))
    m = re.search(r"回撤[^。\n，,；;]{0,10}?(?:超过|达到|大于|>|≥)\s*(\d+(?:\.\d+)?)\s*%", t)
    if m:
        rules["stop_drawdown"] = float(m.group(1)) / 100
    m = re.search(r"连续(?:亏损|止损)\s*(\d+)\s*(?:次|笔)", t)
    if m:
        rules["stop_losing_streak"] = int(m.group(1))
    m = re.search(r"标的[：:]\s*([^\n　]+)", t)
    if m:
        secs = re.findall(r"[A-Z0-9]{2,10}/[A-Z]{3,5}(?::PERP)?", m.group(1))
        if secs:
            rules["securities"] = secs
    if "现货" in t and "永续" not in t and "合约" not in t and "perp" not in low:
        rules["spot_only"] = True
    return rules


def _drawdown_path(points: list[dict[str, Any]]) -> list[tuple[int, float]]:
    peak, out = 0.0, []
    for p in points:
        v = float(p.get("v") or 0)
        peak = max(peak, v)
        out.append((int(p.get("t") or 0), (peak - v) / peak if peak > 0 else 0.0))
    return out


def _dd_at(path: list[tuple[int, float]], ts: int) -> float:
    cur = 0.0
    for t, d in path:
        if t > ts:
            break
        cur = d
    return cur


def _log_near(logs: list[dict[str, Any]], ts: int, window: int) -> str:
    best = ""
    for lg in logs:
        t = int(lg.get("ts") or 0)
        if ts - window <= t <= ts + window and lg.get("message"):
            best = str(lg["message"])
    return best[:120]


def rule_review(session: dict[str, Any], spec: str, *, bar_ms: int = 3_600_000) -> dict[str, Any]:
    rules = parse_rules(spec)
    fills = session.get("fills") or []
    logs = session.get("logs") or []
    eq = _drawdown_path(session.get("equity_points") or [])
    trades: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    per_day: dict[str, int] = {}
    streak = 0
    pos: dict[str, float] = {}

    def flag(level: str, text: str, f: dict[str, Any] | None = None, rule: str = "") -> None:
        flags.append(
            {
                "level": level,
                "text": text,
                "fill_id": (f or {}).get("fill_id", ""),
                "ts": (f or {}).get("ts"),
                "rule": rule,
            }
        )

    for f in fills:
        sec, side = f.get("security") or session.get("symbol") or "", f.get("side") or ""
        qty, price = float(f.get("amount") or 0), float(f.get("price") or 0)
        before = pos.get(sec, 0.0)
        after = before + (qty if side == "buy" else -qty)
        pos[sec] = after
        reason = REASON.get(f.get("reason") or "", f.get("reason") or "策略信号")
        pnl = float(f.get("realized_pnl") or 0)
        opening = abs(after) > abs(before) + 1e-12
        parts = [f"{SIDE.get(side, side)} {qty:g} {sec.split('/')[0]} @ {_px(price)}", f"原因：{reason}"]
        if not opening and pnl:
            parts.append(f"这笔平仓{'赚' if pnl > 0 else '亏'} {abs(pnl):,.2f}")
        if sec.upper().endswith(":PERP") and after < 0 and opening:
            parts.append("开/加空仓")
        log = _log_near(logs, int(f.get("ts") or 0), bar_ms)
        if log:
            parts.append(f"策略日志：{log}")
        if f.get("note"):
            parts.append(str(f["note"])[:80])
        trades.append(
            {
                "fill_id": f.get("fill_id", ""),
                "ts": f.get("ts"),
                "security": sec,
                "side": side,
                "qty": qty,
                "price": price,
                "explanation": "；".join(parts),
                "by": "rules",
            }
        )

        ts = int(f.get("ts") or 0)
        if f.get("reason") == "liquidation":
            flag("error", f"{sec} 被强平：保证金不足以覆盖亏损", f, "liquidation")
        if rules.get("long_only") and after < -1e-12:
            flag("error", f"说明书写的是只做多，但 {sec} 出现了空头仓位", f, "long_only")
        if rules.get("spot_only") and sec.upper().endswith(":PERP"):
            flag("warn", f"说明书只提到现货，但交易了永续合约 {sec}", f, "spot_only")
        if (
            rules.get("securities")
            and sec not in rules["securities"]
            and sec.replace(":PERP", "") not in rules["securities"]
        ):
            flag("warn", f"{sec} 不在说明书列出的标的里（{'、'.join(rules['securities'])}）", f, "securities")
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000)) if ts else ""
        per_day[day] = per_day.get(day, 0) + 1
        if rules.get("max_trades_per_day") and per_day[day] == rules["max_trades_per_day"] + 1:
            flag("warn", f"{day} 成交超过说明书写的每天最多 {rules['max_trades_per_day']} 笔", f, "max_trades_per_day")
        if opening and rules.get("stop_drawdown"):
            dd = _dd_at(eq, ts)
            if dd > rules["stop_drawdown"]:
                flag(
                    "error",
                    f"说明书说回撤超过 {rules['stop_drawdown']:.0%} 应该停，当时回撤 {dd:.1%} 仍在开新仓",
                    f,
                    "stop_drawdown",
                )
        if not opening and pnl:
            streak = streak + 1 if pnl < 0 else 0
        if opening and rules.get("stop_losing_streak") and streak >= rules["stop_losing_streak"]:
            flag(
                "error",
                f"说明书说连续亏损 {rules['stop_losing_streak']} 次应该停，已连亏 {streak} 次仍在开新仓",
                f,
                "stop_losing_streak",
            )

    port = session.get("portfolio") or {}
    equity = float(port.get("equity") or port.get("total_value") or 0)
    positions = port.get("positions") or []
    if isinstance(positions, dict):
        positions = [{"security": k, **(v if isinstance(v, dict) else {})} for k, v in positions.items()]
    for p in positions:
        lev = p.get("leverage")
        if lev and rules.get("max_leverage") and float(lev) > rules["max_leverage"] + 1e-9:
            flag(
                "error",
                f"{p.get('security')} 用了 {float(lev):g} 倍杠杆，说明书上限 {rules['max_leverage']:g} 倍",
                None,
                "max_leverage",
            )
        val = abs(float(p.get("value") or p.get("notional") or p.get("market_value") or 0))
        if rules.get("max_weight") and equity > 0 and val / equity > rules["max_weight"] * 1.02:
            flag(
                "warn",
                f"{p.get('security')} 仓位占权益 {val / equity:.0%}，说明书上限 {rules['max_weight']:.0%}",
                None,
                "max_weight",
            )
    demo = session.get("demo") or {}
    if demo.get("halted"):
        flag("error", f"模拟盘熔断中：{demo.get('halt_reason') or '原因见实验页'}", None, "halt")
    rej = [o for o in demo.get("orders") or [] if o.get("status") == "rejected"]
    if len(rej) >= 3:
        flag(
            "warn",
            f"最近有 {len(rej)} 笔委托被交易所拒绝，例如：{(rej[-1].get('message') or '')[:60]}",
            None,
            "rejections",
        )
    if session.get("errors"):
        flag("warn", f"策略运行报错 {session['errors']} 次", None, "errors")
    return {"rules": rules, "trades": trades, "flags": flags}


def llm_review(
    call: Callable[[str, str], Any], spec: str, trades: list[dict[str, Any]], *, model: str = ""
) -> dict[str, Any]:
    recent = trades[-20:]
    user = json.dumps(
        {
            "spec": (spec or "（没有说明书）")[:4000],
            "fills": [
                {k: t[k] for k in ("fill_id", "ts", "security", "side", "qty", "price", "explanation")} for t in recent
            ],
        },
        ensure_ascii=False,
    )
    resp = call(SYSTEM_PROMPT, user)
    text = getattr(resp, "content", None) or getattr(resp, "text", None) or str(resp)
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    return {
        "explanations": {
            e.get("fill_id"): str(e.get("text") or "")[:200]
            for e in data.get("explanations") or []
            if isinstance(e, dict)
        },
        "contradictions": [
            {"level": "warn", "text": str(c.get("text") or "")[:200], "fill_id": c.get("fill_id") or "", "rule": "llm"}
            for c in data.get("contradictions") or []
            if isinstance(c, dict) and c.get("text")
        ],
        "model": model or getattr(resp, "model", "") or "",
    }


def review(
    store: Any,
    session: dict[str, Any],
    spec: str,
    *,
    llm: Callable[[str, str], Any] | None = None,
    model: str = "",
    bar_ms: int = 3_600_000,
) -> dict[str, Any]:
    out = rule_review(session, spec, bar_ms=bar_ms)
    tagger: dict[str, Any] = {"kind": "rules"}
    if llm is not None and out["trades"]:
        try:
            got = llm_review(llm, spec, out["trades"], model=model)
            for t in out["trades"]:
                if got["explanations"].get(t["fill_id"]):
                    t["explanation_ai"] = got["explanations"][t["fill_id"]]
            out["flags"].extend(got["contradictions"])
            tagger = {"kind": "llm", "model": got["model"], "prompt_version": PROMPT_VERSION, "prompt_sha": PROMPT_SHA}
        except Exception as exc:  # 模型失败不影响规则层
            tagger = {"kind": "rules", "llm_error": f"{type(exc).__name__}: {exc}"[:200]}
    rec = {
        "id": session["id"],
        "session_id": session["id"],
        "strategy_id": session.get("strategy_id"),
        "strategy_name": session.get("strategy_name"),
        "execution": session.get("execution") or "paper",
        "has_spec": bool((spec or "").strip()),
        "rules": out["rules"],
        "trades": out["trades"][-200:],
        "flags": out["flags"][-100:],
        "tagger": tagger,
        "reviewed_at": int(time.time() * 1000),
        "fills_seen": len(session.get("fills") or []),
    }
    store.upsert(REVIEWS, rec)
    return rec


def brief_section(store: Any, running_ids: set[str] | None = None) -> dict[str, Any]:
    """给 AI 晨报：运行中实验的值守结论。"""
    rows = store.list(REVIEWS)
    if running_ids is not None:
        rows = [r for r in rows if r["id"] in running_ids]
    flagged = [
        {"name": r.get("strategy_name"), "session_id": r["id"], "level": f["level"], "text": f["text"]}
        for r in rows
        for f in (r.get("flags") or [])
        if f.get("level") in ("error", "warn")
    ]
    flagged.sort(key=lambda x: x["level"] != "error")
    return {
        "watched": len(rows),
        "flagged": flagged[:8],
        "clean": [r.get("strategy_name") for r in rows if not r.get("flags")][:6],
    }


__all__ = ["PROMPT_VERSION", "brief_section", "llm_review", "parse_rules", "review", "rule_review"]
