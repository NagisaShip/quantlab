"""因子评估：IC / RankIC、衰减、分组收益、多空净值、换手、分状态表现、样本外保留、库内相关与入库检查。

口径：
- 因子在第 t 根 K 线收盘时计算，只用到 t 及以前的数据；预测目标是从 t 收盘到 t+h 收盘的收益。
- 标的数 ≥ 4 用**截面**口径（同一时刻比较不同币），否则用**时序**口径（同一个币不同时刻）。
- 持有期 h > 1 时相邻样本重叠，t 值按每 h 期取一个样本计算，避免高估显著性。
- 多空组合每 h 期调仓一次，按 `cost_bps`（单边）扣除换手成本。
- 显著性门槛按已做过的试验次数做 Bonferroni 修正：试得越多，要求越高。
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np

from ..agents.lab_goal import TF_MS
from .factor_expr import Node, _quiet, evaluate, max_lookback, parse
from .panel import Panel

YEAR_MS = 365 * 86_400_000
ADMISSION = {"max_corr": 0.7, "oos_retention": 0.5, "coverage": 0.8, "turnover": 0.8, "min_is_ic": 0.01}


def _rank_rows(x: np.ndarray) -> np.ndarray:
    """逐行排名（NaN 保持 NaN），并列取平均。整行向量化，只有出现并列的行才逐行处理。"""
    x = np.asarray(x, dtype=float)
    nan = np.isnan(x)
    filled = np.where(nan, np.inf, x)
    order = np.argsort(filled, axis=1, kind="mergesort")
    ranks = np.empty_like(filled)
    rows = np.arange(x.shape[0])[:, None]
    ranks[rows, order] = np.arange(x.shape[1], dtype=float)[None, :]
    ranks[nan] = np.nan
    srt = np.take_along_axis(filled, order, axis=1)
    ties = np.any((srt[:, 1:] == srt[:, :-1]) & np.isfinite(srt[:, 1:]), axis=1)
    for t in np.where(ties)[0]:
        ok = ~nan[t]
        vals = x[t][ok]
        r = ranks[t][ok]
        _uniq, inv = np.unique(vals, return_inverse=True)
        ranks[t, ok] = (np.bincount(inv, weights=r) / np.bincount(inv))[inv]
    return ranks


def _row_corr(a: np.ndarray, b: np.ndarray, min_n: int = 3) -> np.ndarray:
    """逐行 Pearson 相关（成对剔除 NaN），返回 [T]，样本不足为 NaN。"""
    mask = ~(np.isnan(a) | np.isnan(b))
    n = mask.sum(axis=1).astype(float)
    with np.errstate(all="ignore"):
        x = np.where(mask, a, 0.0)
        y = np.where(mask, b, 0.0)
        mx = x.sum(axis=1) / n
        my = y.sum(axis=1) / n
        dx = np.where(mask, a - mx[:, None], 0.0)
        dy = np.where(mask, b - my[:, None], 0.0)
        cov = (dx * dy).sum(axis=1)
        out = cov / np.sqrt((dx * dx).sum(axis=1) * (dy * dy).sum(axis=1))
    out[(n < min_n) | ~np.isfinite(out)] = np.nan
    return out


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3:
        return None
    x, y = a[ok], b[ok]
    sx, sy = x.std(), y.std()
    if sx == 0 or sy == 0:
        return None
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy))


def _spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 3:
        return None
    ra = _rank_rows(a[ok][None, :])[0]
    rb = _rank_rows(b[ok][None, :])[0]
    return _corr(ra, rb)


def forward_returns(close: np.ndarray, h: int) -> np.ndarray:
    out = np.full_like(close, np.nan)
    if h < close.shape[0]:
        with np.errstate(all="ignore"):
            out[:-h] = close[h:] / close[:-h] - 1
    return out


def _ic_series(
    F: np.ndarray, R: np.ndarray, *, cross: bool, block: int = 60
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (times_index, ic, rank_ic)。截面：每期一个；时序：每 block 个样本（跨标的合并）一个。"""
    if cross:
        c = _row_corr(F, R)
        rc = _row_corr(_rank_rows(np.where(np.isnan(R), np.nan, F)), _rank_rows(np.where(np.isnan(F), np.nan, R)))
        ok = ~(np.isnan(c) | np.isnan(rc))
        idx = np.where(ok)[0]
        return idx.astype(int), c[ok], rc[ok]
    idx, ic, ric = [], [], []
    T = F.shape[0]
    for lo in range(0, T - block + 1, block):
        a, b = F[lo : lo + block].ravel(), R[lo : lo + block].ravel()
        c = _corr(a, b)
        rc = _spearman(a, b)
        if c is None or rc is None:
            continue
        idx.append(lo + block - 1)
        ic.append(c)
        ric.append(rc)
    return np.array(idx, dtype=int), np.array(ic), np.array(ric)


