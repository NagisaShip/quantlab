"""策略体检：上线前的四项自动检查，对标 Freqtrade 的 lookahead-analysis / recursive-analysis，并多做两项。

1. 未来函数（lookahead）：在 6 个时间点把数据截断，**重新计算**（研究数据也按截断后的数据重算），
   比较截断点之前的每一个信号和 record() 指标。完整数据和截断数据在同一时刻做出不同决定，
   就说明它用到了截断点之后的数据。先把完整数据跑两遍：两遍结果不同说明策略本身不确定（用了随机数等），
   这时不下未来函数的结论。
2. 预热（recursive）：同一个计分起点，分别给 1/4、1/2、1、2 倍预热，看信号是否一致。
   EMA 这类递归指标、或 history() 取的根数超过预热时，预热不够会让前面一段信号不同。
3. 成本敏感性：手续费、滑点、价差都翻倍再跑一次。收益由正转负、或夏普掉一半以上，说明利润主要靠低成本假设。
4. 参数平台：每个数值参数上下各挪 10%、20%，看夏普是否还在。邻居一挪就塌的"孤峰"参数大概率是拟合出来的。

所有检查都在沙箱进程里执行用户代码；结论只有 ok / warn / bad / skip 四种，界面和毕业闸门按它判断。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..exchanges.base import Bar

VERDICTS = ("ok", "warn", "bad", "skip")
RANK = {"skip": 0, "ok": 1, "warn": 2, "bad": 3}
TITLES = {
    "lookahead": "未来函数",
    "warmup": "预热是否足够",
    "cost": "成本翻倍",
    "plateau": "参数平台",
}
CUT_FRACTIONS = (0.3, 0.45, 0.6, 0.72, 0.84, 0.95)
MAX_PLATEAU_PARAMS = 6
STEPS = (-0.2, -0.1, 0.1, 0.2)
RunFn = Callable[..., Any]


# --------------------------------------------------------------------------- 工具
def _timeline(bars: dict[str, list[Bar]]) -> list[int]:
    return sorted({b.close_time for s in bars.values() for b in s})


def _upto(bars: dict[str, list[Bar]], ts: int) -> dict[str, list[Bar]]:
    return {k: [b for b in v if b.close_time <= ts] for k, v in bars.items()}


def _from(bars: dict[str, list[Bar]], ts: int) -> dict[str, list[Bar]]:
    return {k: [b for b in v if b.close_time >= ts] for k, v in bars.items()}


def _round(v: Any) -> Any:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if not math.isfinite(f):
        return str(f)
    return float(f"{f:.10g}")


def signal_keys(result: Any, *, lo: int | None = None, hi: int | None = None) -> list[tuple]:
    out = []
    for s in result.signals:
        ts = int(s["decision_ts"])
        if (lo is not None and ts < lo) or (hi is not None and ts > hi):
            continue
        tgt = s.get("target") or {}
        out.append(
            (
                ts,
                s.get("instrument_id"),
                tgt.get("kind"),
                _round(tgt.get("value")),
                s.get("order_type"),
                _round(s.get("limit_price")),
            )
        )
    return out


def record_keys(result: Any, *, lo: int | None = None, hi: int | None = None) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for name, pts in (result.records or {}).items():
        out[name] = [
            (int(p["t"]), _round(p["v"])) for p in pts if (lo is None or p["t"] >= lo) and (hi is None or p["t"] <= hi)
        ]
    return out


def _first_diff(a: list[tuple], b: list[tuple]) -> tuple[int, Any, Any] | None:
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            return i, x, y
    return None


def _records_diff(a: dict[str, list[tuple]], b: dict[str, list[tuple]], rel: float = 1e-6) -> dict[str, Any] | None:
    for name in sorted(set(a) | set(b)):
        xa, xb = a.get(name, []), b.get(name, [])
        if len(xa) != len(xb):
            return {"record": name, "full": len(xa), "cut": len(xb), "what": "条数不同"}
        for (t1, v1), (t2, v2) in zip(xa, xb, strict=True):
            if t1 != t2:
                return {"record": name, "ts": min(t1, t2), "what": "时间不同"}
            if isinstance(v1, float) and isinstance(v2, float):
                if abs(v1 - v2) > rel * max(1.0, abs(v1), abs(v2)):
                    return {"record": name, "ts": t1, "full": v1, "cut": v2}
            elif v1 != v2:
                return {"record": name, "ts": t1, "full": v1, "cut": v2}
    return None


def _sig_view(k: tuple | None) -> dict[str, Any] | None:
    if k is None:
        return None
    return {"ts": k[0], "security": k[1], "kind": k[2], "value": k[3], "order_type": k[4], "limit_price": k[5]}


def _item(key: str, verdict: str, summary: str, **details: Any) -> dict[str, Any]:
    assert verdict in VERDICTS
    return {"id": key, "title": TITLES[key], "verdict": verdict, "summary": summary, "details": details}


# --------------------------------------------------------------------------- 1. 未来函数
def lookahead(
    run: RunFn,
    bars: dict[str, list[Bar]],
    *,
    warmup: int,
    research_at: Callable[[int], dict[str, Any] | None] | None = None,
    cuts: tuple[float, ...] = CUT_FRACTIONS,
) -> dict[str, Any]:
    """run(bars, research=...) -> LocalRunResult；research_at(ts) 返回按截断数据重算的研究数据。"""
    tl = _timeline(bars)
    if len(tl) < warmup + 40:
        return _item("lookahead", "skip", f"数据太短（{len(tl)} 根，预热 {warmup} 根），无法做截断对比")
    full = run(bars)
    again = run(bars)
    if signal_keys(full) != signal_keys(again) or record_keys(full) != record_keys(again):
        return _item(
            "lookahead",
            "warn",
            "同一份数据跑两遍结果不同：策略本身不确定（随机数、依赖运行时间等），"
            "未来函数检查无法下结论。先去掉不确定因素再体检。",
            deterministic=False,
        )
    lo = warmup + 20
    idx = sorted({min(len(tl) - 2, max(lo, int(len(tl) * f))) for f in cuts})
    checked, found = [], []
    for i in idx:
        ts = tl[i]
        cut_bars = _upto(bars, ts)
        res = run(cut_bars, research=research_at(ts) if research_at else None)
        a, b = signal_keys(full, hi=ts), signal_keys(res, hi=ts)
        d = _first_diff(a, b)
        rd = _records_diff(record_keys(full, hi=ts), record_keys(res, hi=ts))
        row: dict[str, Any] = {"cut_ts": ts, "signals_compared": len(a), "same": d is None and rd is None}
        if d is not None:
            row["signal"] = {"full": _sig_view(d[1]), "cut": _sig_view(d[2])}
        if rd is not None:
            row["record"] = rd
        checked.append(row)
        if not row["same"]:
            found.append(row)
    total = sum(r["signals_compared"] for r in checked)
    if found:
        f = found[0]
        where = f.get("signal", {}).get("full") or f.get("signal", {}).get("cut") or {}
        rec = f.get("record") or {}
        what = (
            f"信号 {where.get('security')} @ {where.get('ts')}"
            if where
            else f"指标 {rec.get('record')} @ {rec.get('ts')}"
        )
        return _item(
            "lookahead",
            "bad",
            f"有未来函数：{len(found)}/{len(checked)} 个截断点上，截断前的决定和完整数据不一样"
            f"（最早在 {what}）。这个策略的回测成绩不可信。",
            cuts=checked,
            signals_compared=total,
            deterministic=True,
        )
    if total == 0 and not any(record_keys(full).values()):
        return _item(
            "lookahead",
            "warn",
            "策略在检查区间里没有发出信号、也没有 record() 指标，无从比较。",
            cuts=checked,
            signals_compared=0,
            deterministic=True,
        )
    return _item(
        "lookahead",
        "ok",
        f"在 {len(checked)} 个截断点重新计算，{total} 个信号和全部 record() 指标都一致，没有发现未来函数。",
        cuts=checked,
        signals_compared=total,
        deterministic=True,
    )


# --------------------------------------------------------------------------- 2. 预热
def warmup_check(run: RunFn, bars: dict[str, list[Bar]], *, warmup: int, min_warmup: int = 1) -> dict[str, Any]:
    tl = _timeline(bars)
    w0 = max(int(warmup), 1)
    cands = sorted({max(min_warmup, w0 // 4), max(min_warmup, w0 // 2), max(min_warmup, w0), max(min_warmup, 2 * w0)})
    ref_w = cands[-1]
    if len(tl) < ref_w + 60:
        return _item("warmup", "skip", f"数据太短（{len(tl)} 根），至少要 {ref_w + 60} 根才能比较不同预热")
    start_ts = tl[ref_w]  # 计分起点：所有方案都从这里开始交易
    runs: dict[int, Any] = {}
    for w in cands:
        sub = _from(bars, tl[ref_w - w])
        runs[w] = run(sub, warmup=w)
    ref = signal_keys(runs[ref_w], lo=start_ts)
    ref_rec = record_keys(runs[ref_w], lo=start_ts)
    rows = []
    for w in cands:
        mine = signal_keys(runs[w], lo=start_ts)
        d = _first_diff(ref, mine)
        rd = _records_diff(ref_rec, record_keys(runs[w], lo=start_ts))
        same_n = sum(1 for x, y in zip(ref, mine, strict=False) if x == y)
        rows.append(
            {
                "warmup": w,
                "signals": len(mine),
                "same": d is None and rd is None,
                "agree": round(same_n / max(len(ref), 1), 4) if ref else (1.0 if not mine else 0.0),
                "first_diff_ts": (d[1] or d[2])[0] if d else (rd or {}).get("ts"),
            }
        )
    need = next((r["warmup"] for r in rows if all(x["same"] for x in rows if x["warmup"] >= r["warmup"])), ref_w)
    cur = next(r for r in rows if r["warmup"] == max(min_warmup, w0))
    if not ref and not any(ref_rec.values()):
        return _item("warmup", "warn", "计分区间里没有信号和 record() 指标，无从比较。", rows=rows, configured=w0)
    if cur["same"]:
        return _item(
            "warmup",
            "ok",
            f"当前预热 {w0} 根够用：再加长预热，信号和指标都不变"
            + (f"（最少 {need} 根就够）" if need < w0 else "")
            + "。",
            rows=rows,
            configured=w0,
            needed=need,
        )
    return _item(
        "warmup",
        "bad" if cur["agree"] < 0.95 else "warn",
        f"当前预热 {w0} 根不够：加长到 {ref_w} 根后，计分区间里有信号或指标变了"
        f"（一致率 {cur['agree']:.0%}）。递归指标（EMA 等）或 history() 取的根数超过了预热。"
        f"建议预热至少 {need if need > w0 else ref_w} 根。",
        rows=rows,
        configured=w0,
        needed=need,
    )


# --------------------------------------------------------------------------- 3. 成本
def cost_check(run: RunFn, bars: dict[str, list[Bar]], *, base: Any | None = None, mult: float = 2.0) -> dict[str, Any]:
    base = base or run(bars)
    hard = run(bars, cost_mult=mult)
    b, h = base.metrics, hard.metrics
    br, hr = float(b.get("total_return") or 0), float(h.get("total_return") or 0)
    bs, hs = b.get("sharpe"), h.get("sharpe")
    trades = int(b.get("trades") or 0)
    det = {
        "base": {"total_return": br, "sharpe": bs, "fees": b.get("total_fees"), "trades": trades},
        "stressed": {"total_return": hr, "sharpe": hs, "fees": h.get("total_fees")},
        "mult": mult,
    }
    if trades == 0:
        return _item("cost", "skip", "没有成交，成本不影响结果。", **det)
    if br > 0 and hr <= 0:
        return _item(
            "cost",
            "bad",
            f"手续费和滑点翻倍后，收益从 {br:.1%} 变成 {hr:.1%}：利润基本被成本吃掉，实盘成本稍高一点就会亏。",
            **det,
        )
    if bs is not None and hs is not None and bs > 0 and hs < bs * 0.5:
        return _item("cost", "warn", f"成本翻倍后夏普从 {bs:.2f} 掉到 {hs:.2f}，对成本很敏感；考虑降低换手。", **det)
    return _item(
        "cost",
        "ok",
        f"成本翻倍后收益 {br:.1%} → {hr:.1%}，夏普 "
        f"{'—' if bs is None else f'{bs:.2f}'} → {'—' if hs is None else f'{hs:.2f}'}，扛得住。",
        **det,
    )


# --------------------------------------------------------------------------- 4. 参数平台
def _neighbors(v: Any) -> list[tuple[float, Any]]:
    out, seen = [], {v}
    for f in STEPS:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            n = round(v * (1 + f))
            if n == v:
                n = v + (1 if f > 0 else -1) * (2 if abs(f) > 0.15 else 1)
        else:
            n = float(f"{v * (1 + f):.6g}")
        if n in seen or (v > 0 and n <= 0):
            continue
        seen.add(n)
        out.append((f, n))
    return out


def plateau(
    run: RunFn,
    bars: dict[str, list[Bar]],
    params: dict[str, Any],
    *,
    base: Any | None = None,
    objective: str = "sharpe",
) -> dict[str, Any]:
    nums = {k: v for k, v in params.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    if not nums:
        return _item("plateau", "skip", "策略没有声明数值参数（PARAMS），没有可挪动的参数。")
    nums = dict(list(nums.items())[:MAX_PLATEAU_PARAMS])
    base = base or run(bars)
    b0 = base.metrics.get(objective)
    if b0 is None or not math.isfinite(float(b0)):
        return _item("plateau", "skip", "基准参数的夏普算不出来（交易太少），无法比较。")
    b0 = float(b0)
    rows, worst10, worst20 = [], 1e9, 1e9
    for k, v in nums.items():
        for f, n in _neighbors(v):
            r = run(bars, params={**params, k: n})
            o = r.metrics.get(objective)
            o = float(o) if o is not None and math.isfinite(float(o)) else None
            rows.append({"param": k, "base": v, "value": n, "step": f, objective: o})
            ratio = (o / b0) if (o is not None and b0 > 0) else (0.0 if b0 > 0 else None)
            if ratio is not None:
                if abs(f) <= 0.1:
                    worst10 = min(worst10, ratio)
                else:
                    worst20 = min(worst20, ratio)
    det = {"objective": objective, "base_value": b0, "rows": rows, "params": list(nums)}
    if b0 <= 0:
        return _item(
            "plateau", "warn", f"当前参数的夏普是 {b0:.2f}（不赚钱），参数平台没有意义；先把策略做成正收益。", **det
        )
    if worst10 < 0.5:
        bad = min(
            (r for r in rows if abs(r["step"]) <= 0.1), key=lambda r: r[objective] if r[objective] is not None else -1e9
        )
        return _item(
            "plateau",
            "bad",
            f"孤峰参数：{bad['param']} 从 {bad['base']} 挪到 {bad['value']}（10%），"
            f"夏普就从 {b0:.2f} 掉到 {bad[objective] if bad[objective] is not None else '—'}。"
            "这组参数很可能是拟合出来的。",
            **det,
            worst_10=round(worst10, 3),
        )
    if worst20 < 0.5:
        return _item(
            "plateau",
            "warn",
            f"挪 10% 还稳，挪 20% 时夏普最低只剩 {worst20:.0%}：平台偏窄。",
            **det,
            worst_10=round(worst10, 3),
            worst_20=round(worst20, 3),
        )
    return _item(
        "plateau",
        "ok",
        f"每个参数上下挪 10%–20%，夏普最低还有原来的 {min(worst10, worst20):.0%}，是一片平台。",
        **det,
        worst_10=round(worst10, 3),
        worst_20=round(worst20 if worst20 < 1e8 else worst10, 3),
    )


# --------------------------------------------------------------------------- 汇总
def overall(items: list[dict[str, Any]]) -> str:
    worst = max((RANK[i["verdict"]] for i in items), default=0)
    return next(k for k, v in RANK.items() if v == worst) if worst else "skip"


def make_runner(
    source: str,
    *,
    name: str,
    spec: Any,
    lang: str = "python",
    research: dict[str, Any] | None = None,
    time_limit: float = 60.0,
) -> RunFn:
    """把 engine.research.execute 包成体检用的 run(bars, *, params, warmup, cost_mult, research)。"""
    from .research import execute

    def run(
        bars: dict[str, list[Bar]],
        *,
        params: dict[str, Any] | None = None,
        warmup: int | None = None,
        cost_mult: float = 1.0,
        research: dict[str, Any] | None = research,
    ) -> Any:
        s = spec
        if warmup is not None:
            s = replace(s, warmup_bars=int(warmup))
        if cost_mult != 1.0:
            # 撮合时统一放大最终成本；预先改默认费率会被策略 set_order_cost 覆盖。
            s = replace(s, cost_scale=s.cost_scale * cost_mult)
        return execute(
            source, name=name, bars=bars, spec=s, params=params, time_limit=time_limit, lang=lang, research=research
        )

    return run


def run_checkup(
    source: str,
    *,
    name: str,
    bars: dict[str, list[Bar]],
    spec: Any,
    lang: str = "python",
    params: dict[str, Any] | None = None,
    research: dict[str, Any] | None = None,
    research_by_cut: dict[int, dict[str, Any]] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """四项检查都跑一遍。research_by_cut：{截断时间: 按截断数据重算的研究数据}（策略用到 factor / predict 时）。"""
    from ..strategies import lang as L

    t0 = time.time()
    run = make_runner(source, name=name, spec=spec, lang=lang, research=research)
    module = L.load(lang, source, name=name, params=params or None)
    compiled = getattr(module, "compiled", None)
    min_w = int(getattr(compiled, "warmup_bars", 0) or 0) or 1
    warmup = max(int(spec.warmup_bars), min_w)
    declared = {**(L.declared_params(lang, source) or {}), **(params or {})}

    def research_at(ts: int) -> dict[str, Any] | None:
        if research_by_cut is None:
            return research
        return research_by_cut.get(ts, research)

    steps: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    base_box: dict[str, Any] = {}

    def base() -> Any:
        if "r" not in base_box:
            base_box["r"] = run(bars, params=params or None)
        return base_box["r"]

    steps.append(
        (
            "lookahead",
            lambda: lookahead(
                lambda b, **kw: run(b, params=params or None, **kw), bars, warmup=warmup, research_at=research_at
            ),
        )
    )
    steps.append(
        (
            "warmup",
            lambda: warmup_check(
                lambda b, **kw: run(b, params=params or None, **kw), bars, warmup=warmup, min_warmup=min_w
            ),
        )
    )
    steps.append(("cost", lambda: cost_check(lambda b, **kw: run(b, params=params or None, **kw), bars, base=base())))
    steps.append(("plateau", lambda: plateau(run, bars, declared, base=base())))
    items = []
    for i, (key, fn) in enumerate(steps):
        if progress:
            progress(i, len(steps), TITLES[key])
        try:
            items.append(fn())
        except Exception as exc:  # 一项出错不影响其他项
            items.append(_item(key, "skip", f"检查没能完成：{type(exc).__name__}: {str(exc)[:200]}"))
    if progress:
        progress(len(steps), len(steps), "完成")
    b = base()
    return {
        "items": items,
        "overall": overall(items),
        "elapsed_s": round(time.time() - t0, 1),
        "metrics": {
            k: b.metrics.get(k) for k in ("total_return", "sharpe", "max_drawdown", "trades", "win_rate", "total_fees")
        },
        "breakdown": b.metrics.get("breakdown"),
        "warmup": warmup,
        "params": declared,
    }


def cut_points(bars: dict[str, list[Bar]], warmup: int, cuts: tuple[float, ...] = CUT_FRACTIONS) -> list[int]:
    """与 lookahead() 用同样的规则算截断时间，给父进程预先按截断数据重算研究数据。"""
    tl = _timeline(bars)
    if len(tl) < warmup + 40:
        return []
    lo = warmup + 20
    return [tl[i] for i in sorted({min(len(tl) - 2, max(lo, int(len(tl) * f))) for f in cuts})]
