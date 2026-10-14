"""研究纪律：试验台账、缩减夏普（DSR）、过拟合概率（PBO）、紧封测试集。

为什么要有这些：AI 自动研究一晚上能试几百个版本，试得越多，"最好的那个"越可能只是运气。
- 试验台账把每一次评估都记下来（回测、因子、模型、组合、执行），按研究方向分组计数。
- DSR（Bailey & López de Prado, 2014）：在"试了 N 次、各次夏普的离散程度为 V"的前提下，
  最好那一版的真实夏普大于 0 的概率。N 越大，门槛越高。
- 因子与模型看 t 值：N 次试验后要算显著，|t| 至少要过 Bonferroni 修正后的门槛（Harvey-Liu-Zhu 的简化）。
- PBO（Bailey 等, 2015，组合对称交叉验证 CSCV）：把同一段时间切成 S 块，任取一半做样本内选最优，
  看它在另一半里排名是否掉到中位数以下。掉下去的比例就是过拟合概率。
- 紧封测试集：最后一段数据锁住，研究时看不到；每个成果只能开封一次，开封记录永久留痕。

这里只放纯计算和存取，不依赖 API 层；数据从各个成果库里读。
"""

from __future__ import annotations

import contextvars
import itertools
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from statistics import NormalDist
from typing import Any

import numpy as np

EULER = 0.5772156649015329
N01 = NormalDist()
HOLDOUT = "rd_holdout"  # 紧封测试集设置（单条记录 id=current）
UNSEALS = "rd_unseals"  # 开封记录：每个成果一条，永久保留
HOLDOUT_LOG = "rd_holdout_log"  # 设置 / 解除 / 调整的留痕

DOMAIN_LABELS = {"strategy": "策略回测", "factor": "因子", "model": "模型", "portfolio": "组合", "execution": "执行"}


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# 统计量
# ---------------------------------------------------------------------------
def expected_max_sr(n: int, sr_var: float) -> float:
    """N 次独立试验、夏普方差为 V 时，纯运气下最好一次的期望夏普（与输入同一单位）。"""
    if n <= 1 or sr_var <= 0:
        return 0.0
    return math.sqrt(sr_var) * ((1 - EULER) * N01.inv_cdf(1 - 1 / n) + EULER * N01.inv_cdf(1 - 1 / (n * math.e)))


def deflated_sharpe(
    sr_ann: float,
    *,
    n_trials: int,
    sr_ann_var: float,
    periods: int,
    periods_per_year: float,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> dict[str, Any]:
    """缩减夏普比率。输入年化夏普，内部换成每期夏普计算；返回 DSR（概率）和对应门槛。"""
    k = math.sqrt(max(periods_per_year, 1.0))
    sr = sr_ann / k
    sr0 = expected_max_sr(n_trials, sr_ann_var / (k * k))
    t = max(int(periods), 2)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        denom = 1e-6
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)
    return {"dsr": float(N01.cdf(z)), "sr0_ann": float(sr0 * k), "z": float(z), "n_trials": int(n_trials), "periods": t}


def t_threshold(n_trials: int, alpha: float = 0.05) -> float:
    """N 次试验后双侧显著所需的 |t|（Bonferroni）。一次试验时就是 1.96。"""
    n = max(int(n_trials), 1)
    return float(N01.inv_cdf(1 - alpha / (2 * n)))


def return_moments(returns: np.ndarray) -> tuple[float, float]:
    r = returns[np.isfinite(returns)]
    if len(r) < 8 or float(np.std(r)) == 0:
        return 0.0, 3.0
    z = (r - r.mean()) / r.std()
    return float(np.mean(z**3)), float(np.mean(z**4))


