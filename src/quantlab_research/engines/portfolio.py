"""组合与风控：风险模型、优化器、Black-Litterman、约束、风险预算与压力测试，以及按时间滚动的组合回测。

一份**配置方案**（JSON）描述“怎么把预期收益和风险变成权重”：

    {"optimizer": "risk_parity", "risk_model": "ledoit_wolf", "lookback": 180, "rebalance": 6,
     "expected": "none" | "momentum" | "model:模型名" | "factor:因子名",
     "constraints": {"long_only": true, "max_weight": 0.35, "max_gross": 1.0, "max_turnover": 0.6, "top_k": 0},
     "risk": {"vol_target": 0.5, "var_limit": 0.05, "dd_stop": 0.2, "cppi": {"floor": 0.8, "multiplier": 3}},
     "views": [{"symbol": "BTC/USDT", "ret": 0.3, "confidence": 0.5}], ...}

- 风险模型：样本协方差、Ledoit-Wolf 收缩（向等方差等相关结构收缩，系数按公式估计）、EWMA、PCA 因子风险模型。
- 优化器：等权、波动倒数、最小方差、均值-方差、风险平价、HRP、最大分散化、最小 CVaR、分数凯利、Black-Litterman。
  都只用 numpy（投影梯度、乘法迭代、次梯度），设备上不装 scipy 也能跑。
- 预期收益来自模型或因子时按 Grinold 公式换算：α = IC × σ × 标准化分数，避免把预测值直接当收益。
- 回测完全按时间推进：每个调仓点只用之前 lookback 根的收益估计风险与预期；权重在调仓之间随价格漂移；扣换手成本。
- 风控规则（目标波动、VaR 上限、回撤止损、CPPI）在回测里逐期执行；实时调用 optimize() 时执行能算的部分。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

YEAR_MS = 365 * 86_400_000
TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class PortfolioError(ValueError):
    """配置方案有误或数据不足。"""


RISK_MODELS: dict[str, dict[str, str]] = {
    "sample": {"label": "样本协方差", "hint": "直接用历史收益的协方差；标的多、样本少时噪声大"},
    "ledoit_wolf": {"label": "Ledoit-Wolf 收缩", "hint": "向“等方差、等相关”的结构收缩，收缩强度按公式估计；默认推荐"},
    "ewma": {"label": "EWMA", "hint": "指数加权，近期权重更高，对波动聚集反应快（半衰期可调）"},
    "factor": {"label": "PCA 因子模型", "hint": "前 k 个主成分解释共同波动，其余记为个体风险；适合标的较多时"},
}

OPTIMIZERS: dict[str, dict[str, Any]] = {
    "equal": {"label": "等权", "hint": "每个标的同样权重；最朴素也最难被打败的基准", "needs_mu": False},
    "inverse_vol": {"label": "波动倒数", "hint": "权重与波动率成反比，不考虑相关性", "needs_mu": False},
    "min_var": {"label": "最小方差", "hint": "只看风险，组合波动最小", "needs_mu": False},
    "mean_var": {"label": "均值-方差", "hint": "预期收益 − 风险厌恶 × 方差，需要预期收益", "needs_mu": True},
    "risk_parity": {"label": "风险平价", "hint": "每个标的贡献相同的风险（等风险贡献）", "needs_mu": False},
    "hrp": {
        "label": "层次风险平价 HRP",
        "hint": "先按相关性聚类，再在簇之间按风险分配；不需要求逆，稳健",
        "needs_mu": False,
    },
    "max_div": {"label": "最大分散化", "hint": "最大化“加权波动 / 组合波动”，偏好相关性低的标的", "needs_mu": False},
    "min_cvar": {"label": "最小 CVaR", "hint": "直接最小化历史情景下的尾部平均损失，对肥尾更敏感", "needs_mu": False},
    "kelly": {
        "label": "分数凯利",
        "hint": "f × Σ⁻¹μ，再按总杠杆上限截断；需要预期收益，f 通常取 0.2~0.5",
        "needs_mu": True,
    },
    "black_litterman": {
        "label": "Black-Litterman",
        "hint": "以等权隐含收益为先验，融合观点（手写或模型给出）后做均值-方差",
        "needs_mu": False,
    },
}

EXPECTED_SOURCES = {
    "none": "不用预期收益",
    "momentum": "动量：过去 lookback 的平均收益（向截面均值收缩一半）",
    "mean": "历史均值",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "optimizer": "risk_parity",
    "risk_model": "ledoit_wolf",
    "lookback": 180,
    "rebalance": 6,
    "expected": "none",
    "risk_aversion": 4.0,
    "kelly_fraction": 0.3,
    "ewma_halflife": 40,
    "factors": 3,
    "cvar_alpha": 0.95,
    "tau": 0.05,
    "views": [],
    "constraints": {
        "long_only": True,
        "max_weight": 0.35,
        "min_weight": 0.0,
        "max_gross": 1.0,
        "max_turnover": 1.0,
        "top_k": 0,
    },
    "risk": {"vol_target": None, "var_limit": None, "dd_stop": None, "cppi": None},
}

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "covid_2020_03",
        "name": "2020-03 新冠暴跌",
        "start": 1583625600000,
        "end": 1584144000000,
        "btc": -0.48,
        "alt_extra": -0.08,
        "note": "3 月 12 日前后流动性枯竭，BTC 两天内腰斩",
    },
    {
        "id": "luna_2022_05",
        "name": "2022-05 LUNA 崩盘",
        "start": 1651881600000,
        "end": 1652400000000,
        "btc": -0.25,
        "alt_extra": -0.15,
        "note": "UST 脱锚，山寨币普遍跌得更深",
    },
    {
        "id": "ftx_2022_11",
        "name": "2022-11 FTX 倒闭",
        "start": 1667692800000,
        "end": 1668124800000,
        "btc": -0.22,
        "alt_extra": -0.10,
        "specific": {"SOL/USDT": -0.55},
        "note": "交易所信用危机，与 FTX 关联的币重挫",
    },
    {
        "id": "alt_crash",
        "name": "山寨币单边崩（假设）",
        "btc": -0.06,
        "alt_extra": -0.30,
        "note": "BTC 基本不动，山寨币集体下跌 30%",
    },
    {
        "id": "rate_shock",
        "name": "宏观加息冲击（假设）",
        "btc": -0.15,
        "alt_extra": -0.05,
        "note": "风险资产整体下杀，按各币对 BTC 的贝塔传导",
    },
]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def bars_per_year(timeframe: str) -> float:
    return YEAR_MS / TF_MS.get(timeframe, 14_400_000)


def bars_per_day(timeframe: str) -> float:
    return 86_400_000 / TF_MS.get(timeframe, 14_400_000)


def _num(v: Any, lo: float, hi: float, default: float | None) -> float | None:
    if v is None or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError) as exc:
        raise PortfolioError(f"数值无效：{v}") from exc
    if not math.isfinite(x):
        return default
    return min(max(x, lo), hi)


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(raw or {})
    opt = str(raw.get("optimizer") or DEFAULT_CONFIG["optimizer"])
    if opt not in OPTIMIZERS:
        raise PortfolioError(f"未知优化器 {opt}；可选：{'、'.join(OPTIMIZERS)}")
    rm = str(raw.get("risk_model") or DEFAULT_CONFIG["risk_model"])
    if rm not in RISK_MODELS:
        raise PortfolioError(f"未知风险模型 {rm}；可选：{'、'.join(RISK_MODELS)}")
    exp = str(raw.get("expected") or "none").strip()
    if not (exp in EXPECTED_SOURCES or exp.startswith("model:") or exp.startswith("factor:")):
        raise PortfolioError("expected 只能是 none / momentum / mean / model:模型名 / factor:因子名")
    if OPTIMIZERS[opt]["needs_mu"] and exp == "none":
        exp = "momentum"
    c_raw = {**DEFAULT_CONFIG["constraints"], **(raw.get("constraints") or {})}
    long_only = bool(c_raw.get("long_only", True))
    cons = {
        "long_only": long_only,
        "max_weight": _num(c_raw.get("max_weight"), 0.02, 1.0, 0.35),
        "min_weight": _num(c_raw.get("min_weight"), 0.0, 0.2, 0.0) if long_only else 0.0,
        "max_gross": _num(c_raw.get("max_gross"), 0.1, 3.0, 1.0),
        "max_turnover": _num(c_raw.get("max_turnover"), 0.01, 2.0, 1.0),
        "top_k": int(_num(c_raw.get("top_k"), 0, 50, 0) or 0),
    }
    r_raw = {**DEFAULT_CONFIG["risk"], **(raw.get("risk") or {})}
    cppi = r_raw.get("cppi")
    if isinstance(cppi, dict) and cppi:
        cppi = {"floor": _num(cppi.get("floor"), 0.3, 0.98, 0.8), "multiplier": _num(cppi.get("multiplier"), 1, 10, 3)}
    else:
        cppi = None
    risk = {
        "vol_target": _num(r_raw.get("vol_target"), 0.02, 3.0, None),
        "var_limit": _num(r_raw.get("var_limit"), 0.002, 0.5, None),
        "dd_stop": _num(r_raw.get("dd_stop"), 0.02, 0.9, None),
        "cppi": cppi,
    }
    views = []
    for v in raw.get("views") or []:
        if isinstance(v, dict) and v.get("symbol") and v.get("ret") is not None:
            views.append(
                {
                    "symbol": str(v["symbol"]).upper(),
                    "ret": _num(v.get("ret"), -5, 5, 0.0),
                    "confidence": _num(v.get("confidence"), 0.01, 0.99, 0.5),
                }
            )
    return {
        "optimizer": opt,
        "risk_model": rm,
        "expected": exp,
        "lookback": int(_num(raw.get("lookback"), 30, 2000, DEFAULT_CONFIG["lookback"]) or 180),
        "rebalance": int(_num(raw.get("rebalance"), 1, 500, DEFAULT_CONFIG["rebalance"]) or 6),
        "risk_aversion": _num(raw.get("risk_aversion"), 0.1, 50, 4.0),
        "kelly_fraction": _num(raw.get("kelly_fraction"), 0.05, 1.0, 0.3),
        "ewma_halflife": int(_num(raw.get("ewma_halflife"), 5, 500, 40) or 40),
        "factors": int(_num(raw.get("factors"), 1, 10, 3) or 3),
        "cvar_alpha": _num(raw.get("cvar_alpha"), 0.8, 0.995, 0.95),
        "tau": _num(raw.get("tau"), 0.005, 1.0, 0.05),
        "views": views,
        "constraints": cons,
        "risk": risk,
    }


# ---------------------------------------------------------------------------
# 风险模型
# ---------------------------------------------------------------------------
def _clean(R: np.ndarray) -> np.ndarray:
    R = np.where(np.isfinite(R), R, 0.0)
    return R - R.mean(axis=0, keepdims=True)


def ledoit_wolf(R: np.ndarray) -> tuple[np.ndarray, float]:
    """向常相关结构收缩（Ledoit & Wolf 2004，“Honey, I shrunk the sample covariance matrix”）。"""
    X = _clean(R)
    T, N = X.shape
    S = X.T @ X / T
    var = np.diag(S).copy()
    sd = np.sqrt(np.maximum(var, 1e-18))
    corr = S / np.outer(sd, sd)
    rbar = (corr.sum() - N) / max(N * (N - 1), 1)
    F = rbar * np.outer(sd, sd)
    np.fill_diagonal(F, var)
    Y = X**2
    pi_mat = Y.T @ Y / T - S**2
    pi_hat = pi_mat.sum()
    theta_ii = (X**3).T @ X / T - var[:, None] * S
    theta_jj = X.T @ (X**3) / T - var[None, :] * S
    rho_off = (rbar / 2) * ((np.outer(1 / sd, sd)) * theta_ii + (np.outer(sd, 1 / sd)) * theta_jj)
    np.fill_diagonal(rho_off, 0.0)
    rho_hat = np.diag(pi_mat).sum() + rho_off.sum()
    gamma = ((F - S) ** 2).sum()
    kappa = (pi_hat - rho_hat) / gamma if gamma > 0 else 0.0
    delta = float(min(max(kappa / T, 0.0), 1.0))
    return delta * F + (1 - delta) * S, delta


def covariance(R: np.ndarray, model: str = "ledoit_wolf", *, halflife: int = 40, factors: int = 3) -> np.ndarray:
    R = np.where(np.isfinite(R), R, 0.0)
    T, N = R.shape
    if T < 3:
        return np.eye(N) * 1e-4
    if model == "sample":
        S = np.cov(R, rowvar=False, bias=True).reshape(N, N)
    elif model == "ewma":
        w = 0.5 ** (np.arange(T)[::-1] / max(halflife, 1))
        w /= w.sum()
        X = R - (w[:, None] * R).sum(axis=0)
        S = (X * w[:, None]).T @ X
    elif model == "factor":
        X = _clean(R)
        S0 = X.T @ X / T
        vals, vecs = np.linalg.eigh(S0)
        k = max(1, min(factors, N - 1))
        idx = np.argsort(vals)[::-1][:k]
        B = vecs[:, idx] * np.sqrt(np.maximum(vals[idx], 0))
        common = B @ B.T
        spec = np.maximum(np.diag(S0) - np.diag(common), np.diag(S0) * 0.05)
        S = common + np.diag(spec)
    else:
        S, _ = ledoit_wolf(R)
    S = (S + S.T) / 2
    return S + np.eye(N) * 1e-10


# ---------------------------------------------------------------------------
# 约束投影
# ---------------------------------------------------------------------------
def project_simplex_box(v: np.ndarray, total: float, lo: float, hi: float) -> np.ndarray:
    """投影到 {Σw = total, lo ≤ w ≤ hi}。s(λ) = Σclip(v − λ, lo, hi) 分段线性，按断点排序后精确求解。"""
    n = len(v)
    total = min(max(total, n * lo), n * hi)
    bps = np.sort(np.concatenate([v - lo, v - hi]))
    vals = np.clip(v[None, :] - bps[:, None], lo, hi).sum(axis=1)  # 随 λ 增大单调不增
    k = int(np.searchsorted(-vals, -total, side="left"))
    if k <= 0:
        lam = float(bps[0])
    elif k >= len(bps):
        lam = float(bps[-1])
    else:
        l0, l1, s0, s1 = bps[k - 1], bps[k], vals[k - 1], vals[k]
        lam = float(l0 if s0 == s1 else l0 + (s0 - total) * (l1 - l0) / (s0 - s1))
    return np.clip(v - lam, lo, hi)


def apply_constraints(
    w: np.ndarray, cons: dict[str, Any], *, score: np.ndarray | None = None, budget: float | None = None
) -> np.ndarray:
    """单币上下限、只做多、总杠杆、只持前 k 个。budget 为 None 时满仓（Σw = 1 或总杠杆）。"""
    w = np.where(np.isfinite(w), w, 0.0).astype(float)
    n = len(w)
    hi = float(cons.get("max_weight") or 1.0)
    gross = float(cons.get("max_gross") or 1.0)
    k = int(cons.get("top_k") or 0)
    if k and k < n:
        key = score if score is not None else np.abs(w)
        keep = np.argsort(-np.where(np.isfinite(key), key, -np.inf))[:k]
        mask = np.zeros(n, dtype=bool)
        mask[keep] = True
        w = np.where(mask, w, 0.0)
    if cons.get("long_only", True):
        total = min(gross, 1.0) if budget is None else min(budget, gross)
        active = w > 0 if (k and k < n) else np.ones(n, dtype=bool)
        out = np.zeros(n)
        m = int(active.sum())
        if m == 0:
            return out
        lo = float(cons.get("min_weight") or 0.0)
        lo = min(lo, total / m)
        out[active] = project_simplex_box(np.maximum(w[active], 0), total, lo, min(hi, total))
        return out
    w = np.clip(w, -hi, hi)
    g = np.abs(w).sum()
    if g > gross:
        w *= gross / g
    return w


# ---------------------------------------------------------------------------
# 优化器
# ---------------------------------------------------------------------------
def _pgd(S: np.ndarray, mu: np.ndarray, lam: float, cons: dict[str, Any], iters: int = 400) -> np.ndarray:
    """最小化 λ/2·wᵀΣw − μᵀw，约束为只做多 + 预算 + 单币上限（投影梯度）。"""
    n = len(mu)
    L = lam * float(np.linalg.eigvalsh(S).max()) + 1e-12
    step = 1.0 / L
    w = apply_constraints(np.full(n, 1.0 / n), cons)
    for _ in range(iters):
        g = lam * S @ w - mu
        w_new = apply_constraints(w - step * g, cons)
        if np.abs(w_new - w).max() < 1e-9:
            w = w_new
            break
        w = w_new
    return w


def risk_parity(S: np.ndarray, budget: np.ndarray | None = None, iters: int = 500) -> np.ndarray:
    n = S.shape[0]
    b = np.full(n, 1.0 / n) if budget is None else budget / budget.sum()
    w = 1 / np.sqrt(np.maximum(np.diag(S), 1e-18))
    w /= w.sum()
    for _ in range(iters):
        rc = w * (S @ w)
        rc = rc / max(rc.sum(), 1e-18)
        w_new = w * np.sqrt(b / np.maximum(rc, 1e-12))
        w_new /= w_new.sum()
        if np.abs(w_new - w).max() < 1e-10:
            return w_new
        w = w_new
    return w


def hrp(S: np.ndarray) -> np.ndarray:
    """López de Prado (2016)：单链接聚类 → 准对角化排序 → 递归二分按簇方差倒数分配。"""
    n = S.shape[0]
    if n == 1:
        return np.ones(1)
    sd = np.sqrt(np.maximum(np.diag(S), 1e-18))
    corr = np.clip(S / np.outer(sd, sd), -1, 1)
    dist = np.sqrt(np.maximum((1 - corr) / 2, 0))
    clusters: dict[int, list[int]] = {i: [i] for i in range(n)}
    order_tree: dict[int, tuple[int, int]] = {}
    D = {(i, j): dist[i, j] for i in range(n) for j in range(i + 1, n)}
    nxt = n
    while len(clusters) > 1:
        (a, b), _ = min(D.items(), key=lambda kv: kv[1])
        clusters[nxt] = clusters[a] + clusters[b]
        order_tree[nxt] = (a, b)
        del clusters[a], clusters[b]
        D = {k: v for k, v in D.items() if a not in k and b not in k}
        for c in clusters:
            if c == nxt:
                continue
            d = min(dist[i, j] for i in clusters[nxt] for j in clusters[c])
            D[(min(c, nxt), max(c, nxt))] = d
        nxt += 1
    root = next(iter(clusters))

    def leaves(node: int) -> list[int]:
        if node < n:
            return [node]
        a, b = order_tree[node]
        return leaves(a) + leaves(b)

    order = leaves(root)
    w = np.ones(n)

    def cvar(items: list[int]) -> float:
        sub = S[np.ix_(items, items)]
        iv = 1 / np.maximum(np.diag(sub), 1e-18)
        iv /= iv.sum()
        return float(iv @ sub @ iv)

    stack = [order]
    while stack:
        items = stack.pop()
        if len(items) < 2:
            continue
        half = len(items) // 2
        left, right = items[:half], items[half:]
        vl, vr = cvar(left), cvar(right)
        alpha = 1 - vl / (vl + vr)
        w[left] *= alpha
        w[right] *= 1 - alpha
        stack += [left, right]
    return w / w.sum()


def max_diversification(S: np.ndarray, cons: dict[str, Any]) -> np.ndarray:
    sd = np.sqrt(np.maximum(np.diag(S), 1e-18))
    C = S / np.outer(sd, sd)
    y = _pgd(C, np.zeros(len(sd)), 1.0, {**cons, "max_weight": 1.0, "top_k": 0})
    w = y / sd
    return w / w.sum()


def min_cvar(R: np.ndarray, alpha: float, cons: dict[str, Any], iters: int = 150) -> np.ndarray:
    """Rockafellar-Uryasev：在历史情景上最小化 CVaR，投影次梯度 + 记录最优点。"""
    R = np.where(np.isfinite(R), R, 0.0)
    T, n = R.shape
    w = apply_constraints(np.full(n, 1.0 / n), cons)
    best, best_val = w.copy(), np.inf
    k = max(1, round((1 - alpha) * T))
    scale = float(np.abs(R).mean()) + 1e-9
    for i in range(iters):
        loss = -R @ w
        idx = np.argsort(loss)[-k:]
        val = float(loss[idx].mean())
        if val < best_val:
            best, best_val = w.copy(), val
        g = -R[idx].mean(axis=0)
        w = apply_constraints(w - (0.5 / scale) / math.sqrt(i + 1) * g / max(np.abs(g).max(), 1e-12) * scale, cons)
    return best


def black_litterman(
    S: np.ndarray,
    symbols: list[str],
    views: list[dict[str, Any]],
    *,
    delta: float,
    tau: float,
    w_prior: np.ndarray,
    per_bar: float,
    mu_views: np.ndarray | None = None,
    view_conf: float = 0.3,
) -> tuple[np.ndarray, np.ndarray]:
    """返回 (后验预期收益, 先验隐含收益)，单位都是每根 K 线。观点为绝对观点：某币年化收益 = ret。"""
    n = len(symbols)
    pi = delta * S @ w_prior
    rows, q, conf = [], [], []
    for v in views:
        if v["symbol"] in symbols:
            p = np.zeros(n)
            p[symbols.index(v["symbol"])] = 1
            rows.append(p)
            q.append(v["ret"] / per_bar)
            conf.append(v["confidence"])
    if mu_views is not None:
        for j in range(n):
            if np.isfinite(mu_views[j]):
                p = np.zeros(n)
                p[j] = 1
                rows.append(p)
                q.append(float(mu_views[j]))
                conf.append(view_conf)
    if not rows:
        return pi, pi
    P = np.array(rows)
    Q = np.array(q)
    c = np.array(conf)
    base = np.diag(P @ (tau * S) @ P.T)
    omega = np.diag(base * (1 - c) / c)
    tS_inv = np.linalg.inv(tau * S)
    om_inv = np.linalg.inv(omega)
    post = np.linalg.solve(tS_inv + P.T @ om_inv @ P, tS_inv @ pi + P.T @ om_inv @ Q)
    return post, pi


def grinold_alpha(score: np.ndarray, sigma: np.ndarray, ic: float) -> np.ndarray:
    """α = IC × σ × z：把预测分数换算成每根 K 线的预期超额收益。"""
    s = np.where(np.isfinite(score), score, np.nan)
    m, sd = np.nanmean(s) if np.isfinite(s).any() else 0.0, np.nanstd(s) if np.isfinite(s).sum() > 1 else 0.0
    z = (s - m) / sd if sd > 0 else np.zeros_like(s)
    return np.where(np.isfinite(z), ic * sigma * np.clip(z, -3, 3), 0.0)


def solve(
    cfg: dict[str, Any], R: np.ndarray, symbols: list[str], mu: np.ndarray | None, *, per_bar: float
) -> dict[str, Any]:
    """给定一段收益历史（只含过去）与预期收益，算未经风控叠加的目标权重。"""
    n = R.shape[1]
    S = covariance(R, cfg["risk_model"], halflife=cfg["ewma_halflife"], factors=cfg["factors"])
    cons = cfg["constraints"]
    sigma = np.sqrt(np.maximum(np.diag(S), 1e-18))
    mu = np.zeros(n) if mu is None else np.where(np.isfinite(mu), mu, 0.0)
    opt = cfg["optimizer"]
    lam = cfg["risk_aversion"]
    extra: dict[str, Any] = {}
    if opt == "equal":
        w = np.full(n, 1.0 / n)
    elif opt == "inverse_vol":
        w = 1 / sigma
        w /= w.sum()
    elif opt == "min_var":
        w = _pgd(S, np.zeros(n), 1.0, cons)
    elif opt == "mean_var":
        w = _pgd(S, mu, lam, cons)
    elif opt == "risk_parity":
        w = risk_parity(S)
    elif opt == "hrp":
        w = hrp(S)
    elif opt == "max_div":
        w = max_diversification(S, cons)
    elif opt == "min_cvar":
        w = min_cvar(R, cfg["cvar_alpha"], cons)
    elif opt == "kelly":
        raw = np.linalg.solve(S + np.eye(n) * float(np.trace(S)) / n * 0.1, mu) * cfg["kelly_fraction"]
        if cons.get("long_only", True):
            raw = np.maximum(raw, 0)
        g = np.abs(raw).sum()
        budget = min(g, float(cons.get("max_gross") or 1.0))
        w = apply_constraints(raw, cons, score=mu, budget=budget) if g > 0 else np.zeros(n)
        return {"w": w, "cov": S, "mu": mu, "extra": {"kelly_gross": round(float(g), 4)}}
    elif opt == "black_litterman":
        prior = np.full(n, 1.0 / n)
        post, pi = black_litterman(
            S,
            symbols,
            cfg["views"],
            delta=lam,
            tau=cfg["tau"],
            w_prior=prior,
            per_bar=per_bar,
            mu_views=None if cfg["expected"] == "none" else mu,
        )
        w = _pgd(S, post, lam, cons)
        extra = {"prior": pi, "posterior": post}
        mu = post
    else:  # pragma: no cover
        raise PortfolioError(f"未知优化器 {opt}")
    w = apply_constraints(w, cons, score=mu if opt in ("mean_var", "black_litterman") else None)
    return {"w": w, "cov": S, "mu": mu, "extra": extra}


def overlay(
    w: np.ndarray,
    S: np.ndarray,
    risk: dict[str, Any],
    cons: dict[str, Any],
    *,
    per_bar: float,
    per_day: float,
    equity_state: dict[str, float] | None = None,
) -> tuple[np.ndarray, list[str]]:
    """风控叠加：目标波动、VaR 上限、回撤止损、CPPI。返回缩放后的权重与触发记录。"""
    notes: list[str] = []
    vol_bar = math.sqrt(max(float(w @ S @ w), 0.0))
    scale = 1.0
    if risk.get("vol_target") and vol_bar > 0:
        target_bar = risk["vol_target"] / math.sqrt(per_bar)
        s = target_bar / vol_bar
        cap = float(cons.get("max_gross") or 1.0) / max(np.abs(w).sum(), 1e-12)
        scale = min(s, cap)
        if s < 1:
            notes.append("vol_target")
    if risk.get("var_limit") and vol_bar > 0:
        var_day = 1.645 * vol_bar * math.sqrt(per_day) * scale
        if var_day > risk["var_limit"]:
            scale *= risk["var_limit"] / var_day
            notes.append("var_limit")
    if equity_state is not None:
        eq, peak, init = equity_state["equity"], equity_state["peak"], equity_state["initial"]
        if risk.get("dd_stop") and peak > 0 and 1 - eq / peak > risk["dd_stop"]:
            scale *= 0.5
            notes.append("dd_stop")
        cp = risk.get("cppi")
        if cp:
            floor = cp["floor"] * init
            cushion = max(eq - floor, 0.0) / eq if eq > 0 else 0.0
            expo = min(1.0, cp["multiplier"] * cushion)
            if expo < 1:
                scale *= expo
                notes.append("cppi")
    return w * scale, notes


# ---------------------------------------------------------------------------
# 风险度量
# ---------------------------------------------------------------------------
def risk_contribution(w: np.ndarray, S: np.ndarray) -> np.ndarray:
    var = float(w @ S @ w)
    if var <= 0:
        return np.zeros_like(w)
    return w * (S @ w) / var


def var_cvar(r: np.ndarray, alpha: float = 0.95) -> tuple[float, float]:
    r = r[np.isfinite(r)]
    if len(r) < 10:
        return float("nan"), float("nan")
    q = float(np.quantile(r, 1 - alpha))
    tail = r[r <= q]
    return -q, -float(tail.mean()) if len(tail) else -q


def stress_test(
    weights: dict[str, float],
    R: np.ndarray,
    symbols: list[str],
    *,
    closes: np.ndarray | None = None,
    times: np.ndarray | None = None,
    S: np.ndarray | None = None,
    per_day: float = 6.0,
) -> list[dict[str, Any]]:
    """历史情景重放（数据覆盖时）或按对 BTC 的贝塔推算的风格化情景，外加“相关性趋同”。"""
    w = np.array([weights.get(s, 0.0) for s in symbols])
    R = np.where(np.isfinite(R), R, 0.0)
    anchor = symbols.index("BTC/USDT") if "BTC/USDT" in symbols else None
    mkt = R[:, anchor] if anchor is not None else R.mean(axis=1)
    vm = float(mkt.var()) or 1e-12
    beta = np.array([float(np.cov(R[:, j], mkt, bias=True)[0, 1]) / vm for j in range(len(symbols))])
    out = []
    for sc in SCENARIOS:
        method = "按贝塔推算"
        shock = None
        if (
            closes is not None
            and times is not None
            and sc.get("start")
            and times[0] <= sc["start"]
            and times[-1] >= sc["end"]
        ):
            i0 = int(np.searchsorted(times, sc["start"]))
            i1 = int(np.searchsorted(times, sc["end"]))
            with np.errstate(invalid="ignore", divide="ignore"):
                shock = closes[min(i1, len(times) - 1)] / closes[i0] - 1
            if np.isfinite(shock).all():
                method = "历史重放"
            else:
                shock = None
        if shock is None:
            shock = np.array(
                [
                    beta[j] * sc["btc"] + (0.0 if s == "BTC/USDT" else sc.get("alt_extra", 0.0))
                    for j, s in enumerate(symbols)
                ]
            )
            for s, v in (sc.get("specific") or {}).items():
                if s in symbols:
                    shock[symbols.index(s)] = v
            shock = np.maximum(shock, -0.95)
        contrib = w * shock
        worst = int(np.argmin(contrib)) if len(contrib) else 0
        out.append(
            {
                "id": sc["id"],
                "name": sc["name"],
                "method": method,
                "note": sc.get("note", ""),
                "pnl": round(float(contrib.sum()), 5),
                "worst": symbols[worst] if len(symbols) else None,
                "worst_pnl": round(float(contrib[worst]), 5),
                "shocks": {s: round(float(x), 4) for s, x in zip(symbols, shock, strict=True)},
            }
        )
    if S is not None:
        sd = np.sqrt(np.maximum(np.diag(S), 1e-18))
        C = np.full_like(S, 0.9)
        np.fill_diagonal(C, 1.0)
        S9 = C * np.outer(sd, sd)
        v_now = 2.326 * math.sqrt(max(float(w @ S @ w), 0)) * math.sqrt(per_day)
        v_9 = 2.326 * math.sqrt(max(float(w @ S9 @ w), 0)) * math.sqrt(per_day)
        out.append(
            {
                "id": "corr_09",
                "name": "相关性趋同（ρ = 0.9）",
                "method": "参数法",
                "note": f"危机时相关性往往一起升高；单日 99% VaR 从 {v_now:.1%} 变为 {v_9:.1%}",
                "pnl": round(-v_9, 5),
                "worst": None,
                "worst_pnl": None,
                "shocks": {},
            }
        )
    return out


def efficient_frontier(
    S: np.ndarray, mu: np.ndarray, cons: dict[str, Any], *, per_bar: float, points: int = 14
) -> list[dict[str, float]]:
    out = []
    for lam in np.geomspace(0.3, 300, points):
        w = _pgd(S, mu, float(lam), {**cons, "top_k": 0}, iters=250)
        out.append({"vol": float(math.sqrt(max(w @ S @ w, 0)) * math.sqrt(per_bar)), "ret": float(mu @ w * per_bar)})
    out.sort(key=lambda x: x["vol"])
    return out


def _metrics(r: np.ndarray, per_bar: float) -> dict[str, float | None]:
    r = r[np.isfinite(r)]
    if len(r) < 5:
        return {"ann_return": None, "ann_vol": None, "sharpe": None, "max_drawdown": None, "calmar": None}
    nav = np.cumprod(1 + r)
    years = len(r) / per_bar
    ann_ret = float(nav[-1] ** (1 / years) - 1) if nav[-1] > 0 and years > 0 else -1.0
    vol = float(r.std() * math.sqrt(per_bar))
    sharpe = float(r.mean() / r.std() * math.sqrt(per_bar)) if r.std() > 0 else 0.0
    peak = np.maximum.accumulate(nav)
    mdd = float((1 - nav / peak).max())
    return {
        "ann_return": ann_ret,
        "ann_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": mdd,
        "calmar": ann_ret / mdd if mdd > 1e-9 else None,
    }


# ---------------------------------------------------------------------------
# 预期收益
# ---------------------------------------------------------------------------
def expected_at(cfg: dict[str, Any], R_hist: np.ndarray, signal_row: np.ndarray | None, ic: float) -> np.ndarray | None:
    src = cfg["expected"]
    n = R_hist.shape[1]
    if src == "none":
        return None
    sigma = np.nanstd(R_hist, axis=0)
    if src == "mean":
        return np.nan_to_num(np.nanmean(R_hist, axis=0))
    if src == "momentum":
        m = np.nan_to_num(np.nanmean(R_hist, axis=0))
        return 0.5 * m + 0.5 * m.mean()
    if signal_row is None:
        return np.zeros(n)
    return grinold_alpha(signal_row, np.nan_to_num(sigma), ic)


def evaluate_scheme(
    config: dict[str, Any],
    panel: Any,
    *,
    cost_bps: float = 10.0,
    signal: np.ndarray | None = None,
    signal_ic: float = 0.03,
    stop: Any = None,
) -> dict[str, Any]:
    """滚动组合回测：每个调仓点只用过去的数据估计。

    返回 {summary, nav, weights, latest, risk, stress, frontier, ...}。
    """
    cfg = normalize_config(config)
    close = panel.data["close"]
    T, N = close.shape
    if N < 2:
        raise PortfolioError("组合至少需要 2 个标的")
    with np.errstate(invalid="ignore", divide="ignore"):
        R = close[1:] / close[:-1] - 1
    R = np.vstack([np.full((1, N), np.nan), R])
    per_bar = bars_per_year(panel.timeframe)
    per_day = bars_per_day(panel.timeframe)
    lb, reb = cfg["lookback"], cfg["rebalance"]
    if T < lb + 60:
        raise PortfolioError(f"K 线不够：需要至少 lookback + 60 = {lb + 60} 根，现在 {T} 根")
    cost = cost_bps / 1e4
    w = np.zeros(N)
    w_ew = np.zeros(N)
    port, bench = np.full(T, np.nan), np.full(T, np.nan)
    eq = {"equity": 1.0, "peak": 1.0, "initial": 1.0}
    weights_log: list[dict[str, Any]] = []
    turns: list[float] = []
    triggers: dict[str, int] = {}
    last: dict[str, Any] = {}
    for t in range(lb, T - 1):
        if stop is not None and (t - lb) % 50 == 0:
            stop()
        if (t - lb) % reb == 0:
            hist = R[t - lb + 1 : t + 1]
            live = np.isfinite(close[t]) & (np.isfinite(hist).sum(axis=0) >= lb * 0.8)
            cols = np.where(live)[0]
            target = np.zeros(N)
            if len(cols) >= 2:
                Rh = hist[:, cols]
                sig = signal[t, cols] if signal is not None else None
                mu = expected_at(cfg, Rh, sig, signal_ic)
                sol = solve(cfg, np.nan_to_num(Rh), [panel.symbols[j] for j in cols], mu, per_bar=per_bar)
                wv, notes = overlay(
                    sol["w"],
                    sol["cov"],
                    cfg["risk"],
                    cfg["constraints"],
                    per_bar=per_bar,
                    per_day=per_day,
                    equity_state=eq,
                )
                for k in notes:
                    triggers[k] = triggers.get(k, 0) + 1
                target[cols] = wv
                last = {
                    "cols": cols,
                    "cov": sol["cov"],
                    "mu": sol["mu"],
                    "w": wv,
                    "extra": sol["extra"],
                    "t": t,
                    "notes": notes,
                }
            mt = float(cfg["constraints"]["max_turnover"])
            delta = target - w
            turn = float(np.abs(delta).sum())
            if turn > mt:
                target = w + delta * (mt / turn)
                turn = mt
            port_cost = turn * cost
            w = target
            turns.append(turn)
            ew_t = np.where(live, 1.0 / max(live.sum(), 1), 0.0)
            ew_cost = float(np.abs(ew_t - w_ew).sum()) * cost
            w_ew = ew_t
            if len(weights_log) < 400:
                weights_log.append(
                    {
                        "t": int(panel.times[t]),
                        **{panel.symbols[j]: round(float(w[j]), 4) for j in range(N) if abs(w[j]) > 1e-4},
                    }
                )
        else:
            port_cost = ew_cost = 0.0
        r1 = np.nan_to_num(R[t + 1])
        pr = float(w @ r1) - port_cost
        br = float(w_ew @ r1) - ew_cost
        port[t + 1], bench[t + 1] = pr, br
        eq["equity"] *= 1 + pr
        eq["peak"] = max(eq["peak"], eq["equity"])
        # 权重随价格漂移
        gross = 1 + pr + port_cost
        if gross > 0:
            w = w * (1 + r1) / gross
        bg = 1 + br + ew_cost
        if bg > 0:
            w_ew = w_ew * (1 + r1) / bg
    pr = port[lb + 1 :]
    br = bench[lb + 1 :]
    m = _metrics(pr, per_bar)
    mb = _metrics(br, per_bar)
    _, cvar = var_cvar(pr, 0.95)
    eff_n = [
        float(np.abs([v for k, v in row.items() if k != "t"]).sum()) ** 2
        / max(float((np.array([v for k, v in row.items() if k != "t"]) ** 2).sum()), 1e-9)
        for row in weights_log
        if len(row) > 1
    ]
    half = len(pr) // 2
    h1, h2 = _metrics(pr[:half], per_bar), _metrics(pr[half:], per_bar)
    summary = {
        **m,
        "cvar_95": cvar,
        "turnover": float(np.mean(turns)) if turns else 0.0,
        "effective_n": float(np.mean(eff_n)) if eff_n else None,
        "ew_sharpe": mb["sharpe"],
        "ew_max_drawdown": mb["max_drawdown"],
        "ew_ann_return": mb["ann_return"],
        "beat_ew": (m["sharpe"] or 0) - (mb["sharpe"] or 0) if m["sharpe"] is not None else None,
        "dd_vs_ew": (mb["max_drawdown"] or 0) - (m["max_drawdown"] or 0) if m["max_drawdown"] is not None else None,
        "sharpe_h1": h1["sharpe"],
        "sharpe_h2": h2["sharpe"],
        "rebalances": len(turns),
        "avg_exposure": float(np.mean([sum(abs(v) for k, v in row.items() if k != "t") for row in weights_log]))
        if weights_log
        else None,
        "optimizer": cfg["optimizer"],
        "risk_model": cfg["risk_model"],
        "bars": len(pr),
        "triggers": triggers,
        "cost_bps": cost_bps,
    }
    nav = np.cumprod(1 + np.nan_to_num(pr))
    nav_b = np.cumprod(1 + np.nan_to_num(br))
    step = max(1, len(nav) // 500)
    times = panel.times[lb + 1 :]
    peak = np.maximum.accumulate(nav)
    report: dict[str, Any] = {
        "summary": summary,
        "config": cfg,
        "nav": [
            {"t": int(times[i]), "nav": float(nav[i]), "ew": float(nav_b[i]), "dd": float(1 - nav[i] / peak[i])}
            for i in range(0, len(nav), step)
        ],
        "weights": weights_log[:: max(1, len(weights_log) // 120)],
    }
    if last:
        cols = last["cols"]
        syms = [panel.symbols[j] for j in cols]
        wl = last["w"]
        S = last["cov"]
        rc = risk_contribution(wl, S)
        hist = np.nan_to_num(R[max(1, T - lb) :, cols])
        pr_hist = hist @ wl
        v95, c95 = var_cvar(pr_hist, 0.95)
        v99, c99 = var_cvar(pr_hist, 0.99)
        vol_bar = math.sqrt(max(float(wl @ S @ wl), 0))
        sd = np.sqrt(np.maximum(np.diag(S), 1e-18))
        # 展示用样本相关（收缩后的协方差会把相关性拉向均值，看不出结构）
        corr = np.nan_to_num(np.corrcoef(hist, rowvar=False)) if hist.shape[0] > 2 else S / np.outer(sd, sd)
        report["latest"] = {
            "t": int(panel.times[last["t"]]),
            "weights": {s: round(float(x), 4) for s, x in zip(syms, wl, strict=True)},
            "cash": round(1 - float(wl.sum()), 4) if cfg["constraints"]["long_only"] else None,
            "risk_contrib": {s: round(float(x), 4) for s, x in zip(syms, rc, strict=True)},
            "vol": {s: round(float(x * math.sqrt(per_bar)), 4) for s, x in zip(syms, sd, strict=True)},
            "expected": {s: round(float(x * per_bar), 4) for s, x in zip(syms, last["mu"], strict=True)}
            if cfg["expected"] != "none" or cfg["optimizer"] == "black_litterman"
            else None,
            "notes": last["notes"],
        }
        if "prior" in last["extra"]:
            report["latest"]["bl"] = {
                "prior": {s: round(float(x * per_bar), 4) for s, x in zip(syms, last["extra"]["prior"], strict=True)},
                "posterior": {
                    s: round(float(x * per_bar), 4) for s, x in zip(syms, last["extra"]["posterior"], strict=True)
                },
            }
        report["risk"] = {
            "ann_vol": vol_bar * math.sqrt(per_bar),
            "var_95_bar": v95,
            "cvar_95_bar": c95,
            "var_99_bar": v99,
            "cvar_99_bar": c99,
            "var_95_day_param": 1.645 * vol_bar * math.sqrt(per_day),
            "corr": {"symbols": syms, "values": [[round(float(x), 3) for x in row] for row in corr]},
        }
        report["stress"] = stress_test(
            report["latest"]["weights"], hist, syms, closes=close[:, cols], times=panel.times, S=S, per_day=per_day
        )
        mu_f = last["mu"] if np.abs(last["mu"]).sum() > 0 else np.nan_to_num(np.nanmean(hist, axis=0))
        report["frontier"] = {
            "points": efficient_frontier(S, mu_f, cfg["constraints"], per_bar=per_bar),
            "current": {"vol": vol_bar * math.sqrt(per_bar), "ret": float(mu_f @ wl * per_bar)},
            "equal": {
                "vol": math.sqrt(
                    max(float(np.full(len(cols), 1 / len(cols)) @ S @ np.full(len(cols), 1 / len(cols))), 0)
                )
                * math.sqrt(per_bar),
                "ret": float(mu_f.mean() * per_bar),
            },
            "assets": [
                {"symbol": s, "vol": float(sd[i] * math.sqrt(per_bar)), "ret": float(mu_f[i] * per_bar)}
                for i, s in enumerate(syms)
            ],
            "mu_source": "预期收益" if np.abs(last["mu"]).sum() > 0 else "历史均值（仅作示意）",
        }
    report["checks"] = quality_checks(summary, panel)
    report["admitted"] = all(c["ok"] for c in report["checks"] if c["required"])
    report["panel"] = {**panel.describe(), "synthetic": panel.synthetic, "warnings": panel.warnings[:6]}
    return report


def quality_checks(s: dict[str, Any], panel: Any) -> list[dict[str, Any]]:
    out = [
        {
            "key": "beat_ew",
            "label": "风险调整后不输等权",
            "required": True,
            "ok": (s.get("beat_ew") or 0) >= -0.05,
            "detail": f"夏普 {s.get('sharpe') or 0:.2f}，等权 {s.get('ew_sharpe') or 0:.2f}",
        },
        {
            "key": "halves",
            "label": "前后两半都不差",
            "required": False,
            "ok": (s.get("sharpe_h1") or 0) > -0.5 and (s.get("sharpe_h2") or 0) > -0.5,
            "detail": f"前半夏普 {s.get('sharpe_h1') or 0:.2f}，后半 {s.get('sharpe_h2') or 0:.2f}",
        },
        {
            "key": "concentration",
            "label": "没有过度集中",
            "required": False,
            "ok": (s.get("effective_n") or 0) >= 2.5,
            "detail": f"平均有效持仓数 {s.get('effective_n') or 0:.1f}（风险资产权重归一后 1 / Σw²，现金不计）",
        },
        {
            "key": "turnover",
            "label": "换手可承受",
            "required": False,
            "ok": (s.get("turnover") or 0) <= 0.6,
            "detail": f"平均每次调仓换手 {(s.get('turnover') or 0):.0%}",
        },
    ]
    if panel.synthetic:
        out.append(
            {
                "key": "data",
                "label": "使用真实数据",
                "required": False,
                "ok": False,
                "detail": "含合成行情，只能验证流程",
            }
        )
    return out


# ---------------------------------------------------------------------------
# 实时调用：策略里的 optimize()
# ---------------------------------------------------------------------------
def optimize_live(
    config: dict[str, Any], closes: dict[str, list[float]], expected: dict[str, float], *, timeframe: str | None = None
) -> dict[str, float]:
    cfg = normalize_config(config)
    tf = timeframe or str(config.get("timeframe") or "4h")
    syms = [s for s, c in closes.items() if len(c) >= 30]
    if len(syms) < 2:
        return {s: 1.0 for s in syms}
    L = min(len(closes[s]) for s in syms)
    C = np.array([closes[s][-L:] for s in syms], dtype=float).T
    with np.errstate(invalid="ignore", divide="ignore"):
        R = C[1:] / C[:-1] - 1
    R = np.nan_to_num(R)
    per_bar, per_day = bars_per_year(tf), bars_per_day(tf)
    if expected:
        sig = np.array([expected.get(s, np.nan) for s in syms], dtype=float)
        mu = grinold_alpha(sig, R.std(axis=0), 0.05)
    else:
        mu = expected_at(
            {**cfg, "expected": cfg["expected"] if cfg["expected"] in EXPECTED_SOURCES else "none"}, R, None, 0.0
        )
    sol = solve(cfg, R, syms, mu, per_bar=per_bar)
    w, _ = overlay(sol["w"], sol["cov"], cfg["risk"], cfg["constraints"], per_bar=per_bar, per_day=per_day)
    return {s: round(float(x), 6) for s, x in zip(syms, w, strict=True)}


def risk_check(weights: dict[str, float], panel: Any, *, lookback: int = 180) -> dict[str, Any]:
    """对一组给定权重做风险体检：波动、VaR/CVaR、风险贡献、相关性、压力测试。"""
    syms = [s for s in panel.symbols if s in weights]
    if not syms:
        raise PortfolioError("权重里的标的不在数据里")
    cols = [panel.symbols.index(s) for s in syms]
    close = panel.data["close"][:, cols]
    with np.errstate(invalid="ignore", divide="ignore"):
        R = close[1:] / close[:-1] - 1
    R = np.nan_to_num(R[-lookback:])
    w = np.array([float(weights[s]) for s in syms])
    S = covariance(R, "ledoit_wolf")
    per_bar, per_day = bars_per_year(panel.timeframe), bars_per_day(panel.timeframe)
    pr = R @ w
    v95, c95 = var_cvar(pr, 0.95)
    vol_bar = math.sqrt(max(float(w @ S @ w), 0))
    return {
        "weights": {s: float(x) for s, x in zip(syms, w, strict=True)},
        "ann_vol": vol_bar * math.sqrt(per_bar),
        "var_95_bar": v95,
        "cvar_95_bar": c95,
        "var_95_day_param": 1.645 * vol_bar * math.sqrt(per_day),
        "risk_contrib": {s: round(float(x), 4) for s, x in zip(syms, risk_contribution(w, S), strict=True)},
        "stress": stress_test(
            {s: float(x) for s, x in zip(syms, w, strict=True)},
            R,
            syms,
            closes=close,
            times=panel.times,
            S=S,
            per_day=per_day,
        ),
        "panel": {**panel.describe(), "synthetic": panel.synthetic},
    }


__all__ = [
    "DEFAULT_CONFIG",
    "EXPECTED_SOURCES",
    "OPTIMIZERS",
    "RISK_MODELS",
    "SCENARIOS",
    "PortfolioError",
    "apply_constraints",
    "black_litterman",
    "covariance",
    "efficient_frontier",
    "evaluate_scheme",
    "hrp",
    "ledoit_wolf",
    "normalize_config",
    "optimize_live",
    "risk_check",
    "risk_contribution",
    "risk_parity",
    "solve",
    "stress_test",
    "var_cvar",
]
