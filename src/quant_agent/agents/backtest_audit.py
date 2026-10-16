"""回测口径与结论核验：把「怎么算的」写清楚，把「能不能下这个结论」交给可复核的计算。

研究室的 Agent 在下结论前必须拿到这里的输出（原样附在工具结果里，Agent 只能解读、不能改写）：

1. **分段口径**（``segment_semantics``）：每一种分段 / 全区间回测都声明——是否连续复利、是否重置初始资金、
   跨段持仓怎么处理、交易按开仓还是平仓时间归段、各段收益能否复原全窗口收益。
   - ``independent``：每段单独回测，段首重置资金、空仓起步；段末未平仓按收盘估值、不跨段。各段收益连乘
     **不能**复原全区间收益（复利起点不同、跨段交易被切断）。
   - ``sliced``：一次全区间回测，按时间切段读净值。连续复利、不重置资金、持仓跨段延续；
     段收益 = 段末净值 / 段首净值 − 1（含未实现盈亏），各段连乘**应当**复原全区间收益，平台会核对。
2. **成本一致性**（``cost_audit``）：对比基础成本与压力成本两次回测的实际扣费——有效费率、每笔手续费、
   滑点、净值变化。对不上时标记异常，并禁止下「成本不是约束」这类结论。
3. **开发数据标记**（``tag_development``）：某个区间的结果被用来改参数 / 改代码后，它就是「已参与开发的数据」，
   之后不能再称为独立的样本外验证。
4. **代码与报告核对**（``code_report_check``）：移动止损是否单向移动、因子公式与入场条件是否一致、
   报告里的数据量、交易数、分段收益与平台记录是否一致。
5. **稳定性报告**（``stability_report``）：滚动窗口统计、收益分布、基准对照、参数敏感性。
"""

from __future__ import annotations

import ast
import math
import re
from statistics import mean, pstdev
from typing import Any

SEGMENT_MODES = ("independent", "sliced")
ATTRIBUTIONS = ("close", "open")


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


# ---------------------------------------------------------------------------
# 1. 分段口径
# ---------------------------------------------------------------------------
def segment_semantics(mode: str, attribution: str = "close") -> dict[str, Any]:
    """一种回测模式的计算口径（字段固定，报告与界面原样展示）。"""
    attribution = attribution if attribution in ATTRIBUTIONS else "close"
    when = "平仓时间" if attribution == "close" else "开仓时间"
    if mode == "sliced":
        return {
            "mode": "sliced",
            "label": "连续切片：一次全区间回测，按时间切段",
            "continuous_compounding": True,
            "reset_capital": False,
            "cross_segment_positions": "持仓跨段延续；段收益按段首、段末净值计算，含未实现盈亏",
            "trade_attribution": attribution,
            "trade_attribution_label": f"完整交易按{when}归入所在的段",
            "reconstructs_full": True,
            "reconstruct_note": "各段收益连乘应等于全区间收益（平台逐项核对）",
        }
    if mode == "full":
        return {
            "mode": "full",
            "label": "全区间：一次回测",
            "continuous_compounding": True,
            "reset_capital": False,
            "cross_segment_positions": "不分段",
            "trade_attribution": attribution,
            "trade_attribution_label": "全部交易计入同一区间；回测结束时未平仓的按收盘价估值，不计为完整交易",
            "reconstructs_full": True,
            "reconstruct_note": "—",
        }
    return {
        "mode": "independent",
        "label": "独立分段：每段单独回测",
        "continuous_compounding": False,
        "reset_capital": True,
        "cross_segment_positions": "每段空仓起步；段末未平仓按收盘价估值，不带入下一段（跨段交易被切断）",
        "trade_attribution": "close",
        "trade_attribution_label": "每段只统计段内开平的完整交易（段末未平仓不计）",
        "reconstructs_full": False,
        "reconstruct_note": "每段重置资金、跨段交易被切断，各段收益连乘不能复原全区间收益，只能比较各段的稳定性",
    }


def _bounds(start: int, end: int, segments: int) -> list[tuple[int, int]]:
    span = (end - start) // segments
    return [(start + k * span, end if k == segments - 1 else start + (k + 1) * span) for k in range(segments)]


