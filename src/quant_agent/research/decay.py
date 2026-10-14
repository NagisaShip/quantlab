"""策略衰减监测：拿运行中的表现对照回测重采样出的「正常区间」，跌出区间就标记衰减并建议降级。

做法
----
1. 回测画像：回测的日收益序列、每笔交易收益、日均交易笔数。
2. 正常区间：从回测日收益里按 3 天一块做块状重采样，拼出和实际运行同样天数的窗口（默认 2000 个），
   得到窗口收益、最大回撤的分布；从回测交易里按实际运行期间的笔数有放回抽样，得到胜率和平均每笔收益的分布。
3. 对照：实际运行的每个指标落在分布的哪个分位。收益、胜率、每笔收益越低越差，回撤越大越差。
4. 判定用序贯监测（CUSUM）：日收益和每笔交易收益只要持续低于回测均值就累积分数，偶尔差一下会被抵掉。
   衰减线是这样定的：从回测重采样出很多条「策略还正常」的运行路径，算每条路径在整个监测期（至少 14 天）
   里分数的最高点，取 95% 分位。所以正常策略在两周里被误标的概率约 5%，而真失效的策略分数会一路涨上去。
   - 衰减：分数越过衰减线；
   - 关注：分数越过 80% 分位，或任一指标落在正常区间最差的 5% 以内；
   - 正常：其余。运行不满 3 天时只显示「观察中」。

判定只给建议：降级（从晋级关卡退回一级）要人确认。一次判定只看这一个实验，不跨实验合并。
"""

from __future__ import annotations

import math
import random
import time
from typing import Any

DAY = 86_400_000
REPORTS = "decay_reports"
MIN_DAYS = 3.0
BLOCK = 3
SAMPLES = 2000
HORIZON_DAYS = 14
CUSUM_K = 0.25

STATUS_LABEL = {"warming": "观察中", "ok": "正常", "watch": "关注", "decay": "衰减"}


# ---------------------------------------------------------------- 画像
def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def profile(backtest: dict[str, Any]) -> dict[str, Any]:
    """回测记录 → 画像：日收益、每笔交易收益、日均交易笔数。"""
    daily = [r for r in (_f(d.get("return")) for d in backtest.get("daily") or []) if r is not None]
    if len(daily) < 10:
        daily = _daily_from_curve(backtest.get("timestamps") or [], backtest.get("equity_curve") or [])
    trades = [r for r in (_f(t.get("return")) for t in backtest.get("round_trips") or []) if r is not None]
    days = max(1, len(daily))
    return {
        "daily": daily,
        "trades": trades,
        "trades_per_day": len(trades) / days,
        "days": days,
        "backtest_id": backtest.get("id"),
    }


