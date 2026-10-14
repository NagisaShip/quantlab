"""影子运行：候选版本和线上版本在同样的实时行情里并行模拟，稳定跑赢才替换。

一对影子 = 两个实时模拟实验（基准、候选），同标的池、同周期、同成本设置。
比较方法：两条净值按小时对齐，算每小时收益差的均值和 t 值；观察期不足时只显示进度。
替换条件（默认）：观察满 N 天、收益差 t ≥ 1.65、候选的最大回撤不超过基准的 1.2 倍。
"替换"只是把这对记录标成已替换并停止基准实验，不涉及任何真实下单。
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

SHADOWS = "rd_shadows"
HOUR = 3_600_000


def _now() -> float:
    return time.time()


def create(
    store: Any,
    *,
    baseline_id: str,
    candidate_id: str,
    baseline_name: str,
    candidate_name: str,
    min_days: int,
    user: str,
) -> dict[str, Any]:
    rec = {
        "id": f"sh_{uuid.uuid4().hex[:8]}",
        "baseline_id": baseline_id,
        "candidate_id": candidate_id,
        "label": f"{candidate_name} vs {baseline_name}",
        "baseline_name": baseline_name,
        "candidate_name": candidate_name,
        "min_days": int(min_days),
        "status": "running",
        "created_at": _now(),
        "created_by": user,
    }
    store.upsert(SHADOWS, rec)
    return rec


def items(store: Any) -> list[dict[str, Any]]:
    return sorted(store.list(SHADOWS), key=lambda r: -(r.get("created_at") or 0))


def get(store: Any, sid: str) -> dict[str, Any] | None:
    return store.get(SHADOWS, sid)


def hourly(points: list[dict[str, Any]]) -> dict[int, float]:
    out: dict[int, float] = {}
    for p in points or []:
        if p.get("v") is None:
            continue
        out[int(p["t"]) // HOUR * HOUR] = float(p["v"])
    return out


def _dd(vals: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


def compare(base: dict[str, Any], cand: dict[str, Any], *, min_days: int) -> dict[str, Any]:
    hb, hc = hourly(base.get("equity_points") or []), hourly(cand.get("equity_points") or [])
    ts = sorted(set(hb) & set(hc))
    curve = [{"t": t, "base": hb[t] / hb[ts[0]], "cand": hc[t] / hc[ts[0]]} for t in ts] if ts else []
    diffs = []
    for a, b in zip(ts, ts[1:], strict=False):
        rb = hb[b] / hb[a] - 1 if hb[a] else 0.0
        rc = hc[b] / hc[a] - 1 if hc[a] else 0.0
        diffs.append(rc - rb)
    hours = (ts[-1] - ts[0]) / HOUR if len(ts) > 1 else 0.0
    n = len(diffs)
    mean = sum(diffs) / n if n else 0.0
    sd = math.sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1)) if n > 1 else 0.0
    t = mean / sd * math.sqrt(n) if sd > 0 else 0.0
    ret_b = curve[-1]["base"] - 1 if curve else None
    ret_c = curve[-1]["cand"] - 1 if curve else None
    dd_b = _dd([c["base"] for c in curve]) if curve else None
    dd_c = _dd([c["cand"] for c in curve]) if curve else None
    need = min_days * 24
    if hours < need:
        verdict, tone, ready = f"观察中 {hours / 24:.1f}/{min_days} 天", "", False
    elif t >= 1.65 and (dd_c or 0) <= max(dd_b or 0, 0.005) * 1.2:
        verdict, tone, ready = "候选稳定跑赢，满足替换条件", "ok", True
    elif t <= -1.65:
        verdict, tone, ready = "候选明显落后", "bad", False
    else:
        verdict, tone, ready = "差别不显著，继续观察", "", False
    step = max(1, len(curve) // 400)
    return {
        "hours": hours,
        "need_hours": need,
        "progress": min(1.0, hours / need) if need else 1.0,
        "ret_base": ret_b,
        "ret_cand": ret_c,
        "dd_base": dd_b,
        "dd_cand": dd_c,
        "excess": (ret_c or 0) - (ret_b or 0) if curve else None,
        "t": t,
        "n": n,
        "verdict": verdict,
        "tone": tone,
        "ready": ready,
        "curve": curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else []),
    }


def mark(store: Any, sid: str, **patch: Any) -> dict[str, Any] | None:
    rec = store.get(SHADOWS, sid)
    if rec is None:
        return None
    rec = {**rec, **patch, "updated_at": _now()}
    store.upsert(SHADOWS, rec)
    return rec


__all__ = ["SHADOWS", "compare", "create", "get", "hourly", "items", "mark"]
