"""多因子的多重共线性诊断。

几十个因子放在一起时，高度相关的因子会让组合权重不稳定、重复计入同一类风险、夸大「独立证据」。
这里对一组因子表达式在同一面板上计算：

- 两两相关（截面 rank 后按时间汇总的 Spearman 相关，均值）；
- VIF（方差膨胀因子 = 相关矩阵逆的对角线），> 5 提示共线、> 10 严重；
- 条件数（相关矩阵最大 / 最小特征值之比的平方根），> 30 提示整体共线；
- 高相关簇（|ρ| ≥ 阈值的连通分量）；
- 按 |Rank IC| 优先保留、逐个剔除 VIF 最高者，给出建议保留集合；
- 处理建议：簇内择优 / 等权合成 / 对已保留因子做残差正交化 / PCA。
"""

from __future__ import annotations

from typing import Any

import numpy as np

from . import factor_expr as FX
from .factor_eval import _rank_rows, forward_returns


def _cs_rank(x: np.ndarray) -> np.ndarray:
    return _rank_rows(x)


def _pooled_corr(a: np.ndarray, b: np.ndarray) -> float | None:
    """逐时间截面 rank 相关的均值；单标的时退化为时间序列相关。"""
    if a.shape[1] >= 3:
        vals = []
        for i in range(a.shape[0]):
            x, y = a[i], b[i]
            m = np.isfinite(x) & np.isfinite(y)
            if m.sum() >= 3:
                xs, ys = x[m], y[m]
                if xs.std() > 0 and ys.std() > 0:
                    vals.append(float(np.corrcoef(xs, ys)[0, 1]))
        return float(np.mean(vals)) if vals else None
    x, y = a.ravel(), b.ravel()
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 10 or x[m].std() == 0 or y[m].std() == 0:
        return None
    return float(np.corrcoef(x[m], y[m])[0, 1])


def diagnose(
    panel: Any,
    factors: list[dict[str, str]],
    *,
    horizon: int = 1,
    threshold: float = 0.7,
    vif_limit: float = 5.0,
) -> dict[str, Any]:
    """factors: [{"name": ..., "expr": ...}]，返回诊断结果（可直接给模型看）。"""
    names: list[str] = []
    mats: list[np.ndarray] = []
    errors: list[str] = []
    for i, f in enumerate(factors[:60]):
        name = str(f.get("name") or f"f{i + 1}")[:40]
        try:
            raw = FX.evaluate(FX.parse(str(f.get("expr") or "")), panel.data)
        except Exception as exc:  # 表达式错误不影响其他因子
            errors.append(f"{name}：{str(exc)[:120]}")
            continue
        mat = _cs_rank(raw) if raw.shape[1] >= 3 else raw
        if not np.isfinite(mat).any():
            errors.append(f"{name}：全部为空值")
            continue
        names.append(name)
        mats.append(mat)
    k = len(names)
    if k < 2:
        return {"ok": False, "error": "至少需要 2 个可计算的因子", "errors": errors}
    corr = np.eye(k)
    for i in range(k):
        for j in range(i + 1, k):
            c = _pooled_corr(mats[i], mats[j])
            corr[i, j] = corr[j, i] = 0.0 if c is None else c
    # IC：与下一期收益的相关，用来决定簇内保留谁
    fwd = forward_returns(panel.data["close"], horizon)
    fwd_r = _cs_rank(fwd) if fwd.shape[1] >= 3 else fwd
    ics = [_pooled_corr(m, fwd_r) for m in mats]
    try:
        inv = _inv(corr)
        vif = [float(max(1.0, inv[i, i])) for i in range(k)]
        eig = np.linalg.eigvalsh(corr)
        cond = float(np.sqrt(max(eig) / max(min(eig), 1e-9)))
    except np.linalg.LinAlgError:
        vif, cond = [float("inf")] * k, float("inf")
    # 高相关簇
    parent = list(range(k))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    for i in range(k):
        for j in range(i + 1, k):
            if abs(corr[i, j]) >= threshold:
                parent[find(i)] = find(j)
                pairs.append({"a": names[i], "b": names[j], "corr": round(float(corr[i, j]), 3)})
    groups: dict[int, list[int]] = {}
    for i in range(k):
        groups.setdefault(find(i), []).append(i)
    clusters = [[names[i] for i in g] for g in groups.values() if len(g) > 1]
    # 贪心剔除：每次去掉 VIF 最高且 |IC| 较低的因子，直到所有 VIF ≤ 上限
    keep = list(range(k))
    dropped: list[dict[str, Any]] = []
    while len(keep) > 1:
        sub = corr[np.ix_(keep, keep)]
        sub_vif = np.diag(_inv(sub))
        worst = int(np.argmax(sub_vif - 0.5 * np.array([abs(ics[i] or 0.0) for i in keep])))
        if sub_vif.max() <= vif_limit:
            break
        idx = keep.pop(worst)
        dropped.append({"name": names[idx], "vif": round(float(sub_vif[worst]), 2), "rank_ic": _r(ics[idx])})
    severity = "严重" if cond > 30 or max(vif) > 10 else "中等" if max(vif) > vif_limit or clusters else "低"
    return {
        "ok": True,
        "factors": [{"name": names[i], "vif": round(vif[i], 2), "rank_ic": _r(ics[i])} for i in range(k)],
        "corr": {"names": names, "matrix": [[round(float(x), 3) for x in row] for row in corr]},
        "condition_number": round(cond, 1),
        "high_corr_pairs": sorted(pairs, key=lambda p: -abs(p["corr"]))[:30],
        "clusters": clusters,
        "suggest_keep": [names[i] for i in keep],
        "suggest_drop": dropped,
        "severity": severity,
        "advice": [
            "同一簇内的因子在讲同一件事：保留 |Rank IC| 最高、经济含义最清楚的一个，或等权合成为一个复合因子",
            "保留多个相关因子时，按顺序对已保留因子做截面回归取残差（正交化），再进入模型",
            "因子很多时可用 PCA / 聚类降维，或在模型里用 L1/L2 正则（Lasso / Ridge）稳定权重",
            "多个相关因子同时显著不等于多份独立证据；样本外仍需逐个验证",
        ],
        "panel": {
            k2: v
            for k2, v in panel.describe().items()
            if k2 in ("bars", "symbols", "timeframe", "sources", "synthetic")
        },
        "errors": errors,
    }


def _inv(c: np.ndarray) -> np.ndarray:
    """相关矩阵求逆：完全重复的因子会让矩阵奇异，加一点岭项让 VIF 变得极大而不是被伪逆抹平。"""
    return np.linalg.inv(c + np.eye(len(c)) * 1e-6)


def _r(v: float | None) -> float | None:
    return None if v is None else round(float(v), 4)