def pbo_cscv(matrix: np.ndarray, *, splits: int = 10, max_combos: int = 2000) -> dict[str, Any]:
    """组合对称交叉验证。matrix: T×N 收益（行是时间，列是候选版本）。"""
    m = np.asarray(matrix, dtype=float)
    m = m[np.all(np.isfinite(m), axis=1)]
    t, n = m.shape
    if n < 2:
        raise ValueError("至少要两个候选版本才能算过拟合概率")
    s = min(splits, (t // 8) // 2 * 2)
    if s < 4:
        raise ValueError(f"共同的观测太少（{t} 个），至少需要 32 个")
    blocks = np.array_split(np.arange(t), s)
    combos = list(itertools.combinations(range(s), s // 2))
    if len(combos) > max_combos:
        rng = np.random.default_rng(7)
        combos = [combos[i] for i in sorted(rng.choice(len(combos), max_combos, replace=False))]

    def sharpe(x: np.ndarray) -> np.ndarray:
        sd = x.std(axis=0)
        return np.where(sd > 0, x.mean(axis=0) / np.where(sd > 0, sd, 1), 0.0)

    logits: list[float] = []
    pairs: list[tuple[float, float]] = []
    for c in combos:
        ins = np.concatenate([blocks[i] for i in c])
        oos = np.concatenate([blocks[i] for i in range(s) if i not in c])
        sr_is, sr_oos = sharpe(m[ins]), sharpe(m[oos])
        best = int(np.argmax(sr_is))
        rank = float((sr_oos < sr_oos[best]).sum() + 0.5 * ((sr_oos == sr_oos[best]).sum() - 1) + 1)
        w = rank / (n + 1)
        logits.append(math.log(w / (1 - w)))
        pairs.append((float(sr_is[best]), float(sr_oos[best])))
    lg = np.array(logits)
    ins_sr, oos_sr = np.array(pairs).T
    slope = float(np.polyfit(ins_sr, oos_sr, 1)[0]) if len(set(np.round(ins_sr, 12))) > 1 else None
    hist, edges = np.histogram(lg, bins=12)
    return {
        "pbo": float((lg <= 0).mean()),
        "n": int(n),
        "t": int(t),
        "splits": int(s),
        "combos": len(combos),
        "logit_median": float(np.median(lg)),
        "prob_oos_loss": float((oos_sr < 0).mean()),
        "degradation_slope": slope,
        "hist": [{"x0": float(edges[i]), "x1": float(edges[i + 1]), "n": int(hist[i])} for i in range(len(hist))],
        "scatter": [{"is": round(a, 5), "oos": round(b, 5)} for a, b in pairs[:400]],
    }


def align_series(series: dict[str, list[tuple[int, float]]]) -> tuple[list[str], np.ndarray, list[int]]:
    """把各版本的净值序列按共同时间点对齐，转成收益矩阵。"""
    keys = [k for k, v in series.items() if len(v) >= 3]
    if len(keys) < 2:
        return keys, np.zeros((0, len(keys))), []
    common = set.intersection(*[{t for t, _ in series[k]} for k in keys])
    ts = sorted(common)
    if len(ts) < 3:
        return keys, np.zeros((0, len(keys))), []
    cols = []
    for k in keys:
        d = dict(series[k])
        nav = np.array([d[t] for t in ts], dtype=float)
        cols.append(nav[1:] / np.where(nav[:-1] == 0, np.nan, nav[:-1]) - 1)
    return keys, np.column_stack(cols), ts[1:]


# ---------------------------------------------------------------------------
# 紧封测试集
# ---------------------------------------------------------------------------
_BYPASS: contextvars.ContextVar[bool] = contextvars.ContextVar("rd_holdout_bypass", default=False)


@contextmanager
def unsealed() -> Iterator[None]:
    """开封评估期间临时看得到测试集。只给开封流程用。"""
    tok = _BYPASS.set(True)
    try:
        yield
    finally:
        _BYPASS.reset(tok)


def holdout_config(store: Any) -> dict[str, Any] | None:
    try:
        rec = store.get(HOLDOUT, "current")
    except Exception:
        return None
    return rec if rec and rec.get("cutoff") else None


def research_cutoff(store: Any) -> int | None:
    """研究流程能看到的最后时刻（毫秒）。没有设置或正在开封时返回 None。"""
    if _BYPASS.get():
        return None
    rec = holdout_config(store)
    return int(rec["cutoff"]) if rec else None


def clamp_end(store: Any, end: int | None) -> tuple[int | None, str | None]:
    """把研究请求的结束时间夹到紧封线之前。返回 (新的 end, 提示)。"""
    cut = research_cutoff(store)
    if cut is None or (end is not None and end <= cut):
        return end, None
    day = time.strftime("%Y-%m-%d", time.localtime(cut / 1000))
    return cut, f"已排除紧封测试集（{day} 之后的数据），需要时到「研究纪律 → 紧封测试集」开封"


def seal(store: Any, *, cutoff: int, user: str, note: str = "") -> dict[str, Any]:
    prev = holdout_config(store)
    rec = {"id": "current", "cutoff": int(cutoff), "set_at": _now(), "set_by": user, "note": note[:200]}
    store.upsert(HOLDOUT, rec)
    _log(
        store,
        "change" if prev else "seal",
        user,
        {"cutoff": int(cutoff), "previous": (prev or {}).get("cutoff"), "note": note[:200]},
    )
    return rec


def unseal_all(store: Any, *, user: str, reason: str) -> None:
    prev = holdout_config(store)
    if prev:
        store.delete(HOLDOUT, "current")
        _log(store, "remove", user, {"previous": prev.get("cutoff"), "note": reason[:200]})


def _log(store: Any, action: str, user: str, data: dict[str, Any]) -> None:
    store.upsert(HOLDOUT_LOG, {"id": f"h_{int(_now() * 1000)}", "action": action, "user": user, "at": _now(), **data})


def holdout_log(store: Any) -> list[dict[str, Any]]:
    return sorted(store.list(HOLDOUT_LOG), key=lambda r: -(r.get("at") or 0))


def unseal_key(kind: str, ref: str) -> str:
    return f"{kind}:{ref}"[:120]


def unseal_record(store: Any, kind: str, ref: str) -> dict[str, Any] | None:
    return store.get(UNSEALS, unseal_key(kind, ref))


def record_unseal(
    store: Any, *, kind: str, ref: str, name: str, user: str, cutoff: int, result: dict[str, Any]
) -> dict[str, Any]:
    rec = {
        "id": unseal_key(kind, ref),
        "kind": kind,
        "ref": ref,
        "name": name[:80],
        "user": user,
        "at": _now(),
        "cutoff": int(cutoff),
        "result": result,
    }
    store.upsert(UNSEALS, rec)
    return rec


def unseals(store: Any) -> list[dict[str, Any]]:
    return sorted(store.list(UNSEALS), key=lambda r: -(r.get("at") or 0))


# ---------------------------------------------------------------------------
# 试验台账：从各个成果库汇总
# ---------------------------------------------------------------------------
def _bpy(timeframe: str | None) -> float:
    from .portfolio import bars_per_year

    try:
        return float(bars_per_year(timeframe or "1h"))
    except Exception:
        return 8760.0


def collect_trials(store: Any) -> list[dict[str, Any]]:
    """所有评估过的版本，统一成 {domain, family, family_label, name, metric, value, t_stat, periods, ...}。"""
    out: list[dict[str, Any]] = []
    for r in store.list("backtests"):
        m = r.get("metrics") or {}
        tf = r.get("timeframe") or "1h"
        bpy = _bpy(tf)
        span = max(0.0, (float(r.get("end_ts") or 0) - float(r.get("start_ts") or 0)) / 1000)
        periods = int(span / (365 * 86400) * bpy) if span else 0
        out.append(
            {
                "domain": "strategy",
                "id": r["id"],
                "family": r.get("slug") or r.get("strategy_name") or "草稿",
                "family_label": r.get("strategy_name") or r.get("slug") or "未保存的草稿",
                "name": f"{r.get('strategy_name') or ''} {r.get('version') or ''}".strip(),
                "sharpe": m.get("sharpe"),
                "periods": periods,
                "ppy": bpy,
                "created_at": r.get("created_at"),
                "ref": r["id"],
            }
        )
    for r in store.list("optimizations"):
        # 参数优化的每一组试验：只是没存成单独的回测记录，同样算进这个策略的试验次数
        srs = [x for x in r.get("trial_sharpes") or [] if x is not None]
        if not srs:
            continue
        bpy = _bpy(r.get("timeframe"))
        span = max(0.0, (float(r.get("end_ts") or 0) - float(r.get("start_ts") or 0)) / 1000)
        periods = int(span / (365 * 86400) * bpy) if span else 0
        label = "智能调参" if r.get("method") == "bayes" else "网格调参"
        for i, sr in enumerate(srs):
            out.append(
                {
                    "domain": "strategy",
                    "id": f"{r['id']}:{i}",
                    "family": r.get("slug") or "草稿",
                    "family_label": r.get("strategy_name") or r.get("slug") or "未保存的草稿",
                    "name": f"{label} 第 {i + 1} 组",
                    "sharpe": sr,
                    "periods": periods,
                    "ppy": bpy,
                    "created_at": r.get("created_at"),
                    "ref": r["id"],
                }
            )
    for r in store.list("research_trials"):
        out.append(
            {
                "domain": "factor",
                "id": r["id"],
                "family": r.get("project_id") or "factor",
                "family_label": "因子表达式",
                "name": r.get("expression", "")[:80],
                "t_stat": r.get("ic_t"),
                "value": r.get("rank_ic"),
                "count": int(r.get("count") or 1),
                "created_at": r.get("last_at") or r.get("first_at"),
                "ref": r["id"],
            }
        )
    for r in store.list("ml_runs"):
        s = r.get("summary") or {}
        out.append(
            {
                "domain": "model",
                "id": r["id"],
                "family": r.get("project_id") or "model",
                "family_label": "模型训练",
                "name": f"{r.get('model')} → {r.get('label')}",
                "t_stat": s.get("oos_ic_t"),
                "value": s.get("oos_rank_ic"),
                "sharpe": s.get("ls_sharpe"),
                "ppy": _bpy(r.get("timeframe")),
                "created_at": r.get("created_at"),
                "ref": r["id"],
                "hash": r.get("spec_hash"),
            }
        )
    for r in store.list("pf_runs"):
        s = r.get("summary") or {}
        out.append(
            {
                "domain": "portfolio",
                "id": r["id"],
                "family": r.get("project_id") or "portfolio",
                "family_label": "组合方案",
                "name": f"{r.get('optimizer')} · {r.get('risk_model')}",
                "sharpe": s.get("sharpe"),
                "periods": int(s.get("bars") or 0),
                "ppy": _bpy(r.get("timeframe")),
                "created_at": r.get("created_at"),
                "ref": r["id"],
                "hash": r.get("hash"),
            }
        )
    for r in store.list("ex_runs"):
        s = r.get("summary") or {}
        out.append(
            {
                "domain": "execution",
                "id": r["id"],
                "family": r.get("project_id") or "execution",
                "family_label": "执行方案",
                "name": f"{r.get('algo')} · {r.get('style')}",
                "value": s.get("is_bps"),
                "created_at": r.get("created_at"),
                "ref": r["id"],
                "hash": r.get("hash"),
            }
        )
    return out


def ledger(store: Any, *, project_titles: dict[str, str] | None = None) -> dict[str, Any]:
    """台账总览：各研究类型的试验次数、每个研究方向的最好成绩经试验次数修正后还剩多少。"""
    titles = project_titles or {}
    trials = collect_trials(store)
    by_domain: dict[str, int] = {}
    for t in trials:
        by_domain[t["domain"]] = by_domain.get(t["domain"], 0) + int(t.get("count") or 1)
    fams: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for t in trials:
        fams.setdefault((t["domain"], t["family"]), []).append(t)
    total_alpha = sum(v for k, v in by_domain.items() if k != "execution")
    families: list[dict[str, Any]] = []
    for (dom, fam), rows in fams.items():
        n = sum(int(r.get("count") or 1) for r in rows)
        label = titles.get(fam) or rows[0]["family_label"]
        row: dict[str, Any] = {
            "domain": dom,
            "family": fam,
            "label": label,
            "n": n,
            "project_id": fam if fam in titles else None,
            "last_at": max((r.get("created_at") or 0) for r in rows),
        }
        srs = [float(r["sharpe"]) for r in rows if r.get("sharpe") is not None and math.isfinite(float(r["sharpe"]))]
        if dom in ("strategy", "portfolio") and srs:
            best = max(rows, key=lambda r: r.get("sharpe") if r.get("sharpe") is not None else -1e9)
            var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
            periods = int(best.get("periods") or 0) or 500
            d = deflated_sharpe(
                float(best["sharpe"]),
                n_trials=n,
                sr_ann_var=var,
                periods=periods,
                periods_per_year=float(best.get("ppy") or 8760),
            )
            row.update(
                best_name=best["name"],
                best_sharpe=float(best["sharpe"]),
                sr_std=math.sqrt(var),
                **d,
                verdict="ok" if d["dsr"] >= 0.95 else "warn" if d["dsr"] >= 0.5 else "bad",
            )
        elif dom in ("factor", "model"):
            ts = [r for r in rows if r.get("t_stat") is not None]
            if ts:
                best = max(ts, key=lambda r: abs(float(r["t_stat"])))
                thr = t_threshold(n)
                tv = abs(float(best["t_stat"]))
                row.update(
                    best_name=best["name"],
                    best_t=float(best["t_stat"]),
                    t_needed=thr,
                    best_value=best.get("value"),
                    verdict="ok" if tv >= thr else "warn" if tv >= 1.96 else "bad",
                )
        elif dom == "execution":
            vals = [r["value"] for r in rows if r.get("value") is not None]
            if vals:
                row.update(best_value=min(vals), verdict="ok")
        families.append(row)
    families.sort(key=lambda r: -(r.get("last_at") or 0))
    recent = sorted(trials, key=lambda r: -(r.get("created_at") or 0))[:200]
    return {
        "by_domain": by_domain,
        "total": sum(by_domain.values()),
        "total_alpha": total_alpha,
        "t_needed_global": t_threshold(max(total_alpha, 1)),
        "families": families,
        "recent": recent,
        "domain_labels": DOMAIN_LABELS,
    }


def strategy_record(store: Any, slug: str) -> dict[str, Any] | None:
    """一个策略在台账里的记录：试验次数、最好夏普、DSR、紧封测试结果。导出策略包时放进 lineage.json。"""
    fam = next((f for f in ledger(store)["families"] if f["domain"] == "strategy" and f["family"] == slug), None)
    unseal = unseal_record(store, "strategy", slug)
    if fam is None and unseal is None:
        return None
    out: dict[str, Any] = {"strategy": slug}
    if fam:
        out.update({k: fam.get(k) for k in ("n", "best_name", "best_sharpe", "dsr", "verdict") if k in fam})
    if unseal:
        out["holdout"] = {"at": unseal.get("at"), "cutoff": unseal.get("cutoff"), "result": unseal.get("result")}
    return out


__all__ = [
    "DOMAIN_LABELS",
    "align_series",
    "clamp_end",
    "collect_trials",
    "deflated_sharpe",
    "expected_max_sr",
    "holdout_config",
    "holdout_log",
    "ledger",
    "pbo_cscv",
    "record_unseal",
    "research_cutoff",
    "return_moments",
    "seal",
    "strategy_record",
    "t_threshold",
    "unseal_all",
    "unseal_key",
    "unseal_record",
    "unsealed",
    "unseals",
]
