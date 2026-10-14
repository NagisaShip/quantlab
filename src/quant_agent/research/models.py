"""模型研究：数据集构建 → 带 purge 的滚动训练 → 样本外评估 → 泄漏检查 → 模型卡。

口径
----
- **特征**：因子库里的因子或任意因子表达式，在 t 收盘时计算；截面口径（标的数 ≥ 4）每期做截面 z 分数并在 ±3
  处截尾，时序口径用过去 100 根的滚动 z 分数。都只用 t 及以前的数据。
- **标签**：未来收益 / 截面收益排名 / 涨跌方向 / 三重障碍（止盈、止损、超时，出自 López de Prado）/ 未来波动率。
  标签覆盖 (t, t+h]，所以训练集里 t 必须早于测试段起点 h 根以上（purge）。
- **滚动训练**：时间轴切成 folds+1 段，第 k 段用前 k 段训练（扩张窗口），训练集末尾 15% 做早停验证，
  也同样 purge。拼起来的样本外预测覆盖后 folds 段，所有指标都只在样本外预测上算。
- **基线**：线性模型（岭回归 / 逻辑回归），线性模型自己的基线是“特征等权合成”。跑不赢基线就不算进展。
- **泄漏检查**：①截断面板重算特征，和全量面板在同一时刻的值必须一致（发现用到未来数据的特征）；
  ②把训练标签随机打乱重训，样本外 IC 必须接近 0；③purge 长度不小于持有期。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .factor_eval import _rank_rows, _row_corr, evaluate_matrix, forward_returns
from .factor_expr import FactorExprError, _quiet, evaluate, fields_used, max_lookback, parse
from .learners import LEARNERS, Learner, LearnerError, load_learner, make_learner
from .panel import Panel

LABELS: dict[str, dict[str, Any]] = {
    "fwd_ret": {"label": "未来收益", "task": "reg", "hint": "从 t 收盘到 t+h 收盘的收益；截面口径下按期标准化"},
    "cs_rank": {"label": "截面收益排名", "task": "reg", "hint": "同一时刻各币未来收益的排名（0~1），对极端值不敏感"},
    "direction": {"label": "涨跌方向", "task": "clf", "hint": "未来 h 期收益 > 0 记 1；截面口径下是跑赢中位数记 1"},
    "triple_barrier": {
        "label": "三重障碍",
        "task": "clf",
        "hint": "先碰到 +k·σ 止盈记 1，先碰到 −k·σ 止损记 0，超时按收益正负",
    },
    "volatility": {"label": "未来波动率", "task": "reg", "hint": "未来 h 期收益的均方根，用于风控与仓位"},
}

DEFAULT_FEATURES: list[dict[str, str]] = [
    {"name": "短期反转", "expression": "-returns(3)"},
    {"name": "中期动量", "expression": "returns(30) - returns(3)"},
    {"name": "低波动", "expression": "-ts_std(ret, 30)"},
    {"name": "非流动性", "expression": "ts_mean(abs(ret) / log(amount + 1), 30)"},
    {"name": "量价相关", "expression": "-ts_corr(close, volume, 20)"},
    {"name": "最大单期收益", "expression": "-ts_max(ret, 42)"},
]
HAR_FEATURES = [
    {"name": "RV1", "expression": "abs(ret)"},
    {"name": "RV6", "expression": "sqrt(ts_mean(ret * ret, 6))"},
    {"name": "RV24", "expression": "sqrt(ts_mean(ret * ret, 24))"},
]
MAX_FEATURES = 24


class ModelError(RuntimeError):
    """规格有误或数据不足。"""


class Stopped(RuntimeError):
    """训练被取消。"""


# ---------------------------------------------------------------------------
# 规格
# ---------------------------------------------------------------------------
def normalize_spec(
    raw: dict[str, Any], *, resolve: Callable[[str], dict[str, Any] | None] | None = None
) -> dict[str, Any]:
    """规范化训练规格。resolve(factor_id 或名字) → 因子库记录，用于把引用固化成表达式快照。"""
    kind = str(raw.get("model") or "ridge")
    if kind not in LEARNERS:
        raise ModelError(f"未知模型 {kind}")
    label = str(raw.get("label") or ("volatility" if LEARNERS[kind]["family"] == "vol" else "fwd_ret"))
    if label not in LABELS:
        raise ModelError(f"未知标签 {label}")
    if LEARNERS[kind]["family"] == "vol" and label != "volatility":
        raise ModelError(f"{LEARNERS[kind]['label']} 只能预测波动率，请把标签改成「未来波动率」")
    feats: list[dict[str, str]] = []
    for f in raw.get("features") or []:
        if isinstance(f, str):
            f = {"expression": f}
        ref = f.get("factor_id") or f.get("ref")
        if ref and resolve:
            rec = resolve(str(ref))
            if rec is None:
                raise ModelError(f"因子库里没有 {ref}")
            f = {"name": rec["name"], "expression": rec["expression"], "factor_id": rec["id"]}
        expr = str(f.get("expression") or "").strip()
        if not expr:
            continue
        try:
            expr = str(parse(expr))
        except FactorExprError as exc:
            raise ModelError(f"特征 {f.get('name') or expr[:30]} 有误：{exc}") from exc
        if any(x["expression"] == expr for x in feats):
            continue
        feats.append(
            {
                "name": str(f.get("name") or expr[:24])[:40],
                "expression": expr,
                **({"factor_id": f["factor_id"]} if f.get("factor_id") else {}),
            }
        )
    if kind == "har":
        feats = [*HAR_FEATURES, *[f for f in feats if f["expression"] not in {h["expression"] for h in HAR_FEATURES}]]
    if kind == "garch":
        feats = feats or [HAR_FEATURES[1]]
    if not feats:
        feats = [dict(f) for f in DEFAULT_FEATURES]
    if len(feats) > MAX_FEATURES:
        raise ModelError(f"特征最多 {MAX_FEATURES} 个")
    params = raw.get("params") or {}
    return {
        "model": kind,
        "label": label,
        "features": feats,
        "horizon": max(1, min(int(raw.get("horizon") or 1), 60)),
        "params": {k: v for k, v in params.items() if k in LEARNERS[kind]["params"]},
        "symbols": [s.strip().upper() for s in raw.get("symbols") or [] if str(s).strip()],
        "timeframe": raw.get("timeframe") or "4h",
        "bars_limit": max(300, min(int(raw.get("bars_limit") or 1500), 20_000)),
        "start": raw.get("start"),
        "end": raw.get("end"),
        "source": raw.get("source") or "auto",
        "cost_bps": float(raw.get("cost_bps") if raw.get("cost_bps") is not None else 10.0),
        "folds": max(2, min(int(raw.get("folds") or 4), 8)),
        "seed": int(raw.get("seed") if raw.get("seed") is not None else 7),
        "barrier_k": float(raw.get("barrier_k") or 1.5),
        "shuffle_test": bool(raw.get("shuffle_test", True)),
    }


def spec_hash(spec: dict[str, Any]) -> str:
    keep = {
        k: spec.get(k)
        for k in (
            "model",
            "label",
            "features",
            "horizon",
            "params",
            "symbols",
            "timeframe",
            "bars_limit",
            "start",
            "end",
            "folds",
            "seed",
            "barrier_k",
        )
    }
    keep["features"] = [f["expression"] for f in spec.get("features") or []]
    return hashlib.sha1(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def fields_for(spec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for f in spec["features"]:
        for x in fields_used(parse(f["expression"])):
            if x not in out:
                out.append(x)
    return out


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------
@dataclass
class Dataset:
    panel: Panel
    X: np.ndarray  # [T, N, F] 标准化后的特征（NaN = 不可用）
    y: np.ndarray  # [T, N] 训练标签
    R: np.ndarray  # [T, N] 未来 h 期收益（评估用）
    task: str
    horizon: int
    warm: int
    feature_names: list[str]
    cross: bool
    label: str = ""
    rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))  # 特征齐全的行（展平索引）
    t_rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    n_rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    y_rows: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def symbols(self) -> list[str]:
        return self.panel.symbols

    def flat_X(self, idx: np.ndarray) -> np.ndarray:
        return self.X[self.t_rows[idx], self.n_rows[idx]]

    def seq_X(self, idx: np.ndarray, L: int) -> np.ndarray:
        T = self.X.shape[0]
        t = self.t_rows[idx]
        offs = np.arange(-L + 1, 1)
        tt = np.clip(t[:, None] + offs[None, :], 0, T - 1)
        out = self.X[tt, self.n_rows[idx][:, None]]
        out[(t[:, None] + offs[None, :]) < 0] = 0.0
        return np.nan_to_num(out)

    def labelled(self) -> np.ndarray:
        return np.where(np.isfinite(self.y_rows))[0]


def _cs_z(F: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"), _quiet():
        m = np.nanmean(F, axis=1, keepdims=True)
        s = np.nanstd(F, axis=1, keepdims=True)
        z = (F - m) / np.where(s > 0, s, np.nan)
    return np.clip(z, -3, 3)


def _ts_z(F: np.ndarray, window: int = 100) -> np.ndarray:
    T = F.shape[0]
    out = np.full_like(F, np.nan)
    with np.errstate(all="ignore"), _quiet():
        for t in range(20, T):
            hist = F[max(0, t - window) : t + 1]
            m, s = np.nanmean(hist, axis=0), np.nanstd(hist, axis=0)
            out[t] = (F[t] - m) / np.where(s > 0, s, np.nan)
    return np.clip(out, -3, 3)


def _label(panel: Panel, kind: str, h: int, cross: bool, barrier_k: float) -> tuple[np.ndarray, np.ndarray]:
    close = panel.data["close"]
    R = forward_returns(close, h)
    T, N = R.shape
    if kind == "fwd_ret":
        y = _cs_z(R) if cross else R / (np.nanstd(R) or 1.0)
    elif kind == "cs_rank":
        rk = _rank_rows(R)
        with np.errstate(all="ignore"):
            cnt = np.sum(np.isfinite(R), axis=1, keepdims=True)
            y = rk / np.maximum(cnt - 1, 1)
    elif kind == "direction":
        with np.errstate(all="ignore"), _quiet():
            ref = np.nanmedian(R, axis=1, keepdims=True) if cross else 0.0
            y = np.where(np.isfinite(R), (R > ref).astype(float), np.nan)
    elif kind == "triple_barrier":
        ret = panel.data["ret"]
        y = np.full((T, N), np.nan)
        with np.errstate(all="ignore"), _quiet():
            sig = np.full((T, N), np.nan)
            for t in range(20, T):
                sig[t] = np.nanstd(ret[t - 20 : t + 1], axis=0)
            for t in range(T - h):
                base = close[t]
                up, dn = base * (1 + barrier_k * sig[t] * math.sqrt(h)), base * (1 - barrier_k * sig[t] * math.sqrt(h))
                path = close[t + 1 : t + h + 1]
                if not np.isfinite(base).any():
                    continue
                hit_up = np.where(path >= up, np.arange(1, h + 1)[:, None], h + 1).min(axis=0)
                hit_dn = np.where(path <= dn, np.arange(1, h + 1)[:, None], h + 1).min(axis=0)
                lab = np.where(hit_up < hit_dn, 1.0, np.where(hit_dn < hit_up, 0.0, (R[t] > 0).astype(float)))
                lab[~(np.isfinite(base) & np.isfinite(sig[t]) & np.isfinite(R[t]))] = np.nan
                y[t] = lab
    else:  # volatility
        ret = panel.data["ret"]
        y = np.full((T, N), np.nan)
        with np.errstate(all="ignore"), _quiet():
            for t in range(T - h):
                seg = ret[t + 1 : t + h + 1]
                y[t] = np.sqrt(np.nanmean(seg**2, axis=0))
                y[t][np.sum(np.isfinite(seg), axis=0) < h] = np.nan
    return y, R


def build_dataset(spec: dict[str, Any], panel: Panel) -> Dataset:
    h = int(spec["horizon"])
    T, N = panel.data["close"].shape
    cross = N >= 4
    nodes = [parse(f["expression"]) for f in spec["features"]]
    raw = [evaluate(n, panel.data) for n in nodes]
    warm = max([max_lookback(n) for n in nodes] + [20])
    vol_label = spec["label"] == "volatility"
    if vol_label:  # 波动率是绝对量，不能做截面标准化；时序上也只做缩放
        feats = [np.where(np.isfinite(F), F, np.nan) for F in raw]
    else:
        feats = [_cs_z(F) if cross else _ts_z(F) for F in raw]
    X = np.stack(feats, axis=2) if feats else np.zeros((T, N, 0))
    X[:warm] = np.nan
    y, R = _label(panel, spec["label"], h, cross, float(spec.get("barrier_k") or 1.5))
    task = LABELS[spec["label"]]["task"]
    ok = np.all(np.isfinite(X), axis=2)
    flat = np.where(ok.ravel())[0]
    t_rows, n_rows = flat // N, flat % N
    ds = Dataset(
        panel=panel,
        X=X,
        y=y,
        R=R,
        task=task,
        horizon=h,
        warm=warm,
        feature_names=[f["name"] for f in spec["features"]],
        cross=cross,
        label=spec["label"],
        rows=flat,
        t_rows=t_rows.astype(int),
        n_rows=n_rows.astype(int),
        y_rows=y.ravel()[flat],
    )
    if len(ds.labelled()) < 200:
        raise ModelError(
            f"可用样本只有 {len(ds.labelled())} 个（特征与标签都齐全的 时刻×标的），至少需要 200 个。"
            "加长区间或减少回看很长的特征"
        )
    return ds


# ---------------------------------------------------------------------------
# 滚动训练
# ---------------------------------------------------------------------------
def folds(ds: Dataset, n_folds: int) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, int, int]]:
    """返回 [(train_idx, val_idx, test_idx, test_lo, test_hi)]，idx 为 Dataset 行号。"""
    lab = ds.labelled()
    t_lab = ds.t_rows[lab]
    T = ds.X.shape[0]
    start = ds.warm
    edges = (start + np.linspace(0, T - start, n_folds + 2)).astype(int)
    h = ds.horizon
    out = []
    for k in range(1, n_folds + 1):
        lo, hi = int(edges[k]), int(edges[k + 1])
        train_end = lo - h  # purge：标签覆盖 (t, t+h]，不能伸进测试段
        tr_all = lab[t_lab < train_end]
        test = np.where((ds.t_rows >= lo) & (ds.t_rows < hi))[0]
        if len(tr_all) < 100 or len(test) == 0:
            continue
        cut_t = int(np.quantile(ds.t_rows[tr_all], 0.85))
        tr = tr_all[ds.t_rows[tr_all] < cut_t - h]
        va = tr_all[ds.t_rows[tr_all] >= cut_t]
        if len(tr) < 80:
            tr, va = tr_all, tr_all[:0]
        out.append((tr, va, test, lo, hi))
    if not out:
        raise ModelError("样本太少，切不出滚动训练的窗口")
    return out


def _fold_ic(ds: Dataset, pred: np.ndarray, idx: np.ndarray) -> float | None:
    P = np.full(ds.y.shape, np.nan)
    P[ds.t_rows[idx], ds.n_rows[idx]] = pred
    target = ds.y if (ds.task == "reg" and not ds.cross) or ds.label == "volatility" else ds.R
    if ds.cross:
        rc = _row_corr(_rank_rows(P), _rank_rows(np.where(np.isfinite(P), target, np.nan)))
        rc = rc[np.isfinite(rc)]
        return float(np.mean(rc)) if len(rc) else None
    a, b = P.ravel(), target.ravel()
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 10:
        return None
    ra, rb = _rank_rows(a[m][None, :])[0], _rank_rows(b[m][None, :])[0]
    c = np.corrcoef(ra, rb)[0, 1]
    return float(c) if np.isfinite(c) else None


def walk_forward(
    spec: dict[str, Any],
    ds: Dataset,
    *,
    kind: str | None = None,
    shuffle: bool = False,
    stop: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
    epochs_cap: int | None = None,
) -> dict[str, Any]:
    kind = kind or spec["model"]
    params = dict(spec["params"]) if kind == spec["model"] else {}
    if epochs_cap and "epochs" in LEARNERS[kind]["params"]:
        params["epochs"] = min(int(params.get("epochs") or LEARNERS[kind]["params"]["epochs"]), epochs_cap)
    rng = np.random.default_rng(spec["seed"] + 99)
    P = np.full(ds.y.shape, np.nan)
    fold_rows, curves, last_model, last_test = [], [], None, None
    y_backup = ds.y_rows.copy()
    y_perm = None
    if shuffle:  # 整体打乱一次：打乱后的标签和特征无关，训练与测试都用它；能预测它只可能是流程泄漏（记忆了测试样本）
        lab = ds.labelled()
        y_perm = y_backup.copy()
        y_perm[lab] = y_backup[lab][rng.permutation(len(lab))]
        ds.y_rows = y_perm
    try:
        for k, (tr, va, test, lo, hi) in enumerate(folds(ds, spec["folds"]), start=1):
            if stop:
                stop()
            if progress:
                progress(f"{LEARNERS[kind]['label']} · 第 {k}/{spec['folds']} 段")
            m = make_learner(kind, params, ds.task, spec["seed"])
            curve = m.fit(ds, tr, va, stop=stop)
            pred = m.predict(ds, test)
            P[ds.t_rows[test], ds.n_rows[test]] = pred
            fold_rows.append(
                {
                    "fold": k,
                    "train": len(tr),
                    "val": len(va),
                    "test": len(test),
                    "start": int(ds.panel.times[lo]),
                    "end": int(ds.panel.times[min(hi, len(ds.panel.times)) - 1]),
                    "rank_ic": _fold_ic(ds, pred, test),
                }
            )
            curves.append(curve)
            last_model, last_test = m, test
    finally:
        ds.y_rows = y_backup
    return {"P": P, "folds": fold_rows, "curves": curves, "model": last_model, "last_test": last_test, "y_perm": y_perm}


def _perm_ic(ds: Dataset, P: np.ndarray, y_rows: np.ndarray) -> float | None:
    """预测与（打乱后的）标签的秩相关：截面口径逐期平均，时序口径整体计算。"""
    Y = np.full(ds.y.shape, np.nan)
    Y[ds.t_rows, ds.n_rows] = y_rows
    Y = np.where(np.isfinite(P), Y, np.nan)
    if ds.cross:
        rc = _row_corr(_rank_rows(np.where(np.isfinite(Y), P, np.nan)), _rank_rows(Y))
        rc = rc[np.isfinite(rc)]
        return float(np.mean(rc)) if len(rc) else None
    a, b = P.ravel(), Y.ravel()
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 10:
        return None
    c = np.corrcoef(_rank_rows(a[m][None, :])[0], _rank_rows(b[m][None, :])[0])[0, 1]
    return float(c) if np.isfinite(c) else None


def _equal_weight(ds: Dataset, spec: dict[str, Any]) -> np.ndarray:
    """线性模型的基线：特征等权合成，方向按第一个训练窗口里各自与标签的相关性确定（不偷看样本外）。"""
    fs = folds(ds, spec["folds"])
    tr = fs[0][0]
    X, y = ds.flat_X(tr), ds.y_rows[tr]
    signs = []
    for j in range(X.shape[1]):
        c = np.corrcoef(X[:, j], y)[0, 1] if np.std(X[:, j]) > 0 else 0.0
        signs.append(1.0 if not np.isfinite(c) or c >= 0 else -1.0)
    P = np.full(ds.y.shape, np.nan)
    for _, _, test, _, _ in fs:
        P[ds.t_rows[test], ds.n_rows[test]] = ds.flat_X(test) @ np.array(signs) / max(len(signs), 1)
    return P


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------
def _psi(a: np.ndarray, b: np.ndarray, bins: int = 10) -> float | None:
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if len(a) < 50 or len(b) < 20:
        return None
    edges = np.unique(np.quantile(a, np.linspace(0, 1, bins + 1)[1:-1]))
    pa = np.bincount(np.searchsorted(edges, a), minlength=len(edges) + 1) / len(a)
    pb = np.bincount(np.searchsorted(edges, b), minlength=len(edges) + 1) / len(b)
    pa, pb = np.clip(pa, 1e-4, None), np.clip(pb, 1e-4, None)
    return float(np.sum((pb - pa) * np.log(pb / pa)))


def lookahead_check(spec: dict[str, Any], panel: Panel) -> list[dict[str, Any]]:
    """截断面板重算：同一时刻的特征值在“只看到这一刻为止”和“看到全部数据”时必须一样。"""
    T = len(panel.times)
    cuts = [int(T * q) for q in (0.5, 0.75, 0.95) if int(T * q) > 60]
    out = []
    for f in spec["features"]:
        node = parse(f["expression"])
        full = evaluate(node, panel.data)
        worst = 0.0
        for c in cuts:
            part = evaluate(node, panel.slice(0, c + 1).data)
            a, b = part[-1], full[c]
            m = np.isfinite(a) & np.isfinite(b)
            if m.any():
                worst = max(worst, float(np.max(np.abs(a[m] - b[m]) / (np.abs(b[m]) + 1e-9))))
            if np.isfinite(a).sum() != np.isfinite(b).sum():
                worst = max(worst, 1.0)
        out.append({"name": f["name"], "ok": worst < 1e-6, "max_diff": worst})
    return out


def permutation_importance(ds: Dataset, model: Learner, test: np.ndarray, seed: int) -> list[dict[str, Any]]:
    base = _fold_ic(ds, model.predict(ds, test), test)
    if base is None:
        return []
    rng = np.random.default_rng(seed + 5)
    out = []
    X_backup = ds.X
    for j, name in enumerate(ds.feature_names):
        Xp = X_backup.copy()
        tt = np.unique(ds.t_rows[test])
        col = Xp[tt, :, j]
        Xp[tt, :, j] = col.ravel()[rng.permutation(col.size)].reshape(col.shape)
        ds.X = Xp
        try:
            ic = _fold_ic(ds, model.predict(ds, test), test)
        finally:
            ds.X = X_backup
        out.append({"name": name, "drop": None if ic is None else float(base - ic)})
    out.sort(key=lambda r: -(r["drop"] or 0))
    return out


def _calibration(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict[str, Any]]:
    m = np.isfinite(p) & np.isfinite(y)
    p, y = p[m], y[m]
    out = []
    for k in range(bins):
        lo, hi = k / bins, (k + 1) / bins
        sel = (p >= lo) & ((p < hi) if k < bins - 1 else (p <= hi))
        if sel.sum() >= 10:
            out.append(
                {
                    "bin": round((lo + hi) / 2, 2),
                    "pred": float(p[sel].mean()),
                    "actual": float(y[sel].mean()),
                    "n": int(sel.sum()),
                }
            )
    return out


def _vol_metrics(P: np.ndarray, Y: np.ndarray, naive: np.ndarray) -> dict[str, Any]:
    m = np.isfinite(P) & np.isfinite(Y) & (P > 0) & (Y > 0)
    if m.sum() < 30:
        return {}
    p, y = P[m], Y[m]
    corr = float(np.corrcoef(p, y)[0, 1])
    qlike = float(np.mean(y**2 / p**2 - np.log(y**2 / p**2) - 1))
    r2 = 1 - float(np.sum((y - p) ** 2) / np.sum((y - y.mean()) ** 2))
    mn = m & np.isfinite(naive) & (naive > 0)
    naive_corr = float(np.corrcoef(naive[mn], Y[mn])[0, 1]) if mn.sum() > 30 else None
    return {"vol_corr": corr, "qlike": qlike, "vol_r2": r2, "naive_corr": naive_corr}


def train_and_evaluate(
    spec: dict[str, Any],
    panel: Panel,
    *,
    stop: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
    trials: int = 1,
) -> dict[str, Any]:
    """完整的一次训练：滚动样本外 + 基线 + 泄漏检查 + 重要性 + 漂移 + 最终模型。返回报告与模型状态。"""
    t0 = time.time()
    ds = build_dataset(spec, panel)
    kind = spec["model"]
    family = LEARNERS[kind]["family"]
    wf = walk_forward(spec, ds, stop=stop, progress=progress)
    P = wf["P"]
    h = ds.horizon
    vol = spec["label"] == "volatility"

    # ---- 基线 ----
    if progress:
        progress("训练基线")
    if vol:
        naive = np.sqrt(np.clip(evaluate(parse("ts_mean(ret * ret, 24)"), panel.data), 0, None))
        baseline_P = naive
        baseline_name = "过去 24 根的已实现波动率"
    elif kind == "ridge":
        baseline_P = _equal_weight(ds, spec)
        naive = None
        baseline_name = "特征等权合成"
    else:
        baseline_P = walk_forward(spec, ds, kind="ridge", stop=stop)["P"]
        naive = None
        baseline_name = "线性模型"

    # ---- 样本外指标 ----
    signal = P if not vol else None
    report: dict[str, Any] = {}
    if signal is not None:
        rep = evaluate_matrix(signal, panel, warm=ds.warm, horizon=h, cost_bps=float(spec["cost_bps"]), trials=trials)
        base_rep = evaluate_matrix(baseline_P, panel, warm=ds.warm, horizon=h, cost_bps=float(spec["cost_bps"]))
        s = rep["summary"]
        oos = {
            "oos_rank_ic": s["rank_ic"],
            "oos_ic": s["ic"],
            "oos_icir": s["icir"],
            "oos_ic_t": s["ic_t"],
            "ls_sharpe": s["ls_sharpe"],
            "ls_return": s["ls_return"],
            "turnover": s["turnover"],
            "coverage": s["coverage"],
            "t_required": s["t_required"],
            "mode": s["mode"],
            "baseline_rank_ic": base_rep["summary"]["rank_ic"],
            "baseline_ls_sharpe": base_rep["summary"]["ls_sharpe"],
            "stability": s["oos_retention"],
        }
        report.update(
            decay=rep["decay"],
            regimes=rep["regimes"],
            groups=rep["groups"],
            ls_nav=rep["ls_nav"],
            ic_series=rep["ic_series"],
        )
    else:
        vm = _vol_metrics(P, ds.y, naive)  # type: ignore[arg-type]
        oos = {
            "oos_rank_ic": vm.get("vol_corr"),
            "oos_ic": vm.get("vol_corr"),
            "qlike": vm.get("qlike"),
            "vol_r2": vm.get("vol_r2"),
            "baseline_rank_ic": vm.get("naive_corr"),
            "mode": "volatility",
        }
    beat = None
    if oos.get("oos_rank_ic") is not None and oos.get("baseline_rank_ic") is not None:
        beat = float(oos["oos_rank_ic"] - oos["baseline_rank_ic"])
    oos["beat_baseline"] = beat

    # 方向准确率、Brier、校准
    m = np.isfinite(P)
    if ds.task == "clf":
        yy = ds.y[m]
        pp = P[m]
        ok = np.isfinite(yy)
        oos["dir_acc"] = float(np.mean((pp[ok] > 0.5) == (yy[ok] > 0.5))) if ok.any() else None
        oos["brier"] = float(np.mean((pp[ok] - yy[ok]) ** 2)) if ok.any() else None
        report["calibration"] = _calibration(pp, yy)
    elif not vol:
        with np.errstate(all="ignore"), _quiet():
            pc = P - (np.nanmedian(P, axis=1, keepdims=True) if ds.cross else 0)
            rc = ds.R - (np.nanmedian(np.where(m, ds.R, np.nan), axis=1, keepdims=True) if ds.cross else 0)
        ok = m & np.isfinite(rc) & (pc != 0) & (rc != 0)
        oos["dir_acc"] = float(np.mean(np.sign(pc[ok]) == np.sign(rc[ok]))) if ok.any() else None
    folds_ic = [f["rank_ic"] for f in wf["folds"] if f["rank_ic"] is not None]
    oos["fold_positive"] = float(np.mean([x > 0 for x in folds_ic])) if folds_ic else None
    oos["folds"] = len(wf["folds"])

    # ---- 泄漏检查 ----
    if progress:
        progress("泄漏检查")
    look = lookahead_check(spec, panel)
    leak_items = [
        {
            "key": "lookahead",
            "label": "特征不使用未来数据",
            "ok": all(x["ok"] for x in look),
            "detail": "截断数据重算后完全一致"
            if all(x["ok"] for x in look)
            else "以下特征在截断数据上重算结果不同：" + "、".join(x["name"] for x in look if not x["ok"]),
            "required": True,
        },
        {
            "key": "purge",
            "label": "训练与测试之间留出持有期",
            "ok": True,
            "detail": f"每段训练集都在测试段起点前 {h} 根截止（标签覆盖未来 {h} 根）",
            "required": True,
        },
    ]
    shuffle_ic = None
    if spec.get("shuffle_test", True) and kind == "garch":
        leak_items.append(
            {
                "key": "shuffle",
                "label": "打乱标签后学不到东西",
                "ok": True,
                "required": True,
                "detail": "GARCH 只用收益序列本身估计，不使用标签训练，跳过这一项",
            }
        )
    elif spec.get("shuffle_test", True):
        if progress:
            progress("打乱标签重训")
        sh = walk_forward(spec, ds, shuffle=True, stop=stop, epochs_cap=10)
        shuffle_ic = _perm_ic(ds, sh["P"], sh["y_perm"])
        n_test = int(np.isfinite(sh["P"]).sum())
        tol = max(0.02, 4.0 / math.sqrt(max(n_test, 1)) * (3 if ds.cross else 1))
        ok_sh = shuffle_ic is None or abs(shuffle_ic) < tol
        leak_items.append(
            {
                "key": "shuffle",
                "label": "打乱标签后学不到东西",
                "ok": bool(ok_sh),
                "required": True,
                "detail": f"把标签整体打乱后重训，样本外预测与打乱标签的 RankIC = {shuffle_ic:.4f}"
                "（应接近 0；明显偏离说明训练与测试样本有重叠）"
                if shuffle_ic is not None
                else "样本不足，无法判断",
            }
        )
    oos["leak_passed"] = 1.0 if all(x["ok"] for x in leak_items if x["required"]) else 0.0
    oos["shuffle_rank_ic"] = shuffle_ic

    # ---- 重要性与漂移 ----
    importance = []
    if (wf["model"] is not None and family != "vol") or kind == "har":
        if progress:
            progress("计算特征重要性")
        importance = permutation_importance(ds, wf["model"], wf["last_test"], spec["seed"])
    psi = []
    lab = ds.labelled()
    if len(lab):
        cut = np.quantile(ds.t_rows, 0.9)
        old = ds.t_rows < np.quantile(ds.t_rows, 0.6)
        new = ds.t_rows >= cut
        for j, name in enumerate(ds.feature_names):
            Xj = ds.X[ds.t_rows, ds.n_rows, j]
            psi.append({"name": name, "psi": _psi(Xj[old], Xj[new])})
    oos["psi_max"] = max([p["psi"] for p in psi if p["psi"] is not None], default=None)

    # ---- 最终模型：用全部带标签的样本（末尾 15% 早停）----
    if progress:
        progress("训练最终模型")
    final = make_learner(kind, spec["params"], ds.task, spec["seed"])
    t_lab = ds.t_rows[lab]
    cut_t = int(np.quantile(t_lab, 0.85))
    tr = lab[t_lab < cut_t - h]
    va = lab[t_lab >= cut_t]
    curve = final.fit(ds, tr, va, stop=stop)
    latest = _latest_predictions(ds, final)

    summary = {
        **oos,
        "model": kind,
        "label": spec["label"],
        "task": ds.task,
        "horizon": h,
        "samples": len(lab),
        "features": len(ds.feature_names),
        "trials": int(trials),
        "oos_rank_ic_x100": (oos.get("oos_rank_ic") or 0) * 100,
        "seconds": round(time.time() - t0, 1),
        "backend": getattr(final, "backend", None),
    }
    checks = [*leak_items, *quality_checks(summary)]
    report.update(
        {
            "summary": summary,
            "checks": checks,
            "admitted": all(c["ok"] for c in checks if c["required"]),
            "folds": wf["folds"],
            "curve": _downsample(curve, 200),
            "fold_curves": [_downsample(c, 60) for c in wf["curves"]],
            "importance": importance,
            "coefficients": dict(zip(ds.feature_names, final.coefficients(), strict=True))
            if hasattr(final, "coefficients")
            else None,
            "psi": psi,
            "lookahead": look,
            "baseline": baseline_name,
            "latest": latest,
            "panel": panel.describe(),
            "spec": spec,
        }
    )
    return {"report": report, "state": final.state()}


def quality_checks(s: dict[str, Any]) -> list[dict[str, Any]]:
    def f(v: Any, pct: bool = False) -> str:
        return "—" if v is None else (f"{v * 100:.0f}%" if pct else f"{v:.3f}")

    out = [
        {
            "key": "baseline",
            "label": "跑赢基线",
            "required": True,
            "ok": s.get("beat_baseline") is not None and s["beat_baseline"] > 0,
            "detail": f"样本外 RankIC {f(s.get('oos_rank_ic'))}，基线 {f(s.get('baseline_rank_ic'))}",
        },
        {
            "key": "folds",
            "label": "各段样本外方向一致",
            "required": False,
            "ok": (s.get("fold_positive") or 0) >= 0.75,
            "detail": f"{f(s.get('fold_positive'), True)} 的滚动段 RankIC 为正（共 {s.get('folds')} 段）",
        },
        {
            "key": "drift",
            "label": "特征分布稳定（PSI）",
            "required": False,
            "ok": s.get("psi_max") is None or s["psi_max"] < 0.25,
            "detail": f"最大 PSI {f(s.get('psi_max'))}（> 0.25 说明近期特征分布明显变了）",
        },
    ]
    if s.get("mode") != "volatility" and s.get("oos_ic_t") is not None:
        out.append(
            {
                "key": "significance",
                "label": "样本外显著",
                "required": False,
                "ok": abs(s["oos_ic_t"]) >= (s.get("t_required") or 1.96),
                "detail": f"|t| = {abs(s['oos_ic_t']):.2f}，门槛 {s.get('t_required') or 1.96:.2f}",
            }
        )
    return out


def _downsample(xs: list[Any], n: int) -> list[Any]:
    if len(xs) <= n:
        return xs
    step = len(xs) / n
    return [xs[int(i * step)] for i in range(n)] + [xs[-1]]


def _latest_predictions(ds: Dataset, model: Learner) -> dict[str, Any]:
    T = ds.X.shape[0]
    idx = np.where(ds.t_rows == T - 1)[0]
    if not len(idx):
        return {}
    pred = model.predict(ds, idx)
    return {
        "t": int(ds.panel.times[-1]),
        "values": {ds.symbols[int(n)]: float(v) for n, v in zip(ds.n_rows[idx], pred, strict=True)},
    }


# ---------------------------------------------------------------------------
# 推理：库里的模型在新面板上预测（只给最新特征），以及回测用的滚动重训
# ---------------------------------------------------------------------------
def predict_panel(spec: dict[str, Any], state: dict[str, Any], panel: Panel) -> np.ndarray:
    """用冻结的模型在面板每一行上预测（不重训）。注意：在训练区间内的预测是样本内的。"""
    ds = _dataset_for_inference(spec, panel)
    model = load_learner(state)
    P = np.full(ds.y.shape, np.nan)
    if len(ds.rows):
        P[ds.t_rows, ds.n_rows] = model.predict(ds, np.arange(len(ds.rows)))
    return P


def rolling_predict(
    spec: dict[str, Any],
    panel: Panel,
    *,
    retrain_every: int | None = None,
    min_train: int | None = None,
    stop: Callable[[], None] | None = None,
) -> tuple[np.ndarray, dict]:
    """回测用：每 retrain_every 根 K 线用当时之前的数据重训一次，预测到下一次重训为止。
    返回 ([T, N] 预测，说明)。最早一段训练窗口内没有预测（NaN）。"""
    ds = _dataset_for_inference(spec, panel)
    T = ds.X.shape[0]
    h = ds.horizon
    every = int(retrain_every or max(50, (T - ds.warm) // 8))
    first = int(min_train or max(ds.warm + 200, ds.warm + (T - ds.warm) // 4))
    P = np.full(ds.y.shape, np.nan)
    lab = ds.labelled()
    t_lab = ds.t_rows[lab]
    params = dict(spec["params"])
    if "epochs" in params or "epochs" in LEARNERS[spec["model"]]["params"]:
        params["epochs"] = min(int(params.get("epochs") or LEARNERS[spec["model"]]["params"]["epochs"]), 25)
    n_fit = 0
    for lo in range(first, T, every):
        if stop:
            stop()
        tr_all = lab[t_lab < lo - h]
        if len(tr_all) < 100:
            continue
        cut_t = int(np.quantile(ds.t_rows[tr_all], 0.85))
        tr, va = tr_all[ds.t_rows[tr_all] < cut_t - h], tr_all[ds.t_rows[tr_all] >= cut_t]
        m = make_learner(spec["model"], params, ds.task, spec["seed"])
        m.fit(ds, tr if len(tr) >= 80 else tr_all, va if len(tr) >= 80 else tr_all[:0], stop=stop)
        test = np.where((ds.t_rows >= lo) & (ds.t_rows < min(T, lo + every)))[0]
        if len(test):
            P[ds.t_rows[test], ds.n_rows[test]] = m.predict(ds, test)
        n_fit += 1
    return P, {"retrain_every": every, "first": int(panel.times[min(first, T - 1)]) if T else None, "fits": n_fit}


def _dataset_for_inference(spec: dict[str, Any], panel: Panel) -> Dataset:
    try:
        return build_dataset(spec, panel)
    except ModelError:
        h = int(spec["horizon"])
        _T, N = panel.data["close"].shape
        cross = N >= 4
        nodes = [parse(f["expression"]) for f in spec["features"]]
        raw = [evaluate(n, panel.data) for n in nodes]
        vol_label = spec["label"] == "volatility"
        feats = raw if vol_label else [_cs_z(F) if cross else _ts_z(F) for F in raw]
        X = np.stack(feats, axis=2)
        warm = max([max_lookback(n) for n in nodes] + [20])
        X[:warm] = np.nan
        y, R = _label(panel, spec["label"], h, cross, float(spec.get("barrier_k") or 1.5))
        ok = np.all(np.isfinite(X), axis=2)
        flat = np.where(ok.ravel())[0]
        return Dataset(
            panel=panel,
            X=X,
            y=y,
            R=R,
            task=LABELS[spec["label"]]["task"],
            horizon=h,
            warm=warm,
            feature_names=[f["name"] for f in spec["features"]],
            cross=cross,
            label=spec["label"],
            rows=flat,
            t_rows=(flat // N).astype(int),
            n_rows=(flat % N).astype(int),
            y_rows=y.ravel()[flat],
        )


# ---------------------------------------------------------------------------
# 模型卡
# ---------------------------------------------------------------------------
def model_card(spec: dict[str, Any], report: dict[str, Any], *, name: str = "") -> dict[str, Any]:
    s = report.get("summary") or {}
    panel = report.get("panel") or {}
    weak: list[str] = []
    if (s.get("beat_baseline") or 0) <= 0:
        weak.append(f"样本外没有跑赢基线（{report.get('baseline')}）")
    for r in report.get("regimes") or []:
        if r.get("rank_ic") is not None and (s.get("oos_rank_ic") or 0) > 0 and r["rank_ic"] < 0:
            weak.append(f"{r['regime']}里方向相反（RankIC {r['rank_ic']:.3f}）")
    if (s.get("psi_max") or 0) >= 0.25:
        weak.append("近期特征分布明显漂移，需要重训或观察")
    if (s.get("fold_positive") or 1) < 0.75:
        weak.append("不同时间段表现不一致")
    if panel.get("synthetic"):
        weak.append("训练用到了合成数据，只能验证流程")
    if s.get("leak_passed") == 0:
        weak.append("泄漏检查没有通过，结果不可信")
    imp = [i["name"] for i in report.get("importance") or [] if (i.get("drop") or 0) > 0][:3]
    day = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000)) if ms else "—"  # noqa: E731
    return {
        "name": name,
        "purpose": f"预测 {spec['timeframe']} 周期上未来 {spec['horizon']} 根 K 线的{LABELS[spec['label']]['label']}",
        "model": f"{LEARNERS[spec['model']]['label']}" + (f"（{s.get('backend')}）" if s.get("backend") else ""),
        "params": spec.get("params") or {},
        "data": f"{len(panel.get('symbols') or [])} 个标的，{day(panel.get('start'))} 至 {day(panel.get('end'))}，"
        f"{panel.get('bars')} 根 K 线，{s.get('samples')} 个样本",
        "features": [f["name"] for f in spec["features"]],
        "validation": f"扩张窗口滚动训练 {s.get('folds')} 段，purge {spec['horizon']} 根，训练集末尾 15% 早停",
        "metrics": {
            k: s.get(k)
            for k in (
                "oos_rank_ic",
                "beat_baseline",
                "dir_acc",
                "brier",
                "ls_sharpe",
                "turnover",
                "fold_positive",
                "psi_max",
                "qlike",
            )
        },
        "drivers": imp,
        "weaknesses": weak or ["暂未发现明显弱点（仍需在实时模拟中观察）"],
        "seed": spec.get("seed"),
    }


__all__ = [
    "DEFAULT_FEATURES",
    "LABELS",
    "Dataset",
    "ModelError",
    "Stopped",
    "build_dataset",
    "fields_for",
    "lookahead_check",
    "model_card",
    "normalize_spec",
    "predict_panel",
    "quality_checks",
    "rolling_predict",
    "spec_hash",
    "train_and_evaluate",
    "walk_forward",
]
_ = LearnerError
