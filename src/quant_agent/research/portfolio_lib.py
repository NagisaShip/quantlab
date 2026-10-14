"""组合与风控的方案库与试验记录。

- 试验（collection ``pf_runs``）：每评估一次记一笔（配置摘要 + 指标），完整报告存 blob；用于对比与多重检验计数。
- 方案库（collection ``pf_schemes``）：存好的配置方案，策略里用 ``optimize('方案名')`` 调用。
  状态：候选 candidate → 启用 active → 退役 retired。
- 预期收益来自模型或因子时，在这里把信号算成 [T, N] 矩阵（模型按滚动重训，因子按表达式），再交给组合回测。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

RUNS = "pf_runs"
SCHEMES = "pf_schemes"
RUN_REPORTS = "pf_run_reports"
REPORTS = "pf_reports"

STATUS_LABELS = {"candidate": "候选", "active": "启用", "retired": "退役"}


def _now() -> float:
    return time.time()


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class PortfolioLibrary:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ---------------- 试验 ----------------
    def record_run(
        self,
        cfg: dict[str, Any],
        report: dict[str, Any],
        *,
        symbols: list[str],
        timeframe: str,
        source: str = "",
        project_id: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        rid = f"p_{uuid.uuid4().hex[:10]}"
        rec = {
            "id": rid,
            "hash": config_hash(cfg),
            "optimizer": cfg.get("optimizer"),
            "risk_model": cfg.get("risk_model"),
            "expected": cfg.get("expected"),
            "symbols": symbols,
            "timeframe": timeframe,
            "summary": report.get("summary"),
            "admitted": report.get("admitted"),
            "source": source,
            "project_id": project_id,
            "note": note[:200],
            "synthetic": bool((report.get("panel") or {}).get("synthetic")),
            "created_at": _now(),
        }
        self.store.upsert(RUNS, rec)
        self.store.put_blob(RUN_REPORTS, rid, report)
        return rec

    def runs(self, limit: int = 200) -> list[dict[str, Any]]:
        return sorted(self.store.list(RUNS), key=lambda r: -(r.get("created_at") or 0))[:limit]

    def run(self, rid: str) -> dict[str, Any] | None:
        return self.store.get(RUNS, rid)

    def run_report(self, rid: str) -> dict[str, Any] | None:
        return self.store.get_blob(RUN_REPORTS, rid)

    def delete_run(self, rid: str) -> bool:
        self.store.delete_blob(RUN_REPORTS, rid)
        return self.store.delete(RUNS, rid)

    def trial_count(self) -> int:
        return len({r.get("hash") for r in self.store.list(RUNS)})

    # ---------------- 方案库 ----------------
    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list(SCHEMES)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return sorted(rows, key=lambda r: -(r.get("updated_at") or 0))

    def get(self, sid: str) -> dict[str, Any] | None:
        return self.store.get(SCHEMES, sid)

    def by_name(self, name: str) -> dict[str, Any] | None:
        return next((m for m in self.store.list(SCHEMES) if m.get("name") == name or m.get("id") == name), None)

    def save(
        self,
        *,
        name: str,
        config: dict[str, Any],
        report: dict[str, Any] | None,
        symbols: list[str],
        timeframe: str,
        status: str = "candidate",
        note: str = "",
        scheme_id: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        created_by: str = "user",
    ) -> dict[str, Any]:
        prev = self.get(scheme_id) if scheme_id else None
        sid = scheme_id or f"pf_{uuid.uuid4().hex[:8]}"
        name = (name or (prev or {}).get("name") or f"{config.get('optimizer')}_{len(symbols)}").strip()[:40]
        clash = next((m for m in self.store.list(SCHEMES) if m.get("name") == name and m.get("id") != sid), None)
        if clash:
            name = f"{name}_{sid[-4:]}"
        rec = {
            "id": sid,
            "name": name,
            "config": {**config, "timeframe": timeframe},
            "symbols": symbols,
            "timeframe": timeframe,
            "summary": (report or {}).get("summary"),
            "admitted": (report or {}).get("admitted"),
            "latest": (report or {}).get("latest"),
            "status": status if status in STATUS_LABELS else "candidate",
            "note": note[:400] or (prev or {}).get("note", ""),
            "project_id": project_id or (prev or {}).get("project_id"),
            "run_id": run_id,
            "created_by": created_by,
            "created_at": (prev or {}).get("created_at") or _now(),
            "updated_at": _now(),
        }
        self.store.upsert(SCHEMES, rec)
        if report is not None:
            self.store.put_blob(REPORTS, sid, report)
        return rec

    def report(self, sid: str) -> dict[str, Any] | None:
        return self.store.get_blob(REPORTS, sid)

    def patch(self, sid: str, **changes: Any) -> dict[str, Any] | None:
        rec = self.get(sid)
        if rec is None:
            return None
        if "status" in changes and changes["status"] not in STATUS_LABELS:
            changes.pop("status")
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()[:40] or rec["name"]
        rec.update(changes, updated_at=_now())
        self.store.upsert(SCHEMES, rec)
        return rec

    def delete(self, sid: str) -> bool:
        self.store.delete_blob(REPORTS, sid)
        return self.store.delete(SCHEMES, sid)

    def search(self, query: str = "") -> list[dict[str, Any]]:
        q = (query or "").lower()
        rows = [r for r in self.list() if not q or q in (r.get("name", "") + str(r["config"].get("optimizer"))).lower()]
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "optimizer": r["config"].get("optimizer"),
                "status": r.get("status"),
                "sharpe": (r.get("summary") or {}).get("sharpe"),
                "max_drawdown": (r.get("summary") or {}).get("max_drawdown"),
            }
            for r in rows[:20]
        ]


def signal_for(cfg: dict[str, Any], panel: Any, store: Any, *, stop: Any = None) -> tuple[Any, float, list[str]]:
    """expected = model:名字 / factor:名字 时，算出 [T, N] 信号矩阵与它的 IC（用于 Grinold 换算）。"""
    src = str(cfg.get("expected") or "none")
    warnings: list[str] = []
    if src.startswith("model:"):
        from .model_lib import ModelLibrary
        from .models import ModelError, rolling_predict

        name = src[6:]
        rec = ModelLibrary(store).by_name(name)
        if rec is None:
            raise ValueError(f"模型库里没有「{name}」")
        try:
            P, info = rolling_predict({**rec["spec"], "symbols": panel.symbols}, panel, stop=stop)
        except ModelError as exc:
            raise ValueError(f"模型「{name}」无法在这组标的上预测：{exc}") from exc
        ic = abs(float((rec.get("summary") or {}).get("oos_rank_ic") or 0.03)) or 0.03
        warnings.append(
            f"预期收益来自模型「{name}」的滚动重训预测（每 {info['retrain_every']} 根重训），按 IC {ic:.3f} 换算"
        )
        return P, ic, warnings
    if src.startswith("factor:"):
        from .factor_expr import evaluate, parse
        from .factor_lib import FactorLibrary

        name = src[7:]
        rec = FactorLibrary(store).by_name(name)
        expr = rec["expression"] if rec else name
        F = evaluate(parse(expr), panel.data)
        ic = abs(float(((rec or {}).get("summary") or {}).get("rank_ic") or 0.03)) or 0.03
        warnings.append(f"预期收益来自因子「{name}」，按 IC {ic:.3f} 换算")
        return F, ic, warnings
    return None, 0.03, warnings


__all__ = ["STATUS_LABELS", "PortfolioLibrary", "config_hash", "signal_for"]