def slice_full(result: dict[str, Any], segments: int, *, attribution: str = "close") -> dict[str, Any]:
    """把一次全区间回测按时间切段：段收益来自净值曲线，交易按开仓 / 平仓时间归段，并核对能否复原全区间收益。"""
    ts = [int(t) for t in result.get("timestamps") or []]
    eq = [_f(v) for v in result.get("equity_curve") or []]
    if len(ts) < segments * 20 or len(ts) != len(eq) or any(v is None for v in eq):
        return {"ok": False, "error": "净值曲线太短或不完整，不能切段"}
    attribution = attribution if attribution in ATTRIBUTIONS else "close"
    trips = result.get("round_trips") or []
    initial = _f(result.get("initial_cash")) or eq[0] or 1.0
    rows = []
    n = len(ts)
    cuts = [round(n * k / segments) for k in range(segments + 1)]
    for k in range(segments):
        i0, i1 = cuts[k], cuts[k + 1] - 1
        # 段首净值取上一段最后一根（第一段取初始资金），保证各段首尾相接、连乘可复原
        e0 = initial if k == 0 else eq[i0 - 1]
        e1 = eq[i1]
        a, b = ts[i0], ts[i1]
        peak, dd = e0, 0.0
        for v in eq[i0 : i1 + 1]:
            peak = max(peak, v)
            dd = max(dd, 1 - v / peak if peak else 0.0)
        key = "close_ts" if attribution == "close" else "open_ts"
        mine = [t for t in trips if t.get(key) is not None and a <= int(t[key]) <= b]
        carried_in = any(
            t.get("open_ts") is not None and t.get("close_ts") is not None and int(t["open_ts"]) < a <= int(t["close_ts"])
            for t in trips
        )
        wins = [t for t in mine if (_f(t.get("return")) or 0) > 0]
        rows.append(
            {
                "segment": k + 1,
                "start": a,
                "end": b,
                "ok": True,
                "equity_start": round(e0, 2),
                "equity_end": round(e1, 2),
                "metrics": {
                    "total_return": e1 / e0 - 1 if e0 else None,
                    "max_drawdown": dd,
                    "trades": len(mine),
                    "win_rate": len(wins) / len(mine) if mine else None,
                },
                "carried_position_in": carried_in,
            }
        )
    return {"ok": True, "segments": rows, "reconcile": reconcile(rows, result.get("metrics") or {}, "sliced", trips)}


