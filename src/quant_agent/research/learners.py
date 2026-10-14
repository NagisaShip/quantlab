"""模型研究用的学习器：岭回归 / 逻辑回归（基线）、梯度提升树、MLP、序列模型、HAR-RV、GARCH(1,1)。

统一接口（都在 ``Dataset`` 的行索引上工作，见 models.py）：

    m = make_learner(kind, params, task, seed)
    curve = m.fit(ds, train_idx, val_idx, stop=callable)   # 返回逐轮 {"epoch", "train", "val"}
    pred = m.predict(ds, idx)                               # [len(idx)]
    state = m.state(); m2 = load_learner(state)            # 可 JSON 序列化，存进模型库

- 只依赖 numpy。装了 LightGBM 时梯度提升树用 LightGBM，否则用内置的直方图版；
  装了 PyTorch 时开放 LSTM / GRU / TCN / Transformer（见 deep.py），没装会给出明确提示。
- 分类任务（涨跌方向、三重障碍）输出概率；回归任务输出连续预测。
"""

from __future__ import annotations

import base64
import math
from collections.abc import Callable
from typing import Any

import numpy as np

LEARNERS: dict[str, dict[str, Any]] = {
    "ridge": {
        "label": "线性基线",
        "hint": "回归用岭回归，分类用 L2 逻辑回归；每次训练都拿它当基线",
        "params": {"alpha": 1.0},
        "family": "linear",
    },
    "gbdt": {
        "label": "梯度提升树",
        "hint": "LightGBM（已安装时）或内置直方图版；擅长非线性与特征交互",
        "params": {
            "n_estimators": 200,
            "learning_rate": 0.05,
            "max_depth": 3,
            "min_leaf": 60,
            "subsample": 0.8,
            "patience": 20,
        },
        "family": "tree",
    },
    "mlp": {
        "label": "MLP 神经网络",
        "hint": "两层全连接，Adam 优化，按验证集早停",
        "params": {
            "hidden": [32, 16],
            "lr": 0.002,
            "epochs": 40,
            "batch": 256,
            "weight_decay": 0.0001,
            "patience": 6,
            "dropout": 0.1,
        },
        "family": "nn",
    },
    "lstm": {
        "label": "LSTM",
        "hint": "看过去 seq_len 根 K 线的特征序列，需要 PyTorch",
        "params": {
            "seq_len": 16,
            "hidden": 32,
            "layers": 1,
            "lr": 0.002,
            "epochs": 20,
            "batch": 256,
            "patience": 4,
            "dropout": 0.1,
        },
        "family": "seq",
        "needs": "torch",
    },
    "gru": {
        "label": "GRU",
        "hint": "比 LSTM 更轻的循环网络，需要 PyTorch",
        "params": {
            "seq_len": 16,
            "hidden": 32,
            "layers": 1,
            "lr": 0.002,
            "epochs": 20,
            "batch": 256,
            "patience": 4,
            "dropout": 0.1,
        },
        "family": "seq",
        "needs": "torch",
    },
    "tcn": {
        "label": "TCN 时序卷积",
        "hint": "膨胀因果卷积，需要 PyTorch",
        "params": {
            "seq_len": 16,
            "hidden": 32,
            "layers": 2,
            "lr": 0.002,
            "epochs": 20,
            "batch": 256,
            "patience": 4,
            "dropout": 0.1,
        },
        "family": "seq",
        "needs": "torch",
    },
    "transformer": {
        "label": "Transformer（PatchTST 风格）",
        "hint": "把序列切成小块做自注意力，需要 PyTorch",
        "params": {
            "seq_len": 16,
            "patch": 4,
            "d_model": 32,
            "heads": 4,
            "layers": 2,
            "lr": 0.001,
            "epochs": 20,
            "batch": 256,
            "patience": 4,
            "dropout": 0.1,
        },
        "family": "seq",
        "needs": "torch",
    },
    "har": {
        "label": "HAR-RV（波动率）",
        "hint": "用 1 / 6 / 24 根 K 线的已实现波动率做线性回归，只用于波动率标签",
        "params": {"alpha": 0.1},
        "family": "vol",
    },
    "garch": {
        "label": "GARCH(1,1)（波动率）",
        "hint": "逐个标的估计条件方差，只用于波动率标签",
        "params": {},
        "family": "vol",
    },
}