def _daily_from_curve(ts: list[Any], vals: list[Any]) -> list[float]:
    last: dict[int, float] = {}
    for t, v in zip(ts, vals, strict=True):
        fv = _f(v)
        if fv is not None and t is not None:
            last[int(t) // DAY] = fv
    days = sorted(last)
    return [last[b] / last[a] - 1 for a, b in zip(days, days[1:], strict=False) if last[a]]


def live_stats(points: list[dict[str, Any]], fills: list[dict[str, Any]]) -> dict[str, Any]:
    """实际运行：净值点 {t, v} + 成交（卖出带 realized_pnl）→ 天数、收益、最大回撤、交易统计。"""
    pts = [(int(p["t"]), float(p["v"])) for p in points if p.get("t") is not None and _f(p.get("v"))]
    pts.sort()
    if len(pts) < 2:
        return {"days": 0.0, "return": None, "max_drawdown": None, "trades": []}
    days = (pts[-1][0] - pts[0][0]) / DAY
    # 回撤按「每满一天取一个点」算，和回测日收益重采样的口径一致（盘中波动不算进去）
    t0 = pts[0][0]
    closes: dict[int, float] = {0: pts[0][1]}
    for t, v in pts:
        closes[(t - t0) // DAY + (1 if (t - t0) % DAY else 0)] = v
    peak, dd = pts[0][1], 0.0
    seq = [v for _, v in sorted(closes.items())]
    for v in seq:
        peak = max(peak, v)
        dd = max(dd, 1 - v / peak if peak else 0.0)
    daily = [b / a - 1 for a, b in zip(seq, seq[1:], strict=False) if a]
    trades = []
    for f in fills:
        pnl, notional = _f(f.get("realized_pnl")), _f(f.get("notional"))
        if f.get("side") == "sell" and pnl is not None and notional:
            cost = notional - pnl
            if cost > 0:
                trades.append(pnl / cost)
    return {"days": days, "return": pts[-1][1] / pts[0][1] - 1, "max_drawdown": dd, "trades": trades, "daily": daily}


# ---------------------------------------------------------------- 序贯监测（CUSUM）
def _moments(xs: list[float]) -> tuple[float, float]:
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / max(1, len(xs) - 1)) or 1e-9
    return mu, sd


def _cusum(xs: list[float], mu: float, sd: float) -> float:
    """单边 CUSUM：表现持续低于回测均值时累积，偶尔差一下会被 K 抵掉。"""
    c = 0.0
    for x in xs:
        c = max(0.0, c + (mu - x) / sd - CUSUM_K)
    return c


def score(prof: dict[str, Any], daily: list[float], trades: list[float]) -> float:
    s = _cusum(daily, *_moments(prof["daily"]))
    if prof["trades"] and len(prof["trades"]) >= 10:
        s += _cusum(trades, *_moments(prof["trades"]))
    return s


def thresholds(prof: dict[str, Any], days: float, *, samples: int = SAMPLES, seed: int = 11) -> dict[str, float]:
    """用回测重采样出「策略还正常」时的运行路径，算整段监测期里分数最大值的分布。

    阈值取它的 95% / 80% 分位：正常策略在整个监测期（至少 14 天）里被误标衰减的概率约 5%。
    """
    rng = random.Random(seed)
    horizon = max(HORIZON_DAYS, math.ceil(days))
    mu_d, sd_d = _moments(prof["daily"])
    use_t = bool(prof["trades"]) and len(prof["trades"]) >= 10
    mu_t, sd_t = _moments(prof["trades"]) if use_t else (0.0, 1.0)
    tpd = prof.get("trades_per_day") or 0.0
    peaks = []
    for _ in range(samples):
        path: list[float] = []
        while len(path) < horizon:
            i = rng.randrange(0, max(1, len(prof["daily"]) - BLOCK + 1))
            path.extend(prof["daily"][i : i + BLOCK])
        cd = ct = best = 0.0
        acc = 0.0
        for r in path[:horizon]:
            cd = max(0.0, cd + (mu_d - r) / sd_d - CUSUM_K)
            if use_t:
                acc += tpd
                while acc >= 1:
                    acc -= 1
                    ct = max(0.0, ct + (mu_t - rng.choice(prof["trades"])) / sd_t - CUSUM_K)
            best = max(best, cd + ct)
        peaks.append(best)
    peaks.sort()
    return {"decay": max(_q(peaks, 0.95) or 0.0, 1.0), "watch": max(_q(peaks, 0.80) or 0.0, 0.5), "horizon": horizon}


# ---------------------------------------------------------------- 正常区间
def _window(daily: list[float], n: int, rng: random.Random) -> tuple[float, float]:
    path: list[float] = []
    while len(path) < n:
        i = rng.randrange(0, max(1, len(daily) - BLOCK + 1))
        path.extend(daily[i : i + BLOCK])
    eq, peak, dd = 1.0, 1.0, 0.0
    for r in path[:n]:
        eq *= 1 + r
        peak = max(peak, eq)
        dd = max(dd, 1 - eq / peak)
    return eq - 1, dd


def bands(
    prof: dict[str, Any], days: float, n_trades: int, *, samples: int = SAMPLES, seed: int = 7
) -> dict[str, list[float]]:
    rng = random.Random(seed)
    n = max(1, round(days))
    rets, dds, wins, means = [], [], [], []
    for _ in range(samples):
        r, d = _window(prof["daily"], n, rng)
        rets.append(r)
        dds.append(d)
        if prof["trades"] and n_trades:
            pick = [rng.choice(prof["trades"]) for _ in range(n_trades)]
            wins.append(sum(1 for x in pick if x > 0) / n_trades)
            means.append(sum(pick) / n_trades)
    return {"return": sorted(rets), "max_drawdown": sorted(dds), "win_rate": sorted(wins), "trade_mean": sorted(means)}


def _pct(dist: list[float], x: float) -> float:
    """x 在分布里的分位（0~1，越小越靠下）。并列取中间。"""
    if not dist:
        return 0.5
    lo = sum(1 for d in dist if d < x)
    eq = sum(1 for d in dist if d == x)
    return (lo + eq / 2) / len(dist)


def _q(dist: list[float], p: float) -> float | None:
    if not dist:
        return None
    return dist[min(len(dist) - 1, max(0, int(p * len(dist))))]


# ---------------------------------------------------------------- 判定
def assess(prof: dict[str, Any], live: dict[str, Any], *, samples: int = SAMPLES) -> dict[str, Any]:
    """返回 {status, label, days, metrics: [{key, label, value, percentile, band, bad}], reasons}。"""
    days = float(live.get("days") or 0)
    out: dict[str, Any] = {"days": round(days, 2), "metrics": [], "reasons": []}
    if days < MIN_DAYS or live.get("return") is None or len(prof.get("daily") or []) < 10:
        why = "回测太短，没法估计正常区间" if len(prof.get("daily") or []) < 10 else f"运行满 {MIN_DAYS:g} 天后开始判定"
        return {**out, "status": "warming", "label": STATUS_LABEL["warming"], "reasons": [why]}
    trades = live.get("trades") or []
    n_tr = len(trades) if len(trades) >= 5 else 0
    b = bands(prof, days, n_tr, samples=samples)
    # 「差」的分位：越接近 0 越差（回撤反过来算）
    rows = [
        ("return", "区间收益", live["return"], _pct(b["return"], live["return"]), "pct"),
        ("max_drawdown", "最大回撤", live["max_drawdown"], 1 - _pct(b["max_drawdown"], live["max_drawdown"]), "pct"),
    ]
    if n_tr:
        wr = sum(1 for x in trades if x > 0) / n_tr
        mean = sum(trades) / n_tr
        rows += [
            ("win_rate", "胜率", wr, _pct(b["win_rate"], wr), "pct"),
            ("trade_mean", "平均每笔收益", mean, _pct(b["trade_mean"], mean), "pct"),
        ]
    for key, label, value, bad_p, _ in rows:
        dist = b[key]
        band = [_q(dist, 0.05), _q(dist, 0.5), _q(dist, 0.95)]
        out["metrics"].append(
            {"key": key, "label": label, "value": value, "badness": round(bad_p, 4), "band": band, "bad": bad_p <= 0.05}
        )
    sc = score(prof, live.get("daily") or [], trades)
    th = thresholds(prof, days, samples=samples)
    out["score"] = {"value": round(sc, 2), "watch": round(th["watch"], 2), "decay": round(th["decay"], 2)}
    if sc >= th["decay"]:
        status = "decay"
        out["reasons"].append(
            f"持续跑输回测的累积分数 {sc:.1f}，超过衰减线 {th['decay']:.1f}"
            f"（策略正常时整段 {th['horizon']} 天里越过这条线的概率约 5%）"
        )
    elif sc >= th["watch"] or any(r["badness"] <= 0.05 for r in out["metrics"]):
        status = "watch"
    else:
        status = "ok"
    for r in out["metrics"]:
        if r["badness"] <= 0.10:
            _lo, mid, _hi = r["band"]
            out["reasons"].append(
                f"{r['label']} {_fmt(r['key'], r['value'])}，回测同样长度的正常区间中位数 "
                f"{_fmt(r['key'], mid)}，比 {100 - r['badness'] * 100:.0f}% 的情形都差"
            )
    out.update(status=status, label=STATUS_LABEL[status], trades=len(trades))
    return out


def _fmt(key: str, v: Any) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.1f}%" if key != "trade_mean" else f"{v * 100:.2f}%"