def reconcile(
    rows: list[dict[str, Any]], full_metrics: dict[str, Any], mode: str, trips: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """各段能否复原全区间：收益连乘 vs 全区间总收益；交易数之和 vs 全区间交易数。"""
    rets = [_f((r.get("metrics") or {}).get("total_return")) for r in rows if r.get("ok")]
    full = _f(full_metrics.get("total_return"))
    chained = math.prod(1 + r for r in rets if r is not None) - 1 if rets and None not in rets else None
    out: dict[str, Any] = {"chained_return": chained, "full_return": full, "issues": []}
    trades = [int((r.get("metrics") or {}).get("trades") or 0) for r in rows if r.get("ok")]
    full_trades = full_metrics.get("trades")
    if trips is not None:
        full_trades = len(trips)
    out["segment_trades"] = sum(trades)
    out["full_trades"] = full_trades
    if mode == "sliced":
        if chained is not None and full is not None:
            gap = abs((1 + chained) - (1 + full))
            out["gap"] = gap
            out["reconstructs"] = gap < 1e-6 + 1e-4 * abs(1 + full)
            if not out["reconstructs"]:
                out["issues"].append(
                    f"各段收益连乘 {chained:.4%} 与全区间收益 {full:.4%} 对不上（差 {gap:.4%}）：切段或净值数据有问题"
                )
        if full_trades is not None and sum(trades) != int(full_trades):
            out["issues"].append(
                f"各段交易数之和 {sum(trades)} ≠ 全区间完整交易 {full_trades}：有交易没有归入任何一段（例如段边界上的交易）"
            )
    else:
        out["reconstructs"] = False
        if chained is not None and full is not None:
            out["gap"] = abs(chained - full)
            out["issues"].append(
                f"独立分段口径：各段连乘 {chained:.4%} 与全区间 {full:.4%} 不可比（每段重置资金、跨段交易被切断），"
                "不要用分段收益推算全区间收益"
            )
    return out


# ---------------------------------------------------------------------------
# 2. 成本一致性
# ---------------------------------------------------------------------------
def _fill_stats(result: dict[str, Any]) -> dict[str, Any]:
    fills = result.get("fills") or []
    notional = fee = slip_w = 0.0
    n = 0
    for x in fills:
        q = _f(x.get("notional"))
        fv = _f(x.get("fee_value") if x.get("fee_value") is not None else x.get("fee"))
        if q is None or fv is None:
            continue
        notional += abs(q)
        fee += fv
        n += 1
        px, mk = _f(x.get("price")), _f(x.get("market_price"))
        if px and mk:
            slip_w += abs(px / mk - 1) * abs(q)
    m = result.get("metrics") or {}
    return {
        "fills": n,
        "notional": notional,
        "fees": fee,
        "fees_reported": _f(m.get("total_fees")),
        "fee_rate": fee / notional if notional else None,
        "slippage_rate": slip_w / notional if notional else None,
        "final_equity": _f(m.get("final_equity")),
        "total_return": _f(m.get("total_return")),
        "trades": m.get("trades"),
        "fills_total": result.get("fills_total"),
    }


def cost_audit(base: dict[str, Any], stressed: dict[str, Any], scale: float, *, tol: float = 0.12) -> dict[str, Any]:
    """对比基础成本与压力成本两次回测的实际扣费明细与净值变化。

    返回 {consistent, checks, anomalies, conclusion_allowed, base, stressed}。consistent=False 时
    ``conclusion_allowed["cost_not_binding"]`` 一定是 False：不能据此说「成本不是约束」。
    """
    b, s = _fill_stats(base), _fill_stats(stressed)
    checks: list[dict[str, Any]] = []
    anomalies: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            anomalies.append(f"{name}：{detail}")

    # 明细合计与汇总一致
    for tag, st in (("基础", b), ("压力", s)):
        rep, own = st["fees_reported"], st["fees"]
        if rep is not None and st["fills"]:
            ok = abs(rep - own) <= max(0.01, 0.005 * abs(rep))
            check(f"{tag}回测手续费明细合计", ok, f"逐笔合计 {own:.2f}，汇总 {rep:.2f}")
        if st["fills_total"] is not None and st["fills"] < int(st["fills_total"]):
            check(f"{tag}回测成交明细完整", False, f"只拿到 {st['fills']} / {st['fills_total']} 笔成交明细，无法逐笔核对")
    # 有效费率按倍数放大
    if b["fee_rate"] and s["fee_rate"]:
        ratio = s["fee_rate"] / b["fee_rate"]
        check(
            "有效费率放大倍数",
            abs(ratio - scale) <= tol * scale,
            f"费率 {b['fee_rate']:.5%} → {s['fee_rate']:.5%}，放大 {ratio:.2f}×，应为 {scale:g}×",
        )
    elif scale != 1:
        check("有效费率放大倍数", False, "有一次回测没有成交或缺成交额，无法核对费率")
    # 滑点（成交价相对行情价的偏离）放大
    if b["slippage_rate"] and s["slippage_rate"]:
        ratio = s["slippage_rate"] / b["slippage_rate"]
        check(
            "滑点放大倍数",
            abs(ratio - scale) <= max(tol * scale, 0.25),
            f"成交偏离 {b['slippage_rate'] * 1e4:.2f} → {s['slippage_rate'] * 1e4:.2f} bps，放大 {ratio:.2f}×，应约为 {scale:g}×",
        )
    # 净值变化方向与幅度：成本更高，期末净值不应更高；交易相同时，净值差应接近多扣的成本
    if b["final_equity"] is not None and s["final_equity"] is not None and scale > 1:
        d_eq = b["final_equity"] - s["final_equity"]
        extra = (s["fees"] - b["fees"]) + ((s["slippage_rate"] or 0) - (b["slippage_rate"] or 0)) * s["notional"]
        if s["final_equity"] > b["final_equity"] * 1.0001:
            check("净值随成本下降", False, f"成本放大后期末净值反而更高（{b['final_equity']:.2f} → {s['final_equity']:.2f}）")
        elif b["trades"] == s["trades"] and extra > 0:
            ok = abs(d_eq - extra) <= max(0.35 * extra, 0.002 * abs(b["final_equity"]))
            check("净值变化与多扣成本一致", ok, f"期末净值少 {d_eq:.2f}，按明细多扣成本约 {extra:.2f}")
        else:
            checks.append(
                {"name": "净值变化", "ok": True, "detail": f"交易笔数 {b['trades']}→{s['trades']}，路径不同，净值差 {d_eq:.2f} 只作参考"}
            )
    consistent = not anomalies
    drop = (b["total_return"] or 0) - (s["total_return"] or 0) if b["total_return"] is not None and s["total_return"] is not None else None
    return {
        "scale": scale,
        "consistent": consistent,
        "checks": checks,
        "anomalies": anomalies,
        "return_drop": drop,
        "conclusion_allowed": {
            # 只有扣费对得上、且收益没有明显下降时才允许说「成本不是约束」
            "cost_not_binding": bool(consistent and drop is not None and drop < 0.2 * abs(b["total_return"] or 0) + 0.005),
        },
        "rule": (
            "成本核对未通过：报告必须标记「成本口径异常」，不得下「成本不是约束」的结论"
            if not consistent
            else "成本核对通过"
        ),
        "base": {k: v for k, v in b.items()},
        "stressed": {k: v for k, v in s.items()},
    }


# ---------------------------------------------------------------------------
# 3. 开发数据标记
# ---------------------------------------------------------------------------
def overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def tag_development(project: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给每段标记数据角色：看过它的结果之后又改过代码 / 参数的区间，是「已参与开发」的数据。"""
    seen = project.get("seen_ranges") or []
    cur = int(project.get("current_v") or 0)
    out = []
    for r in segments:
        a, b = int(r.get("start") or 0), int(r.get("end") or 0)
        used = [x for x in seen if int(x.get("v") or 0) < cur and overlaps(a, b, int(x["start"]), int(x["end"]))]
        out.append(
            {
                **r,
                "data_role": "development" if used else "unseen",
                "data_role_label": (
                    f"已参与开发（v{min(int(x['v']) for x in used)} 起看过这段的结果后又改了代码）"
                    if used
                    else "开发中未见过（仍不是正式样本外：同一项目内只能算探索性检查）"
                ),
            }
        )
    return out


def record_seen(project: dict[str, Any], start: int, end: int, source: str) -> list[dict[str, Any]]:
    """记下「当前版本」看过哪个区间的结果；之后提交新版本时，这些区间就算参与了开发。"""
    seen = list(project.get("seen_ranges") or [])
    v = int(project.get("current_v") or 0)
    if not any(int(x.get("v") or 0) == v and int(x["start"]) == start and int(x["end"]) == end for x in seen):
        seen.append({"v": v, "start": int(start), "end": int(end), "source": source})
    return seen[-200:]


# ---------------------------------------------------------------------------
# 4. 代码与报告核对
# ---------------------------------------------------------------------------
_TRAIL = re.compile(r"(trail|trailing|移动止损|stop)", re.I)


def trailing_stop_check(code: str) -> list[dict[str, Any]]:
    """找移动止损变量的赋值：除了开仓时的初始化，更新时必须是单向的（多头只升 max、空头只降 min）。"""
    issues: list[dict[str, Any]] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return issues
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AugAssign):
            targets, value = [node.target], node.value
        for t in targets:
            name = _target_name(t)
            if not name or not _TRAIL.search(name) or "trail" not in name.lower():
                continue
            src = ast.unparse(value) if value is not None else ""
            uses_self = name.split(".")[-1].split("[")[0] in src
            monotone = bool(re.search(r"\b(max|min)\s*\(", src))
            if isinstance(node, ast.AugAssign) or (uses_self and not monotone):
                issues.append(
                    {
                        "line": getattr(node, "lineno", 0),
                        "variable": name,
                        "problem": f"第 {getattr(node, 'lineno', 0)} 行更新 {name} 没有用 max()/min() 限定方向，移动止损可能双向移动",
                    }
                )
    return issues


def _target_name(t: ast.AST) -> str:
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return f"{_target_name(t.value)}.{t.attr}"
    if isinstance(t, ast.Subscript):
        key = t.slice
        k = key.value if isinstance(key, ast.Constant) else ""
        return f"{_target_name(t.value)}[{k}]"
    return ""


_FIELD_ALIASES = {
    "funding": ("funding", "资金费"),
    "volume": ("volume", "成交量", "vol"),
    "open_interest": ("open_interest", "oi", "持仓量"),
    "basis": ("basis", "基差"),
    "close": ("close", "收盘"),
    "high": ("high",),
    "low": ("low",),
}


def factor_entry_check(factor_expr: str, code: str) -> list[str]:
    """因子公式里用到的数据字段，策略入场逻辑里也应该用到；否则报告里「按因子入场」的说法站不住。"""
    if not factor_expr:
        return []
    fe, cd = factor_expr.lower(), code.lower()
    issues = []
    for field, words in _FIELD_ALIASES.items():
        if field in ("close", "high", "low"):
            continue
        if any(w in fe for w in words) and not any(w in cd for w in words):
            issues.append(f"因子公式用到「{field}」，但策略代码里没有读取它：入场条件与因子定义不一致")
    win_f = set(re.findall(r"\b(\d{1,3})\b", fe))
    win_c = set(re.findall(r"\b(\d{1,3})\b", cd))
    missing = sorted(w for w in win_f if w not in win_c and int(w) > 2)
    if missing:
        issues.append(f"因子公式里的窗口 {', '.join(missing)} 在策略代码里找不到：确认入场用的是同一个公式")
    return issues


_NUM_PATTERNS = {
    "trades": [r"(\d+)\s*笔(?:完整)?交易", r"交易\s*(\d+)\s*笔", r"(\d+)\s*trades\b"],
    "bars": [r"(\d[\d,]*)\s*根\s*K\s*线", r"(\d[\d,]*)\s*bars\b"],
}


def report_numbers_check(text: str, facts: dict[str, Any]) -> list[str]:
    """报告正文里的交易数、K 线数与平台记录对不上时列出来。facts: {trades, bars, segments: [{segment, total_return}]}"""
    issues = []
    for key, pats in _NUM_PATTERNS.items():
        truth = facts.get(key)
        if truth is None:
            continue
        valid = truth if isinstance(truth, (list, set, tuple)) else [truth]
        valid_set = {int(v) for v in valid if v is not None}
        for p in pats:
            for m in re.finditer(p, text, re.I):
                n = int(m.group(1).replace(",", ""))
                if valid_set and n not in valid_set:
                    label = "交易数" if key == "trades" else "K 线数"
                    issues.append(f"报告写「{m.group(0)}」，平台记录的{label}是 {'/'.join(str(v) for v in sorted(valid_set))}")
    for seg in facts.get("segments") or []:
        r = _f(seg.get("total_return"))
        if r is None:
            continue
        for m in re.finditer(rf"第\s*{seg['segment']}\s*段[^。\n]{{0,24}}?(-?\d+(?:\.\d+)?)\s*%", text):
            said = float(m.group(1)) / 100
            if abs(said - r) > 0.0051:
                issues.append(f"报告写第 {seg['segment']} 段收益 {said:.2%}，平台计算为 {r:.2%}")
    return issues


def code_report_check(
    *, code: str, report: str = "", factor_expr: str = "", facts: dict[str, Any] | None = None
) -> dict[str, Any]:
    trailing = trailing_stop_check(code or "")
    factor = factor_entry_check(factor_expr, code or "")
    numbers = report_numbers_check(report or "", facts or {}) if report else []
    issues = [t["problem"] for t in trailing] + factor + numbers
    return {"ok": not issues, "issues": issues, "trailing_stop": trailing, "factor_vs_entry": factor, "numbers": numbers}


# ---------------------------------------------------------------------------
# 5. 稳定性报告
# ---------------------------------------------------------------------------
def _returns(eq: list[float]) -> list[float]:
    return [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1]]


def _sharpe(r: list[float], per_year: float) -> float | None:
    if len(r) < 5:
        return None
    sd = pstdev(r)
    return mean(r) / sd * math.sqrt(per_year) if sd > 0 else None


from ..engine.metrics import BARS_PER_YEAR  # noqa: E402  与回测指标同一张表（支持秒级 / 毫秒级周期）


def stability_report(result: dict[str, Any], *, windows: int = 6) -> dict[str, Any]:
    """滚动窗口夏普 / 收益 / 回撤、收益分布、与基准的对照。参数敏感性由调用方另外补上。"""
    eq = [v for v in (_f(x) for x in result.get("equity_curve") or []) if v is not None]
    bench = [v for v in (_f(x) for x in result.get("benchmark_curve") or []) if v is not None]
    ts = [int(t) for t in result.get("timestamps") or []]
    tf = result.get("timeframe") or "1h"
    per_year = BARS_PER_YEAR.get(tf, 8760)
    if len(eq) < windows * 20:
        return {"ok": False, "error": f"净值点太少（{len(eq)}），不足以做 {windows} 个滚动窗口"}
    r = _returns(eq)
    rb = _returns(bench) if len(bench) == len(eq) else []
    size = len(r) // windows
    rolling = []
    for k in range(windows):
        seg = r[k * size : (k + 1) * size]
        sb = rb[k * size : (k + 1) * size] if rb else []
        ret = math.prod(1 + x for x in seg) - 1
        bret = math.prod(1 + x for x in sb) - 1 if sb else None
        rolling.append(
            {
                "window": k + 1,
                "start": ts[k * size] if ts else None,
                "end": ts[min(len(ts) - 1, (k + 1) * size)] if ts else None,
                "return": ret,
                "benchmark_return": bret,
                "excess": (1 + ret) / (1 + bret) - 1 if bret is not None and bret > -1 else None,
                "sharpe": _sharpe(seg, per_year),
            }
        )
    srt = sorted(r)

    def q(p: float) -> float:
        return srt[min(len(srt) - 1, max(0, int(p * (len(srt) - 1))))]

    mu, sd = mean(r), pstdev(r)
    skew = sum((x - mu) ** 3 for x in r) / len(r) / sd**3 if sd > 0 else None
    kurt = sum((x - mu) ** 4 for x in r) / len(r) / sd**4 - 3 if sd > 0 else None
    positive = [w for w in rolling if w["return"] > 0]
    beat = [w for w in rolling if w["excess"] is not None and w["excess"] > 0]
    sharpes = [w["sharpe"] for w in rolling if w["sharpe"] is not None]
    top = sorted(r, reverse=True)
    top_share = sum(top[: max(1, len(r) // 20)]) / sum(x for x in r if x > 0) if any(x > 0 for x in r) else None
    flags = []
    if len(positive) < len(rolling) / 2:
        flags.append(f"只有 {len(positive)}/{len(rolling)} 个滚动窗口赚钱：收益集中在少数时段")
    if rb and len(beat) < len(rolling) / 2:
        flags.append(f"只有 {len(beat)}/{len(rolling)} 个窗口跑赢基准")
    if top_share is not None and top_share > 0.6:
        flags.append(f"最好的 5% 根 K 线贡献了 {top_share:.0%} 的正收益：结果依赖少数极端行情")
    if sharpes and max(sharpes) - min(sharpes) > 3:
        flags.append("各窗口夏普差异很大（>3）：表现不稳定")
    return {
        "ok": True,
        "windows": rolling,
        "distribution": {
            "bars": len(r),
            "mean": mu,
            "stdev": sd,
            "skew": skew,
            "excess_kurtosis": kurt,
            "p01": q(0.01),
            "p05": q(0.05),
            "p50": q(0.5),
            "p95": q(0.95),
            "p99": q(0.99),
            "worst": srt[0],
            "best": srt[-1],
            "top5pct_share_of_gains": top_share,
        },
        "summary": {
            "positive_windows": len(positive),
            "beat_benchmark_windows": len(beat) if rb else None,
            "sharpe_range": [min(sharpes), max(sharpes)] if sharpes else None,
        },
        "flags": flags,
    }


def sensitivity_grid(params: dict[str, Any], *, step: float = 0.2, limit: int = 6) -> list[tuple[str, Any]]:
    """数值参数各上下挪 step（整数参数至少挪 1），最多 limit 个参数。"""
    out: list[tuple[str, Any]] = []
    for k, v in list(params.items()):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
            continue
        for sign in (-1, 1):
            if isinstance(v, int):
                nv: Any = v + sign * max(1, round(abs(v) * step))
                if nv <= 0:
                    continue
            else:
                nv = round(v * (1 + sign * step), 6)
            out.append((k, nv))
        if len({x[0] for x in out}) >= limit:
            break
    return out


def sensitivity_summary(base: float | None, rows: list[dict[str, Any]], metric: str = "sharpe") -> dict[str, Any]:
    vals = [_f(r.get(metric)) for r in rows]
    ok = [v for v in vals if v is not None]
    if base is None or not ok:
        return {"ok": False, "note": "没有足够的结果评估参数敏感性"}
    worst = min(ok)
    drop = base - worst
    fragile = drop > max(0.5, 0.5 * abs(base))
    return {
        "ok": True,
        "metric": metric,
        "base": base,
        "worst": worst,
        "best": max(ok),
        "fragile": fragile,
        "note": (
            f"参数上下挪 20% 后{metric}最差降到 {worst:.2f}（基准 {base:.2f}）：参数孤峰，结果可能过拟合"
            if fragile
            else f"参数上下挪 20% 后{metric}在 {worst:.2f}–{max(ok):.2f}，相对稳定"
        ),
    }


__all__ = [
    "SEGMENT_MODES",
    "code_report_check",
    "cost_audit",
    "reconcile",
    "record_seen",
    "segment_semantics",
    "sensitivity_grid",
    "sensitivity_summary",
    "slice_full",
    "stability_report",
    "tag_development",
]