class LearnerError(RuntimeError):
    """学习器不可用或参数有误（例如没装 PyTorch）。"""


def available() -> dict[str, bool]:
    out = {k: True for k in LEARNERS}
    torch_ok = _has("torch")
    for k, v in LEARNERS.items():
        if v.get("needs") == "torch":
            out[k] = torch_ok
    return out


def backends() -> dict[str, bool]:
    return {"lightgbm": _has("lightgbm"), "torch": _has("torch")}


def _has(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


def merged_params(kind: str, params: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(LEARNERS[kind]["params"])
    for k, v in (params or {}).items():
        if k in base and v is not None:
            if isinstance(base[k], list):
                v = [max(2, min(int(x), 512)) for x in (v if isinstance(v, list) else [v])][:3] or base[k]
            elif isinstance(base[k], int):
                v = int(v)
            elif isinstance(base[k], float):
                v = float(v)
            base[k] = v
    for k, lo, hi in (
        ("epochs", 1, 300),
        ("batch", 16, 4096),
        ("seq_len", 2, 128),
        ("n_estimators", 5, 2000),
        ("max_depth", 1, 8),
        ("min_leaf", 5, 5000),
        ("layers", 1, 4),
        ("hidden", 2, 512),
    ):
        if k in base and isinstance(base[k], int):
            base[k] = max(lo, min(base[k], hi))
    return base


def make_learner(kind: str, params: dict[str, Any] | None, task: str, seed: int = 7) -> Learner:
    if kind not in LEARNERS:
        raise LearnerError(f"未知模型 {kind}")
    p = merged_params(kind, params)
    if LEARNERS[kind].get("needs") == "torch":
        if not _has("torch"):
            raise LearnerError(f"{LEARNERS[kind]['label']} 需要 PyTorch：pip install torch 后重启服务")
        from .deep import SeqLearner

        return SeqLearner(kind, p, task, seed)
    cls = {"ridge": Linear, "gbdt": GBDT, "mlp": MLP, "har": Linear, "garch": Garch}[kind]
    return cls(kind, p, task, seed)


def load_learner(state: dict[str, Any]) -> Learner:
    kind = state["kind"]
    m = make_learner(kind, state.get("params"), state.get("task", "reg"), int(state.get("seed", 7)))
    m.load(state)
    return m


# ---------------------------------------------------------------------------
class Learner:
    def __init__(self, kind: str, params: dict[str, Any], task: str, seed: int) -> None:
        self.kind, self.params, self.task, self.seed = kind, params, task, seed

    def fit(
        self,
        ds: Any,
        tr: np.ndarray,
        va: np.ndarray,
        *,
        stop: Callable[[], None] | None = None,
        init: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def predict(self, ds: Any, idx: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def state(self) -> dict[str, Any]:
        return {"kind": self.kind, "params": self.params, "task": self.task, "seed": self.seed}

    def load(self, state: dict[str, Any]) -> None:
        pass


def _loss(task: str, p: np.ndarray, y: np.ndarray) -> float:
    if len(y) == 0:
        return float("nan")
    if task == "clf":
        q = np.clip(p, 1e-6, 1 - 1e-6)
        return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q)))
    return float(np.mean((p - y) ** 2))


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


# ---------------------------------------------------------------------------
# 线性：岭回归 / L2 逻辑回归（HAR 也是线性回归，只是特征固定）
# ---------------------------------------------------------------------------
class Linear(Learner):
    w: np.ndarray
    b: float = 0.0

    def fit(self, ds, tr, va, *, stop=None, init=None):
        X, y = ds.flat_X(tr), ds.y_rows[tr]
        alpha = float(self.params.get("alpha", 1.0)) * len(y) / 1000
        F = X.shape[1]
        if self.task == "clf":
            w, b = np.zeros(F), 0.0
            for _ in range(25):  # 牛顿法
                p = _sigmoid(X @ w + b)
                g = X.T @ (p - y) + alpha * w
                gb = float(np.sum(p - y))
                s = p * (1 - p)
                H = (X * s[:, None]).T @ X + alpha * np.eye(F)
                hb = float(np.sum(s)) + 1e-9
                step = np.linalg.solve(H + 1e-9 * np.eye(F), g)
                w -= step
                b -= gb / hb
                if np.max(np.abs(step)) < 1e-6:
                    break
            self.w, self.b = w, b
        else:
            ym = float(np.mean(y))
            self.w = np.linalg.solve(X.T @ X + alpha * np.eye(F), X.T @ (y - ym))
            self.b = ym
        tr_loss = _loss(self.task, self._raw(X), y)
        va_loss = _loss(self.task, self.predict(ds, va), ds.y_rows[va]) if len(va) else None
        return [{"epoch": 1, "train": tr_loss, "val": va_loss}]

    def _raw(self, X: np.ndarray) -> np.ndarray:
        z = X @ self.w + self.b
        return _sigmoid(z) if self.task == "clf" else z

    def predict(self, ds, idx):
        return self._raw(ds.flat_X(idx))

    def coefficients(self) -> list[float]:
        return [float(x) for x in self.w]

    def state(self):
        return {**super().state(), "w": self.w.tolist(), "b": float(self.b)}

    def load(self, state):
        self.w, self.b = np.asarray(state["w"], dtype=float), float(state["b"])


# ---------------------------------------------------------------------------
# MLP（numpy，Adam，早停）
# ---------------------------------------------------------------------------
class MLP(Learner):
    def _init(self, F: int, rng: np.random.Generator) -> list[np.ndarray]:
        sizes = [F, *self.params["hidden"], 1]
        ws: list[np.ndarray] = []
        for a, b in zip(sizes[:-1], sizes[1:], strict=False):
            ws.append(rng.normal(0, math.sqrt(2 / a), (a, b)))
            ws.append(np.zeros(b))
        return ws

    def _forward(
        self, X: np.ndarray, ws: list[np.ndarray], *, train: bool = False, rng: np.random.Generator | None = None
    ) -> tuple[np.ndarray, list[tuple]]:
        acts = []
        h = X
        L = len(ws) // 2
        drop = float(self.params.get("dropout", 0.0))
        for i in range(L):
            W, b = ws[2 * i], ws[2 * i + 1]
            z = h @ W + b
            if i < L - 1:
                a = np.maximum(z, 0)
                mask = None
                if train and drop > 0 and rng is not None:
                    mask = (rng.random(a.shape) >= drop) / (1 - drop)
                    a = a * mask
                acts.append((h, z, mask))
                h = a
            else:
                acts.append((h, z, None))
                h = z
        out = h[:, 0]
        return (_sigmoid(out) if self.task == "clf" else out), acts

    def fit(self, ds, tr, va, *, stop=None, init=None):
        rng = np.random.default_rng(self.seed)
        X, y = ds.flat_X(tr), ds.y_rows[tr]
        Xv, yv = ds.flat_X(va), ds.y_rows[va]
        ws = (
            [np.asarray(w, dtype=float) for w in init["ws"]] if init and init.get("ws") else self._init(X.shape[1], rng)
        )
        if len(ws[0]) != X.shape[1]:
            ws = self._init(X.shape[1], rng)
        m = [np.zeros_like(w) for w in ws]
        v = [np.zeros_like(w) for w in ws]
        lr, wd = float(self.params["lr"]), float(self.params["weight_decay"])
        B = int(self.params["batch"])
        best, best_ws, bad, curve = float("inf"), [w.copy() for w in ws], 0, []
        step = 0
        for ep in range(1, int(self.params["epochs"]) + 1):
            if stop:
                stop()
            order = rng.permutation(len(y))
            for lo in range(0, len(y), B):
                bi = order[lo : lo + B]
                xb, yb = X[bi], y[bi]
                pred, acts = self._forward(xb, ws, train=True, rng=rng)
                grad_out = (pred - yb) / len(yb) * (1.0 if self.task == "clf" else 2.0)
                g = grad_out[:, None]
                grads: list[np.ndarray] = [np.empty(0)] * len(ws)
                for i in range(len(acts) - 1, -1, -1):
                    h_in, z, mask = acts[i]
                    if i < len(acts) - 1:
                        g = g * (z > 0)
                        if mask is not None:
                            g = g * mask
                    grads[2 * i] = h_in.T @ g + wd * ws[2 * i]
                    grads[2 * i + 1] = g.sum(axis=0)
                    g = g @ ws[2 * i].T
                step += 1
                for k in range(len(ws)):
                    m[k] = 0.9 * m[k] + 0.1 * grads[k]
                    v[k] = 0.999 * v[k] + 0.001 * grads[k] ** 2
                    mh = m[k] / (1 - 0.9**step)
                    vh = v[k] / (1 - 0.999**step)
                    ws[k] = ws[k] - lr * mh / (np.sqrt(vh) + 1e-8)
            tl = _loss(self.task, self._forward(X, ws)[0], y)
            vl = _loss(self.task, self._forward(Xv, ws)[0], yv) if len(yv) else tl
            curve.append({"epoch": ep, "train": tl, "val": vl if len(yv) else None})
            if vl < best - 1e-6:
                best, best_ws, bad = vl, [w.copy() for w in ws], 0
            else:
                bad += 1
                if bad >= int(self.params["patience"]):
                    break
        self.ws = best_ws
        return curve

    def predict(self, ds, idx):
        return self._forward(ds.flat_X(idx), self.ws)[0]

    def state(self):
        return {**super().state(), "ws": [w.tolist() for w in self.ws]}

    def load(self, state):
        self.ws = [np.asarray(w, dtype=float) for w in state["ws"]]


# ---------------------------------------------------------------------------
# 梯度提升树：LightGBM 或内置直方图版
# ---------------------------------------------------------------------------
class GBDT(Learner):
    backend = "builtin"

    def fit(self, ds, tr, va, *, stop=None, init=None):
        X, y = ds.flat_X(tr), ds.y_rows[tr]
        Xv, yv = ds.flat_X(va), ds.y_rows[va]
        if _has("lightgbm"):
            return self._fit_lgb(X, y, Xv, yv)
        return self._fit_builtin(X, y, Xv, yv, stop)

    def _fit_lgb(self, X, y, Xv, yv):
        import lightgbm as lgb

        self.backend = "lightgbm"
        p = self.params
        params = {
            "objective": "binary" if self.task == "clf" else "regression",
            "learning_rate": p["learning_rate"],
            "num_leaves": max(2, 2 ** int(p["max_depth"]) - 1),
            "max_depth": int(p["max_depth"]),
            "min_data_in_leaf": int(p["min_leaf"]),
            "bagging_fraction": float(p["subsample"]),
            "bagging_freq": 1,
            "feature_fraction": 0.9,
            "seed": self.seed,
            "verbosity": -1,
            "num_threads": 2,
            "deterministic": True,
        }
        dtr = lgb.Dataset(X, y)
        valid = [dtr]
        names = ["train"]
        if len(yv):
            valid.append(lgb.Dataset(Xv, yv, reference=dtr))
            names.append("val")
        hist: dict[str, Any] = {}
        cbs = [lgb.record_evaluation(hist)]
        if len(yv):
            cbs.append(lgb.early_stopping(int(p["patience"]), verbose=False))
        self.booster = lgb.train(
            params, dtr, num_boost_round=int(p["n_estimators"]), valid_sets=valid, valid_names=names, callbacks=cbs
        )
        metric = "binary_logloss" if self.task == "clf" else "l2"
        tr_c = hist.get("train", {}).get(metric, [])
        va_c = hist.get("val", {}).get(metric, [])
        return [
            {"epoch": i + 1, "train": float(tr_c[i]), "val": float(va_c[i]) if i < len(va_c) else None}
            for i in range(len(tr_c))
        ]

    # ---- 内置：深度受限的回归树，按 32 分位分箱 ----
    def _fit_builtin(self, X, y, Xv, yv, stop):
        p = self.params
        rng = np.random.default_rng(self.seed)
        edges = [np.unique(np.quantile(X[:, j], np.linspace(0, 1, 33)[1:-1])) for j in range(X.shape[1])]
        bins = np.stack([np.searchsorted(edges[j], X[:, j]) for j in range(X.shape[1])], axis=1)
        binsv = (
            np.stack([np.searchsorted(edges[j], Xv[:, j]) for j in range(Xv.shape[1])], axis=1)
            if len(yv)
            else np.zeros((0, X.shape[1]), dtype=int)
        )
        base = (
            float(np.log(np.mean(y) / (1 - np.mean(y))))
            if self.task == "clf" and 0 < np.mean(y) < 1
            else float(np.mean(y))
            if self.task != "clf"
            else 0.0
        )
        F, Fv = np.full(len(y), base), np.full(len(yv), base)
        trees, curve, best, best_n, bad = [], [], float("inf"), 0, 0
        lr = float(p["learning_rate"])
        for it in range(int(p["n_estimators"])):
            if stop and it % 10 == 0:
                stop()
            if self.task == "clf":
                pr = _sigmoid(F)
                g, hss = pr - y, np.maximum(pr * (1 - pr), 1e-6)
            else:
                g, hss = F - y, np.ones(len(y))
            sub = rng.random(len(y)) < float(p["subsample"])
            tree = _grow(bins, g, hss, sub, int(p["max_depth"]), int(p["min_leaf"]), 33)
            trees.append(tree)
            F += lr * _apply(tree, bins)
            if len(yv):
                Fv += lr * _apply(tree, binsv)
            out = _sigmoid(F) if self.task == "clf" else F
            outv = _sigmoid(Fv) if self.task == "clf" else Fv
            tl = _loss(self.task, out, y)
            vl = _loss(self.task, outv, yv) if len(yv) else tl
            curve.append({"epoch": it + 1, "train": tl, "val": vl if len(yv) else None})
            if vl < best - 1e-7:
                best, best_n, bad = vl, it + 1, 0
            else:
                bad += 1
                if bad >= int(p["patience"]):
                    break
        self.trees = trees[: best_n or len(trees)]
        self.edges = [e.tolist() for e in edges]
        self.base = base
        return curve

    def predict(self, ds, idx):
        X = ds.flat_X(idx)
        if getattr(self, "booster", None) is not None:
            return np.asarray(self.booster.predict(X, num_iteration=self.booster.best_iteration or None), dtype=float)
        bins = np.stack([np.searchsorted(np.asarray(self.edges[j]), X[:, j]) for j in range(X.shape[1])], axis=1)
        F = np.full(len(X), self.base)
        lr = float(self.params["learning_rate"])
        for t in self.trees:
            F += lr * _apply(t, bins)
        return _sigmoid(F) if self.task == "clf" else F

    def importance_gain(self) -> list[float] | None:
        if getattr(self, "booster", None) is not None:
            return [float(x) for x in self.booster.feature_importance(importance_type="gain")]
        return None

    def state(self):
        if getattr(self, "booster", None) is not None:
            return {
                **super().state(),
                "backend": "lightgbm",
                "model": base64.b64encode(self.booster.model_to_string().encode()).decode(),
            }
        return {**super().state(), "backend": "builtin", "trees": self.trees, "edges": self.edges, "base": self.base}

    def load(self, state):
        if state.get("backend") == "lightgbm":
            if not _has("lightgbm"):
                raise LearnerError("这个模型用 LightGBM 训练，加载需要先 pip install lightgbm")
            import lightgbm as lgb

            self.booster = lgb.Booster(model_str=base64.b64decode(state["model"]).decode())
            self.backend = "lightgbm"
        else:
            self.booster = None
            self.trees, self.edges, self.base = state["trees"], state["edges"], float(state["base"])


def _grow(
    bins: np.ndarray, g: np.ndarray, h: np.ndarray, sub: np.ndarray, depth: int, min_leaf: int, nb: int
) -> dict[str, Any]:
    """二阶梯度的贪心分裂，节点用字典表示：{"f", "t", "l", "r"} 或 {"v"}。"""
    lam = 1.0

    def leaf(mask: np.ndarray) -> dict[str, Any]:
        return {"v": float(-g[mask].sum() / (h[mask].sum() + lam))}

    def build(mask: np.ndarray, d: int) -> dict[str, Any]:
        if d == 0 or mask.sum() < 2 * min_leaf:
            return leaf(mask)
        G, H = g[mask].sum(), h[mask].sum()
        parent = G * G / (H + lam)
        best = (0.0, -1, -1)
        for j in range(bins.shape[1]):
            b = bins[mask, j]
            gs = np.bincount(b, weights=g[mask], minlength=nb)
            hs = np.bincount(b, weights=h[mask], minlength=nb)
            cnt = np.bincount(b, minlength=nb)
            gl, hl, cl = np.cumsum(gs)[:-1], np.cumsum(hs)[:-1], np.cumsum(cnt)[:-1]
            ok = (cl >= min_leaf) & (mask.sum() - cl >= min_leaf)
            if not ok.any():
                continue
            gain = gl**2 / (hl + lam) + (G - gl) ** 2 / (H - hl + lam) - parent
            gain = np.where(ok, gain, -np.inf)
            k = int(np.argmax(gain))
            if gain[k] > best[0]:
                best = (float(gain[k]), j, k)
        if best[1] < 0:
            return leaf(mask)
        _, j, k = best
        left = mask & (bins[:, j] <= k)
        right = mask & (bins[:, j] > k)
        return {"f": j, "t": k, "l": build(left, d - 1), "r": build(right, d - 1)}

    return build(sub.copy(), depth)


def _apply(tree: dict[str, Any], bins: np.ndarray) -> np.ndarray:
    out = np.zeros(len(bins))
    stack = [(tree, np.ones(len(bins), dtype=bool))]
    while stack:
        node, mask = stack.pop()
        if "v" in node:
            out[mask] = node["v"]
            continue
        go = bins[:, node["f"]] <= node["t"]
        stack.append((node["l"], mask & go))
        stack.append((node["r"], mask & ~go))
    return out


# ---------------------------------------------------------------------------
# GARCH(1,1)：方差目标化 + 网格极大似然，逐个标的
# ---------------------------------------------------------------------------
class Garch(Learner):
    def fit(self, ds, tr, va, *, stop=None, init=None):
        R = ds.panel.data["ret"]
        times = ds.t_rows[tr]
        hi = int(times.max()) + 1 if len(times) else 0
        self.coef: dict[str, list[float]] = {}
        ll_total = 0.0
        for j, sym in enumerate(ds.symbols):
            r = R[:hi, j]
            r = r[np.isfinite(r)]
            if len(r) < 60:
                continue
            var = float(np.var(r))
            best = (-np.inf, 0.05, 0.9)
            for a in (0.02, 0.04, 0.06, 0.08, 0.1, 0.13, 0.16, 0.2):
                for b in (0.7, 0.75, 0.8, 0.85, 0.88, 0.91, 0.94, 0.96):
                    if a + b >= 0.995:
                        continue
                    s2 = _garch_path(r, a, b, var)
                    ll = float(-0.5 * np.sum(np.log(s2[:-1]) + r**2 / s2[:-1]))
                    if ll > best[0]:
                        best = (ll, a, b)
            self.coef[sym] = [best[1], best[2], var]
            ll_total += best[0]
        return [{"epoch": 1, "train": -ll_total / max(len(tr), 1), "val": None}]

    def predict(self, ds, idx):
        R = ds.panel.data["ret"]
        h = int(ds.horizon)
        cache: dict[int, np.ndarray] = {}
        out = np.full(len(idx), np.nan)
        for k, (t, j) in enumerate(zip(ds.t_rows[idx], ds.n_rows[idx], strict=True)):
            sym = ds.symbols[j]
            if sym not in self.coef:
                continue
            if j not in cache:
                a, b, var = self.coef[sym]
                r = np.nan_to_num(R[:, j])
                cache[j] = _garch_path(r, a, b, var)
            a, b, var = self.coef[sym]
            s_next = cache[j][t + 1] if t + 1 < len(cache[j]) else cache[j][-1]
            pers = a + b
            f = [var + pers**i * (s_next - var) for i in range(h)]
            out[k] = math.sqrt(max(np.mean(f), 1e-12))
        return out

    def state(self):
        return {**super().state(), "coef": self.coef}

    def load(self, state):
        self.coef = {k: list(v) for k, v in state["coef"].items()}


def _garch_path(r: np.ndarray, a: float, b: float, var: float) -> np.ndarray:
    """s2[t] = 第 t 期收益的条件方差（用到 t-1 及以前），长度 len(r)+1，最后一个是下一期的预测。"""
    w = var * (1 - a - b)
    s2 = np.empty(len(r) + 1)
    s2[0] = var
    for t in range(len(r)):
        s2[t + 1] = w + a * r[t] ** 2 + b * s2[t]
    return np.maximum(s2, 1e-12)


__all__ = [
    "LEARNERS",
    "Learner",
    "LearnerError",
    "available",
    "backends",
    "load_learner",
    "make_learner",
    "merged_params",
]
