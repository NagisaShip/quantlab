"""模型库与实验追踪。

- 实验（collection ``ml_runs``）：每训练一次记一笔：规格、随机种子、数据摘要、指标、耗时；完整报告与模型参数存 blob。
  类似 MLflow 的 run，可以两两对比。实验次数也用于显著性修正（试得越多门槛越高）。
- 模型库（collection ``ml_models``）：从某次实验“存为模型”，带模型卡；可冻结（冻结后不能被覆盖，只能另存新版本）。
  状态：候选 candidate → 上线 active → 衰减预警 decaying → 退役 retired。
- 健康度：用冻结的模型在最新数据上预测，记录训练区间之后的滚动 RankIC 与特征漂移（PSI），连续走弱给出预警。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import numpy as np

from .models import _psi, build_dataset, model_card, predict_panel, spec_hash

RUNS = "ml_runs"
MODELS = "ml_models"
RUN_REPORTS = "ml_run_reports"
RUN_STATES = "ml_run_states"
REPORTS = "ml_reports"
STATES = "ml_states"

STATUS_LABELS = {"candidate": "候选", "active": "上线", "decaying": "衰减预警", "retired": "退役"}


def _now() -> float:
    return time.time()


class ModelLibrary:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ---------------- 实验 ----------------
    def record_run(
        self,
        spec: dict[str, Any],
        report: dict[str, Any],
        state: dict[str, Any] | None,
        *,
        source: str = "",
        project_id: str = "",
        note: str = "",
        created_by: str = "user",
    ) -> dict[str, Any]:
        rid = f"r_{uuid.uuid4().hex[:10]}"
        s = report.get("summary") or {}
        rec = {
            "id": rid,
            "spec_hash": spec_hash(spec),
            "model": spec["model"],
            "label": spec["label"],
            "horizon": spec["horizon"],
            "features": [f["name"] for f in spec["features"]],
            "seed": spec.get("seed"),
            "symbols": (report.get("panel") or {}).get("symbols") or spec.get("symbols"),
            "timeframe": spec.get("timeframe"),
            "summary": s,
            "admitted": report.get("admitted"),
            "status": "done",
            "source": source,
            "project_id": project_id,
            "note": note[:200],
            "synthetic": bool((report.get("panel") or {}).get("synthetic")),
            "created_by": created_by,
            "created_at": _now(),
        }
        self.store.upsert(RUNS, rec)
        self.store.put_blob(RUN_REPORTS, rid, report)
        if state is not None:
            self.store.put_blob(RUN_STATES, rid, state)
        return rec

    def runs(self, limit: int = 200, *, project_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list(RUNS)
        if project_id:
            rows = [r for r in rows if r.get("project_id") == project_id]
        return sorted(rows, key=lambda r: -(r.get("created_at") or 0))[:limit]

    def run(self, rid: str) -> dict[str, Any] | None:
        return self.store.get(RUNS, rid)

    def run_report(self, rid: str) -> dict[str, Any] | None:
        return self.store.get_blob(RUN_REPORTS, rid)

    def run_state(self, rid: str) -> dict[str, Any] | None:
        return self.store.get_blob(RUN_STATES, rid)

    def delete_run(self, rid: str) -> bool:
        self.store.delete_blob(RUN_REPORTS, rid)
        self.store.delete_blob(RUN_STATES, rid)
        return self.store.delete(RUNS, rid)

    def trial_count(self) -> int:
        return len({r.get("spec_hash") for r in self.store.list(RUNS)})

    # ---------------- 模型库 ----------------
    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list(MODELS)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return sorted(rows, key=lambda r: -(r.get("updated_at") or 0))

    def get(self, mid: str) -> dict[str, Any] | None:
        return self.store.get(MODELS, mid)

    def by_name(self, name: str) -> dict[str, Any] | None:
        return next((m for m in self.store.list(MODELS) if m.get("name") == name or m.get("id") == name), None)

    def save_from_run(
        self,
        rid: str,
        *,
        name: str = "",
        status: str = "candidate",
        note: str = "",
        model_id: str | None = None,
        project_id: str = "",
        created_by: str = "user",
    ) -> dict[str, Any]:
        run = self.run(rid)
        report = self.run_report(rid)
        state = self.run_state(rid)
        if run is None or report is None or state is None:
            raise KeyError("实验不存在或没有模型参数")
        prev = self.get(model_id) if model_id else None
        if prev and prev.get("frozen"):
            prev, model_id = None, None  # 冻结的模型不覆盖：另存一个新模型
        mid = model_id or f"m_{uuid.uuid4().hex[:8]}"
        spec = report["spec"]
        name = (name or (prev or {}).get("name") or f"{spec['model']}_{spec['label']}_{spec['horizon']}").strip()[:40]
        clash = next((m for m in self.store.list(MODELS) if m.get("name") == name and m.get("id") != mid), None)
        if clash:
            name = f"{name}_{mid[-4:]}"
        rec = {
            **(prev or {}),
            "id": mid,
            "name": name,
            "run_id": rid,
            "spec": spec,
            "summary": report.get("summary"),
            "checks": report.get("checks"),
            "admitted": report.get("admitted"),
            "card": model_card(spec, report, name=name),
            "status": status if status in STATUS_LABELS else "candidate",
            "frozen": False,
            "note": note[:400] or (prev or {}).get("note", ""),
            "project_id": project_id or (prev or {}).get("project_id"),
            "created_by": created_by,
            "train_end": (report.get("panel") or {}).get("end"),
            "versions": [
                *((prev or {}).get("versions") or []),
                {"run_id": rid, "at": _now(), "oos_rank_ic": (report.get("summary") or {}).get("oos_rank_ic")},
            ][-20:],
            "created_at": (prev or {}).get("created_at") or _now(),
            "updated_at": _now(),
        }
        self.store.upsert(MODELS, rec)
        self.store.put_blob(REPORTS, mid, report)
        self.store.put_blob(STATES, mid, state)
        return rec

    def report(self, mid: str) -> dict[str, Any] | None:
        return self.store.get_blob(REPORTS, mid)

    def state(self, mid: str) -> dict[str, Any] | None:
        return self.store.get_blob(STATES, mid)

    def patch(self, mid: str, **changes: Any) -> dict[str, Any] | None:
        rec = self.get(mid)
        if rec is None:
            return None
        if "status" in changes and changes["status"] not in STATUS_LABELS:
            changes.pop("status")
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()[:40] or rec["name"]
            if rec.get("card"):
                rec["card"]["name"] = changes["name"]
        rec.update(changes, updated_at=_now())
        self.store.upsert(MODELS, rec)
        return rec

    def delete(self, mid: str) -> bool:
        self.store.delete_blob(REPORTS, mid)
        self.store.delete_blob(STATES, mid)
        return self.store.delete(MODELS, mid)

    def search(self, query: str = "") -> list[dict[str, Any]]:
        q = (query or "").lower()
        rows = [
            r for r in self.list() if not q or q in (r.get("name", "") + str(r.get("spec", {}).get("model"))).lower()
        ]
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "model": r["spec"]["model"],
                "label": r["spec"]["label"],
                "status": r.get("status"),
                "oos_rank_ic": (r.get("summary") or {}).get("oos_rank_ic"),
            }
            for r in rows[:20]
        ]

    # ---------------- 健康度 ----------------
    def health(self, mid: str, panel: Any) -> dict[str, Any]:
        """在新面板上用冻结参数预测：训练区间之后的滚动 RankIC + 特征漂移。"""
        from .factor_eval import evaluate_matrix

        rec = self.get(mid)
        state = self.state(mid)
        if rec is None or state is None:
            raise KeyError("模型不存在")
        spec = rec["spec"]
        P = predict_panel(spec, state, panel)
        train_end = rec.get("train_end") or 0
        after = panel.times > train_end
        live_ic = None
        if after.sum() >= 30 and spec["label"] != "volatility":
            Q = np.where(after[:, None], P, np.nan)
            rep = evaluate_matrix(Q, panel, horizon=spec["horizon"], cost_bps=0)
            live_ic = rep["summary"]["rank_ic"]
        ds = build_dataset(spec, panel)
        psi = []
        old = ds.t_rows < np.quantile(ds.t_rows, 0.6)
        new = ds.t_rows >= np.quantile(ds.t_rows, 0.9)
        for j, name in enumerate(ds.feature_names):
            Xj = ds.X[ds.t_rows, ds.n_rows, j]
            psi.append({"name": name, "psi": _psi(Xj[old], Xj[new])})
        psi_max = max([p["psi"] for p in psi if p["psi"] is not None], default=None)
        entry = {"t": _now(), "live_rank_ic": live_ic, "live_bars": int(after.sum()), "psi_max": psi_max}
        h = [*(rec.get("health") or []), entry][-30:]
        flag = None
        base = (rec.get("summary") or {}).get("oos_rank_ic")
        recent = [x["live_rank_ic"] for x in h[-2:] if x.get("live_rank_ic") is not None]
        if (
            rec.get("status") == "active"
            and base
            and len(recent) == 2
            and all(abs(x) < abs(base) * 0.4 or x * base < 0 for x in recent)
        ):
            flag = "decaying"
        if psi_max is not None and psi_max >= 0.25 and rec.get("status") == "active":
            flag = flag or "drift"
        changes: dict[str, Any] = {"health": h}
        if flag == "decaying":
            changes["status"] = "decaying"
        self.patch(mid, **changes)
        return {
            "entry": entry,
            "psi": psi,
            "flag": flag,
            "latest": {
                "t": int(panel.times[-1]),
                "values": {
                    s: (None if not np.isfinite(P[-1, j]) else float(P[-1, j])) for j, s in enumerate(panel.symbols)
                },
            },
        }


__all__ = ["MODELS", "RUNS", "STATUS_LABELS", "ModelLibrary"]