def _stats(ic: np.ndarray, step: int) -> dict[str, float | None]:
    if len(ic) == 0:
        return {"mean": None, "std": None, "ir": None, "t": None, "win": None, "n": 0}
    mean = float(np.mean(ic))
    std = float(np.std(ic, ddof=1)) if len(ic) > 1 else 0.0
    sampled = ic[:: max(1, step)]
    s_std = float(np.std(sampled, ddof=1)) if len(sampled) > 1 else 0.0
    t = float(np.mean(sampled) / s_std * math.sqrt(len(sampled))) if s_std > 0 else None
    return {
        "mean": mean,
        "std": std,
        "ir": mean / std if std > 0 else None,
        "t": t,
        "win": float(np.mean(ic > 0)),
        "n": len(ic),
    }


def _groups_cross(
    F: np.ndarray, R: np.ndarray, n_groups: int, h: int
) -> tuple[list[np.ndarray], np.ndarray, list[float]]:
    """每 h 期按因子分组调仓。返回 (各组每期收益序列, 多空权重序列[期, N], 调仓时刻)。"""
    T, N = F.shape
    rets = [[] for _ in range(n_groups)]
    weights, stamps = [], []
    RF = _rank_rows(F)
    for t in range(0, T, h):
        ok = ~(np.isnan(F[t]) | np.isnan(R[t]))
        k = int(ok.sum())
        if k < n_groups:
            continue
        q = np.floor(RF[t][ok] / max(k - 1, 1) * n_groups - 1e-9).clip(0, n_groups - 1).astype(int)
        r = R[t][ok]
        for g in range(n_groups):
            sel = r[q == g]
            rets[g].append(float(sel.mean()) if len(sel) else 0.0)
        w = np.zeros(N)
        cols = np.where(ok)[0]
        top, bot = cols[q == n_groups - 1], cols[q == 0]
        if len(top) and len(bot):
            w[top] = 0.5 / len(top)
            w[bot] = -0.5 / len(bot)
        weights.append(w)
        stamps.append(t)
    return [np.array(x) for x in rets], np.array(weights) if weights else np.zeros((0, N)), stamps


def _ts_positions(
    F: np.ndarray, R: np.ndarray, h: int, window: int = 100
) -> tuple[np.ndarray, np.ndarray, list[int], list[np.ndarray]]:
    """时序口径：每个币按因子的滚动 z 值方向做多/做空，等权合成。"""
    T, N = F.shape
    weights, stamps, port = [], [], []
    groups: list[list[float]] = [[], [], []]
    for t in range(0, T, h):
        lo = max(0, t - window)
        hist = F[lo : t + 1]
        with np.errstate(all="ignore"), _quiet():
            m = np.nanmean(hist, axis=0)
            s = np.nanstd(hist, axis=0)
            z = (F[t] - m) / np.where(s == 0, np.nan, s)
        ok = ~(np.isnan(z) | np.isnan(R[t]))
        if t < 20 or not ok.any():
            continue
        w = np.zeros(N)
        w[ok] = np.sign(z[ok]) / ok.sum()
        weights.append(w)
        stamps.append(t)
        port.append(float(np.nansum(w * np.nan_to_num(R[t]))))
        for g, cond in enumerate((z < -0.5, (z >= -0.5) & (z <= 0.5), z > 0.5)):
            sel = R[t][ok & cond]
            groups[g].append(float(sel.mean()) if len(sel) else 0.0)
    return np.array(port), np.array(weights) if weights else np.zeros((0, N)), stamps, [np.array(g) for g in groups]