# ---------------------------------------------------------------- 落库与扫描
def latest_backtest(store: Any, slug: str) -> dict[str, Any] | None:
    from ..store import Collections

    rows = [
        r
        for r in store.list(Collections.BACKTESTS)
        if (r.get("slug") or r.get("strategy_id")) == slug and r.get("status", "done") in ("done", "completed", "ok")
    ]
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    for r in rows[:5]:
        blob = store.get_blob(Collections.BACKTESTS, r["id"])
        if blob and (blob.get("daily") or blob.get("equity_curve")):
            return blob
    return None


def check_session(store: Any, session: dict[str, Any], *, backtest: dict[str, Any] | None = None) -> dict[str, Any]:
    """对一个运行中的实验做一次判定并记下来；从「非衰减」变成「衰减」时 changed=True。"""
    from . import graduation as GR

    slug = session.get("strategy_id") or ""
    bt = backtest or latest_backtest(store, slug)
    if bt is None:
        res: dict[str, Any] = {
            "status": "warming",
            "label": STATUS_LABEL["warming"],
            "reasons": ["这个策略还没有完整回测"],
            "metrics": [],
            "days": 0,
        }
    else:
        res = assess(profile(bt), live_stats(session.get("equity_points") or [], session.get("fills") or []))
    prev = store.get(REPORTS, session["id"]) or {}
    stage = (GR.record(store, slug) or {}).get("stage") or "backtest"
    idx = GR.STAGES.index(stage) if stage in GR.STAGES else 0
    suggest = GR.STAGES[idx - 1] if res["status"] == "decay" and idx > 0 else None
    rec = {
        "id": session["id"],
        "strategy_id": slug,
        "strategy_name": session.get("strategy_name"),
        "execution": session.get("execution") or "paper",
        "backtest_id": (bt or {}).get("id"),
        **res,
        "stage": stage,
        "suggest_demote_to": suggest,
        "first_decay_at": prev.get("first_decay_at") or (int(time.time() * 1000) if res["status"] == "decay" else None),
        "checked_at": int(time.time() * 1000),
        "history": [*(prev.get("history") or []), {"t": int(time.time() * 1000), "status": res["status"]}][-60:],
    }
    if res["status"] != "decay":
        rec["first_decay_at"] = None
    store.upsert(REPORTS, rec)
    return {**rec, "changed": res["status"] == "decay" and prev.get("status") != "decay"}


def reports(store: Any) -> list[dict[str, Any]]:
    rows = list(store.list(REPORTS))
    order = {"decay": 0, "watch": 1, "ok": 2, "warming": 3}
    rows.sort(key=lambda r: (order.get(r.get("status"), 9), -(r.get("checked_at") or 0)))
    return rows
