"""模型定期重训：按计划在最新数据上重训，每次重训是一个新版本，接不接替由样本外表现决定。

- 时点：重训只用「重训时刻」以前已收盘的数据（面板截到 asof），训练集内部照常做 purge 和滚动样本外评估，
  所以重训本身不会把未来数据带进来；新版本的泄漏检查和第一次训练一样跑。
- 版本：每次重训都记一笔实验（来源 retrain），模型的 versions 里多一行，带上一版的实验 id，血缘图能顺着查回去。
- 接替：新版本的样本外 RankIC 不低于旧版本的 min_ratio 倍（默认 0.8）且通过泄漏检查，才替换模型参数；
  否则保留旧参数，只记一笔「重训未采用」。冻结的模型不会被替换，重训结果另存为候选模型。
- 计划：模型上的 retrain = {enabled, every_days}；worker 每小时扫一次，到期的登记为后台任务。
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

DAY_S = 86_400
DEFAULT_PLAN = {"enabled": False, "every_days": 7, "min_ratio": 0.8}


def plan(rec: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULT_PLAN, **(rec.get("retrain") or {})}


def set_plan(lib: Any, mid: str, **changes: Any) -> dict[str, Any] | None:
    rec = lib.get(mid)
    if rec is None:
        return None
    p = plan(rec)
    for k in ("enabled", "every_days", "min_ratio"):
        if changes.get(k) is not None:
            p[k] = changes[k]
    p["every_days"] = max(1, min(90, int(p["every_days"])))
    p["min_ratio"] = max(0.0, min(2.0, float(p["min_ratio"])))
    p["enabled"] = bool(p["enabled"])
    return lib.patch(mid, retrain=p)


def last_retrain(rec: dict[str, Any]) -> float:
    ts = [float(v.get("at") or 0) for v in [*(rec.get("versions") or []), *(rec.get("retrain_log") or [])]]
    return max([float(rec.get("created_at") or 0), *ts])


def due(rec: dict[str, Any], now: float | None = None) -> bool:
    p = plan(rec)
    if not p["enabled"] or rec.get("status") == "retired":
        return False
    return (now or time.time()) - last_retrain(rec) >= p["every_days"] * DAY_S


def cut(panel: Any, asof_ms: int | None) -> Any:
    """面板截到 asof（含）：重训只看到当时已收盘的 K 线。"""
    if asof_ms is None:
        return panel
    hi = int(np.searchsorted(panel.times, int(asof_ms), side="right"))
    return panel.slice(0, hi)


def _ic(summary: dict[str, Any] | None) -> float | None:
    v = (summary or {}).get("oos_rank_ic")
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def retrain(
    lib: Any, mid: str, panel: Any, *, asof_ms: int | None = None, reason: str = "scheduled", stop: Any = None
) -> dict[str, Any]:
    from . import models as M

    rec = lib.get(mid)
    if rec is None:
        raise KeyError("模型不存在")
    p = cut(panel, asof_ms)
    if len(p.times) < 100:
        raise M.ModelError("截到重训时点的数据太少（不足 100 根）")
    spec = {**rec["spec"], "start": None, "end": int(p.times[-1])}
    out = M.train_and_evaluate(spec, p, stop=stop, trials=lib.trial_count() + 1)
    parent = rec.get("run_id")
    run = lib.record_run(
        spec,
        out["report"],
        out["state"],
        source="retrain",
        note=f"定期重训（截至 {time.strftime('%Y-%m-%d %H:%M', time.gmtime(int(p.times[-1]) / 1000))} UTC）",
    )
    old, new = _ic(rec.get("summary")), _ic(out["report"].get("summary"))
    ratio = plan(rec)["min_ratio"]
    leak_ok = bool(out["report"].get("admitted", True))
    if new is None:
        accepted, why = False, "新版本没有样本外 RankIC"
    elif not leak_ok:
        accepted, why = False, "新版本没通过泄漏检查或准入"
    elif old is None or new >= (old * ratio if old > 0 else old):
        accepted, why = True, "样本外不差于旧版本"
    else:
        accepted, why = False, f"样本外 RankIC {new:.3f} 低于旧版本 {old:.3f} 的 {ratio:g} 倍"
    entry = {
        "run_id": run["id"],
        "parent_run_id": parent,
        "at": time.time(),
        "oos_rank_ic": new,
        "asof": int(p.times[-1]),
        "reason": reason,
        "accepted": accepted,
        "why": why,
    }
    if accepted:
        saved = lib.save_from_run(
            run["id"], name=rec["name"], status=rec.get("status") or "candidate", model_id=mid, note=rec.get("note", "")
        )
        if saved["id"] != mid:  # 冻结模型：另存为新候选，原模型不动
            entry["saved_as"] = saved["id"]
            cur = lib.get(mid) or rec
            lib.patch(mid, retrain_log=[*(cur.get("retrain_log") or []), entry][-30:])
        else:
            vs = list(saved.get("versions") or [])
            if vs:
                vs[-1] = {**vs[-1], **entry}
            lib.patch(
                mid,
                versions=vs,
                retrain_log=[*(rec.get("retrain_log") or []), entry][-30:],
                status="active" if rec.get("status") == "decaying" else saved.get("status"),
            )
    else:
        lib.patch(mid, retrain_log=[*(rec.get("retrain_log") or []), entry][-30:])
    return {"model": lib.get(mid), "retrain": entry}