def _downsample(xs: list[Any], n: int = 360) -> list[Any]:
    if len(xs) <= n:
        return xs
    step = len(xs) / n
    return [xs[int(i * step)] for i in range(n)] + [xs[-1]]


def _nav(r: np.ndarray) -> np.ndarray:
    return np.cumprod(1 + np.nan_to_num(r)) if len(r) else np.array([])


def t_required(trials: int, alpha: float = 0.05) -> float:
    """按试验次数做 Bonferroni 修正后的双侧 t 门槛（近似为正态分位）。"""
    m = max(1, int(trials))
    return round(NormalDist().inv_cdf(1 - alpha / (2 * m)), 3)


def evaluate_factor(
    expr: str | Node,
    panel: Panel,
    *,
    horizon: int = 1,
    cost_bps: float = 10.0,
    library: dict[str, np.ndarray] | None = None,
    trials: int = 1,
    oos_frac: float = 0.3,
) -> dict[str, Any]:
    node = parse(expr) if isinstance(expr, str) else expr
    F = evaluate(node, panel.data)
    return evaluate_matrix(
        F,
        panel,
        warm=max_lookback(node),
        horizon=horizon,
        cost_bps=cost_bps,
        library=library,
        trials=trials,
        oos_frac=oos_frac,
    )


def evaluate_matrix(
    F: np.ndarray,
    panel: Panel,
    *,
    warm: int = 0,
    horizon: int = 1,
    cost_bps: float = 10.0,
    library: dict[str, np.ndarray] | None = None,
    trials: int = 1,
    oos_frac: float = 0.3,
) -> dict[str, Any]:
    """把任意 [T, N] 信号（因子值、模型预测）当成因子评估。前 warm 行视为预热，不参与统计。"""
    close = panel.data["close"]
    T, N = F.shape
    h = max(1, min(int(horizon), max(1, T // 10)))
    warm = min(int(warm), T - 1)
    F = np.asarray(F, dtype=float).copy()
    F[:warm] = np.nan
    cross = N >= 4
    R = forward_returns(close, h)
    tf_ms = TF_MS.get(panel.timeframe, 3_600_000)
    ppy = YEAR_MS / (tf_ms * h)

    idx, ic, ric = _ic_series(F, R, cross=cross)
    step = h if cross else 1
    s_ic, s_ric = _stats(ic, step), _stats(ric, step)

    # ---- 衰减 ----
    decay = []
    for hh in (1, 2, 4, 8, 16, 32):
        if hh * 10 > T:
            break
        _, _, r2 = _ic_series(F, forward_returns(close, hh), cross=cross)
        decay.append({"horizon": hh, "rank_ic": float(np.mean(r2)) if len(r2) else None})

    # ---- 方向：只用样本内的 RankIC 定方向（不偷看样本外），反向因子按取负后的组合计算多空 ----
    cut = int(T * (1 - oos_frac))
    is_ric = ric[idx < cut] if len(idx) else np.array([])
    sign = -1.0 if len(is_ric) and float(np.mean(is_ric)) < 0 else 1.0

    # ---- 分组与多空 ----
    if cross:
        n_groups = 5 if N >= 10 else 3
        grets, W, stamps = _groups_cross(F, R, n_groups, h)
        ls_gross = sign * (grets[-1] - grets[0]) / 2 if len(grets[0]) else np.array([])
        # 多空各半仓：收益 = 0.5 × 最高组 − 0.5 × 最低组（反向因子反过来）
    else:
        ls_gross, W, stamps, grets = _ts_positions(F, R, h)
        ls_gross = sign * ls_gross
        n_groups = 3
    if len(W):
        dw = np.abs(np.diff(np.vstack([np.zeros((1, W.shape[1])), W]), axis=0)).sum(axis=1)
        turnover = float(np.mean(dw[1:]) / 2) if len(dw) > 1 else float(dw[0] / 2)
        cost = dw * cost_bps / 1e4
    else:
        dw, turnover, cost = np.array([]), None, np.array([])
    ls_net = ls_gross - cost[: len(ls_gross)] if len(ls_gross) else ls_gross
    if len(ls_net) > 2 and np.std(ls_net) > 0:
        ls_sharpe = float(np.mean(ls_net) / np.std(ls_net, ddof=1) * math.sqrt(ppy))
        nav = _nav(ls_net)
        ls_return = float(nav[-1] ** (ppy / len(ls_net)) - 1) if nav[-1] > 0 else -1.0
        dd = float(np.max(1 - nav / np.maximum.accumulate(nav)))
    else:
        ls_sharpe, ls_return, dd, nav = None, None, None, np.array([])

    # ---- 覆盖率 ----
    live = ~np.isnan(close[warm:])
    coverage = float((~np.isnan(F[warm:]) & live).sum() / max(live.sum(), 1))

    # ---- 分状态 ----
    regimes = []
    with _quiet():
        mkt = np.nanmean(panel.data["ret"], axis=1)
    if len(idx) >= 10:
        trend = np.full(T, np.nan)
        vol = np.full(T, np.nan)
        for t in range(60, T):
            seg = mkt[t - 60 : t]
            trend[t] = np.nansum(seg)
            vol[t] = np.nanstd(seg)
        med = np.nanmedian(vol)
        labels = {"上涨期": trend > 0, "下跌期": trend <= 0, "高波动": vol > med, "低波动": vol <= med}
        for name, mask in labels.items():
            sel = ric[mask[idx]] if len(idx) else np.array([])
            regimes.append(
                {"regime": name, "rank_ic": float(np.mean(sel)) if len(sel) >= 3 else None, "periods": len(sel)}
            )

    # ---- 样本外 ----
    oos_ric = ric[idx >= cut + h] if len(idx) else np.array([])
    is_m = float(np.mean(is_ric)) if len(is_ric) >= 3 else None
    oos_m = float(np.mean(oos_ric)) if len(oos_ric) >= 3 else None
    retention = None
    if is_m is not None and oos_m is not None and abs(is_m) > 1e-6:
        retention = oos_m / is_m

    # ---- 子池（成交额前一半） ----
    sub = None
    if cross and N >= 8:
        amt = np.nanmean(panel.data["amount"], axis=0)
        top = np.argsort(-np.nan_to_num(amt))[: N // 2]
        _, _, r_sub = _ic_series(F[:, top], R[:, top], cross=True)
        sub = float(np.mean(r_sub)) if len(r_sub) else None

    # ---- 库内相关 ----
    corrs = []
    for name, G in (library or {}).items():
        if G.shape != F.shape:
            continue
        if cross:
            rows = np.arange(warm, T, max(1, T // 300))
            cc = _row_corr(_rank_rows(F[rows]), _rank_rows(G[rows]))
            c = float(np.nanmean(cc)) if np.any(~np.isnan(cc)) else None
        else:
            c = _corr(F.ravel(), G.ravel())
        if c is not None:
            corrs.append({"name": name, "corr": round(c, 4)})
    corrs.sort(key=lambda x: -abs(x["corr"]))
    max_corr = abs(corrs[0]["corr"]) if corrs else 0.0

    t_req = t_required(trials)
    summary = {
        "mode": "cross" if cross else "time_series",
        "horizon": h,
        "periods": s_ric["n"],
        "ic": s_ic["mean"],
        "rank_ic": s_ric["mean"],
        "ic_std": s_ric["std"],
        "icir": s_ric["ir"],
        "ic_t": s_ric["t"],
        "ic_win": s_ric["win"],
        "ls_sharpe": ls_sharpe,
        "ls_return": ls_return,
        "ls_total_return": float(nav[-1] - 1) if len(nav) else None,
        "ls_max_drawdown": dd,
        "turnover": turnover,
        "coverage": coverage,
        "max_corr": max_corr,
        "is_rank_ic": is_m,
        "oos_rank_ic": oos_m,
        "oos_retention": retention,
        "sub_rank_ic": sub,
        "t_required": t_req,
        "trials": int(trials),
        "direction": "正向" if sign > 0 else "反向",
        "rank_ic_x100": (s_ric["mean"] or 0) * 100,
        "cost_bps": cost_bps,
    }
    checks = admission_checks(summary)
    times = panel.times
    cum_ic = np.cumsum(ric) if len(ric) else np.array([])
    report = {
        "summary": summary,
        "checks": checks,
        "admitted": all(c["ok"] for c in checks if c["required"]),
        "decay": decay,
        "regimes": regimes,
        "correlations": corrs[:8],
        "groups": {
            "n": n_groups,
            "mean": [float(np.mean(g)) * 1e4 if len(g) else None for g in grets],  # 每期平均收益（bps）
            "nav": [
                {"t": int(times[s]), **{f"g{k + 1}": float(v) for k, v in enumerate(vals)}}
                for s, vals in _downsample(
                    list(zip(stamps, zip(*[_nav(g) for g in grets], strict=False), strict=False))
                )
            ]
            if len(stamps) and all(len(g) == len(stamps) for g in grets)
            else [],
        },
        "ls_nav": [{"t": int(times[s]), "nav": float(v)} for s, v in _downsample(list(zip(stamps, nav, strict=False)))],
        "ic_series": [
            {"t": int(times[i]), "rank_ic": float(r), "cum": float(c)}
            for i, r, c in _downsample(list(zip(idx, ric, cum_ic, strict=False)))
        ],
        "panel": panel.describe(),
    }
    from .contracts import annotate_report

    return annotate_report(report)


def admission_checks(s: dict[str, Any]) -> list[dict[str, Any]]:
    def fmt(v: Any, pct: bool = False) -> str:
        if v is None:
            return "—"
        return f"{v * 100:.0f}%" if pct else f"{v:.3f}"

    ric, t, t_req = s.get("rank_ic"), s.get("ic_t"), s.get("t_required") or 1.96
    ret, sub = s.get("oos_retention"), s.get("sub_rank_ic")
    is_ic, oos_ic = s.get("is_rank_ic"), s.get("oos_rank_ic")
    # 样本内 IC 本身接近 0 时，「保留率」是两个噪声的比值（可能出现 200% 这种数），不能算样本外有效
    weak_is = is_ic is not None and abs(is_ic) < ADMISSION.get("min_is_ic", 0.01)
    oos_ok = ret is not None and ret >= ADMISSION["oos_retention"] and not weak_is
    oos_detail = (
        f"样本内 RankIC {fmt(is_ic)} 接近 0，保留率没有意义（样本外 {fmt(oos_ic)}）"
        if weak_is
        else f"样本外 RankIC {fmt(oos_ic)}，保留 {fmt(ret, True)}（要求 ≥ 50%）"
    )
    out = [
        {
            "key": "significance",
            "label": "显著性（按试验次数修正）",
            "required": True,
            "ok": t is not None and abs(t) >= t_req,
            "detail": f"|t| = {fmt(abs(t) if t is not None else None)}，门槛 {t_req:.2f}（已试 {s.get('trials')} 次）",
        },
        {
            "key": "oos",
            "label": "样本外仍然有效",
            "required": True,
            "ok": oos_ok,
            "detail": oos_detail,
        },
        {
            "key": "novelty",
            "label": "与库内因子不重复",
            "required": True,
            "ok": (s.get("max_corr") or 0) <= ADMISSION["max_corr"],
            "detail": f"最大相关 {fmt(s.get('max_corr'))}（要求 ≤ {ADMISSION['max_corr']}）",
        },
        {
            "key": "coverage",
            "label": "覆盖率",
            "required": True,
            "ok": (s.get("coverage") or 0) >= ADMISSION["coverage"],
            "detail": f"{fmt(s.get('coverage'), True)}（要求 ≥ 80%）",
        },
        {
            "key": "turnover",
            "label": "换手可控",
            "required": False,
            "ok": s.get("turnover") is None or s["turnover"] <= ADMISSION["turnover"],
            "detail": f"每次调仓换手 {fmt(s.get('turnover'), True)}",
        },
    ]
    if sub is not None:
        out.append(
            {
                "key": "subpool",
                "label": "成交额前一半的币里同样有效",
                "required": False,
                "ok": ric is not None and sub * ric > 0,
                "detail": f"子池 RankIC {fmt(sub)}，全池 {fmt(ric)}",
            }
        )
    return out


__all__ = ["ADMISSION", "admission_checks", "evaluate_factor", "evaluate_matrix", "forward_returns", "t_required"]
