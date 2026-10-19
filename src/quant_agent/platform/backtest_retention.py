"""研究记录保留策略：统一记录单位、单策略上限、自动清理与 PIN 保护。

回测、参数优化、因子、模型、组合和执行记录都保存在工作区 JSON store 中。
这里按记录类型倍率折算为“记录单位”，把不同体积的研究产物放进同一个
配额体系；管理员可以在工作区设置里调整每用户单位上限和各类倍率。

PIN 只保护记录不被自动清理或普通删除，用户可以主动取消 PIN 后再删除。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..store import Collections
from .errors import AppError, ErrorCode

DEFAULT_MEMBER_RECORD_UNITS = 200
DEFAULT_MAX_RECORDS = 100_000  # 兼容旧名称；实际表示每用户记录单位上限
DEFAULT_PER_STRATEGY_LIMIT = 5
RETENTION_SETTING_PREFIX = "backtest_retention:"
RECORD_UNIT_BYTES = 350 * 1024
RESERVED_SPACE_RATIO = 0.2


def _created_by(row: dict[str, Any]) -> str:
    return str(row.get("created_by") or "")


def strategy_key(row: dict[str, Any]) -> str:
    slug = str(row.get("slug") or "").strip()
    if slug:
        return f"slug:{slug}"
    return f"inline:{row.get('strategy_name') or '未命名策略'!s}"


def strategy_label(row: dict[str, Any]) -> str:
    return str(row.get("strategy_name") or row.get("slug") or "未命名策略")


def _created(row: dict[str, Any]) -> float:
    try:
        return float(row.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def user_rows(store: Any, user_id: str) -> list[dict[str, Any]]:
    return [row for row in store.list(Collections.BACKTESTS) if _created_by(row) == user_id]


def user_prefs(store: Any, user_id: str) -> dict[str, Any]:
    raw = store.get(Collections.SETTINGS, f"{RETENTION_SETTING_PREFIX}{user_id}") or {}
    try:
        per_strategy = int(raw.get("per_strategy_limit", DEFAULT_PER_STRATEGY_LIMIT))
    except (TypeError, ValueError):
        per_strategy = DEFAULT_PER_STRATEGY_LIMIT
    return {
        "auto_delete_oldest": bool(raw.get("auto_delete_oldest", False)),
        "per_strategy_limit": max(1, min(100, per_strategy)),
        "auto_delete_strategy_oldest": bool(raw.get("auto_delete_strategy_oldest", True)),
    }


def save_user_prefs(store: Any, user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    prefs = {
        "auto_delete_oldest": bool(payload.get("auto_delete_oldest", False)),
        "per_strategy_limit": max(
            1,
            min(100, int(payload.get("per_strategy_limit", DEFAULT_PER_STRATEGY_LIMIT) or DEFAULT_PER_STRATEGY_LIMIT)),
        ),
        "auto_delete_strategy_oldest": bool(payload.get("auto_delete_strategy_oldest", True)),
    }
    store.upsert(
        Collections.SETTINGS,
        {"id": f"{RETENTION_SETTING_PREFIX}{user_id}", **prefs, "updated_at": time.time()},
    )
    return prefs


def data_home() -> Path:
    env = os.environ.get("QAGENT_HOME", "").strip()
    return Path(env) if env else Path.home() / ".quant_agent"


def workspace_hard_max(home: str | Path | None = None) -> dict[str, Any]:
    """每成员记录单位的硬上限：服务器空间 20% 折算成单位，再与 100k 取小。"""
    path = Path(home) if home else data_home()
    try:
        disk_total = int(shutil.disk_usage(path).total)
    except OSError:
        try:
            disk_total = int(shutil.disk_usage(path.anchor or ".").total)
        except OSError:
            disk_total = 0
    reserved = int(disk_total * RESERVED_SPACE_RATIO) if disk_total else 0
    by_disk = reserved // RECORD_UNIT_BYTES if reserved else DEFAULT_MAX_RECORDS
    hard = max(1, min(DEFAULT_MAX_RECORDS, int(by_disk)))
    return {
        "hard_max_units": hard,
        "default_max_units": DEFAULT_MAX_RECORDS,
        "disk_total_bytes": disk_total,
        "reserved_bytes": reserved,
        "reserved_ratio": RESERVED_SPACE_RATIO,
        "record_unit_bytes": RECORD_UNIT_BYTES,
    }


def workspace_limit(auth: Any, workspace_id: str) -> int:
    # backtest_max_records_per_user 是历史配置键；保留它兼容已有工作区。
    settings = (auth.workspace(workspace_id).get("settings") or {}) if auth is not None else {}
    quotas = settings.get("quotas") or {}
    try:
        configured = int(quotas.get("backtest_max_records_per_user", DEFAULT_MAX_RECORDS))
    except (TypeError, ValueError):
        configured = DEFAULT_MAX_RECORDS
    return max(1, min(configured, int(workspace_hard_max()["hard_max_units"])))


def usage(store: Any, user_id: str, *, max_records: int, prefs: dict[str, Any]) -> dict[str, Any]:
    rows = user_rows(store, user_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(strategy_key(row), []).append(row)
    strategies = []
    for key, group in grouped.items():
        group.sort(key=lambda r: (_created(r), r.get("id", "")))
        strategies.append(
            {
                "key": key,
                "name": strategy_label(group[-1]),
                "count": len(group),
                "pinned": sum(1 for r in group if r.get("pinned")),
                "limit": int(prefs["per_strategy_limit"]),
                "over_limit": len(group) > int(prefs["per_strategy_limit"]),
            }
        )
    strategies.sort(key=lambda x: (-x["count"], x["name"]))
    pinned = sum(1 for r in rows if r.get("pinned"))
    return {
        "total": len(rows),
        "pinned": pinned,
        "unpinned": len(rows) - pinned,
        "max_records_per_user": max_records,
        "remaining": max(0, max_records - len(rows)),
        "strategies": strategies,
    }


def _blocked(message: str, **details: Any) -> AppError:
    return AppError(ErrorCode.QUOTA_EXHAUSTED, message, details=details)


def _oldest_unpinned(rows: list[dict[str, Any]], exclude: set[str], limit: int) -> list[dict[str, Any]]:
    candidates = [r for r in rows if not r.get("pinned") and str(r.get("id")) not in exclude]
    candidates.sort(key=lambda r: (_created(r), r.get("id", "")))
    return candidates[:limit]


def plan_for_new(
    rows: list[dict[str, Any]],
    pending: dict[str, Any],
    *,
    max_records: int,
    prefs: dict[str, Any],
    check_total: bool = True,
) -> list[dict[str, Any]]:
    """返回为了容纳 pending 而需要删除的旧记录；不能安全删除时抛 QUOTA_EXHAUSTED。

    `check_total=False` 时只检查单策略上限。统一配额的总量检查由
    :func:`guard_new_record` 按记录单位完成，避免把可配置倍率误当成记录条数。
    """
    existing = [r for r in rows if str(r.get("id")) != str(pending.get("id"))]
    strategy = strategy_key(pending)
    same = [r for r in existing if strategy_key(r) == strategy]
    per_limit = int(prefs["per_strategy_limit"])
    overflow_strategy = max(0, len(same) + 1 - per_limit)
    deletions: list[dict[str, Any]] = []
    excluded: set[str] = set()

    if overflow_strategy:
        if not prefs.get("auto_delete_strategy_oldest", True):
            raise _blocked(
                f"策略「{strategy_label(pending)}」已有 {len(same)} 条回测，单策略上限为 {per_limit}。"
                "请先删除旧记录，或在设置中开启单策略自动清理。",
                reason="strategy_limit",
                strategy=strategy_label(pending),
                used=len(same),
                limit=per_limit,
            )
        picked = _oldest_unpinned(same, excluded, overflow_strategy)
        if len(picked) < overflow_strategy:
            raise _blocked(
                f"策略「{strategy_label(pending)}」达到上限，但没有足够的未 PIN 旧记录可自动清理。"
                "请取消部分 PIN 或手动删除旧记录。",
                reason="strategy_pinned",
                strategy=strategy_label(pending),
                used=len(same),
                limit=per_limit,
                deletable=len(picked),
            )
        for row in picked:
            excluded.add(str(row.get("id")))
            deletions.append(row)

    if not check_total:
        return deletions

    after_strategy = len(existing) + 1 - len(deletions)
    overflow_total = max(0, after_strategy - max_records)
    if overflow_total:
        if not prefs.get("auto_delete_oldest", False):
            raise _blocked(
                f"你的回测记录已有 {len(existing)} 条，工作区上限为 {max_records}。"
                "请删除历史记录，或在设置中开启自动清理最久远记录。",
                reason="total_limit",
                used=len(existing),
                limit=max_records,
            )
        picked = _oldest_unpinned(existing, excluded, overflow_total)
        if len(picked) < overflow_total:
            raise _blocked(
                f"回测记录达到工作区上限 {max_records}，但没有足够的未 PIN 旧记录可自动清理。"
                "请取消部分 PIN 或手动删除历史记录。",
                reason="total_pinned",
                used=len(existing),
                limit=max_records,
                deletable=len(picked),
            )
        deletions.extend(picked)
    return deletions


def preflight(store: Any, auth: Any, workspace_id: str, user_id: str, *, slug: str | None, strategy_name: str) -> None:
    if not user_id:
        return
    rows = user_rows(store, user_id)
    pending = {
        "id": "__pending__",
        "slug": slug,
        "strategy_name": strategy_name,
        "created_at": time.time() + 1,
        "created_by": user_id,
        "pinned": False,
    }
    plan_for_new(
        rows,
        pending,
        max_records=workspace_limit(auth, workspace_id),
        prefs=user_prefs(store, user_id),
        check_total=False,
    )


RECORD_MULTIPLIERS_ID = "record_retention_multipliers"


@dataclass(frozen=True)
class RecordType:
    key: str
    label: str
    collection: str
    blob_kinds: tuple[str, ...]
    title_fields: tuple[str, ...]
    default_multiplier: float
    approx_bytes: int = 0


# 默认倍率按当前工作区的典型落盘体积估算：回测约 350 KB 记为 1.0 单位；
# 其余类型按报告、状态和索引文件合计的相对大小折算。管理员可在设置中逐项调整。
RECORD_TYPES: tuple[RecordType, ...] = (
    RecordType(
        "backtest",
        "回测记录",
        Collections.BACKTESTS,
        ("backtests", "research_snapshots"),
        ("strategy_name", "label"),
        1.0,
        350 * 1024,
    ),
    RecordType(
        "optimization",
        "参数优化",
        Collections.OPTIMIZATIONS,
        ("optimizations",),
        ("strategy_name", "id"),
        2.0,
        700 * 1024,
    ),
    RecordType("factor", "因子", "factors", ("factor_reports",), ("name", "expression"), 0.18, 63 * 1024),
    RecordType("model", "模型", "ml_models", ("ml_reports", "ml_states"), ("name", "id"), 0.25, 88 * 1024),
    RecordType(
        "model_run",
        "模型实验",
        "ml_runs",
        ("ml_run_reports", "ml_run_states"),
        ("model", "label", "id"),
        0.20,
        70 * 1024,
    ),
    RecordType("portfolio", "组合方案", "pf_schemes", ("pf_reports",), ("name", "id"), 0.10, 35 * 1024),
    RecordType("portfolio_run", "组合试验", "pf_runs", ("pf_run_reports",), ("optimizer", "id"), 0.22, 77 * 1024),
    RecordType("execution", "执行方案", "ex_schemes", ("ex_reports",), ("name", "id"), 0.10, 35 * 1024),
    RecordType("execution_run", "执行试验", "ex_runs", ("ex_run_reports",), ("algo", "id"), 0.06, 21 * 1024),
)
RECORD_TYPE_BY_KEY = {item.key: item for item in RECORD_TYPES}


def record_types_view() -> list[dict[str, Any]]:
    """给 API/前端使用的可序列化类型目录。"""
    return [
        {
            "key": item.key,
            "label": item.label,
            "default_multiplier": item.default_multiplier,
            "approx_bytes": item.approx_bytes,
        }
        for item in RECORD_TYPES
    ]


def record_multipliers(store: Any) -> dict[str, float]:
    raw = store.get(Collections.SETTINGS, RECORD_MULTIPLIERS_ID) or {}
    values = raw.get("multipliers") if isinstance(raw.get("multipliers"), dict) else {}
    out: dict[str, float] = {}
    for item in RECORD_TYPES:
        try:
            value = float(values.get(item.key, item.default_multiplier))
        except (TypeError, ValueError):
            value = item.default_multiplier
        out[item.key] = round(max(0.01, min(20.0, value)), 3)
    return out


def save_record_multipliers(store: Any, values: dict[str, Any]) -> dict[str, float]:
    current = record_multipliers(store)
    for key, value in (values or {}).items():
        if key not in current:
            continue
        try:
            current[key] = round(max(0.01, min(20.0, float(value))), 3)
        except (TypeError, ValueError):
            continue
    store.upsert(
        Collections.SETTINGS,
        {"id": RECORD_MULTIPLIERS_ID, "multipliers": current, "updated_at": time.time()},
    )
    return current


def _record_owner(row: dict[str, Any]) -> str:
    return str(row.get("created_by") or "")


def _record_time(row: dict[str, Any]) -> float:
    try:
        return float(row.get("created_at") or row.get("updated_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _record_title(item: RecordType, row: dict[str, Any]) -> str:
    for field in item.title_fields:
        value = str(row.get(field) or "").strip()
        if value:
            return value[:80]
    return str(row.get("id") or "未命名记录")


def _typed_rows(store: Any, user_id: str, *, include_legacy: bool = False) -> list[tuple[RecordType, dict[str, Any]]]:
    out: list[tuple[RecordType, dict[str, Any]]] = []
    for item in RECORD_TYPES:
        for row in store.list(item.collection):
            owner = _record_owner(row)
            if owner == user_id or (include_legacy and owner in ("", "user")):
                out.append((item, row))
    return out


def record_usage(
    store: Any,
    user_id: str,
    *,
    include_legacy: bool = False,
    prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    multipliers = record_multipliers(store)
    by_type: dict[str, dict[str, Any]] = {
        item.key: {
            "key": item.key,
            "label": item.label,
            "count": 0,
            "pinned": 0,
            "multiplier": multipliers[item.key],
            "default_multiplier": item.default_multiplier,
            "approx_bytes": item.approx_bytes,
            "units": 0.0,
        }
        for item in RECORD_TYPES
    }
    all_rows = _typed_rows(store, user_id, include_legacy=include_legacy)
    for item, row in all_rows:
        entry = by_type[item.key]
        entry["count"] += 1
        entry["pinned"] += 1 if row.get("pinned") else 0
        entry["units"] += multipliers[item.key]
    types = list(by_type.values())
    for entry in types:
        entry["units"] = round(float(entry["units"]), 3)
    result: dict[str, Any] = {
        "total_records": len(all_rows),
        "pinned_records": sum(1 for _, row in all_rows if row.get("pinned")),
        "total_units": round(sum(float(entry["units"]) for entry in types), 3),
        "approx_bytes": sum(int(entry["count"]) * int(entry["approx_bytes"]) for entry in types),
        "types": types,
    }
    if prefs is not None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item, row in all_rows:
            if item.key == "backtest":
                grouped.setdefault(strategy_key(row), []).append(row)
        strategies = []
        for key, group in grouped.items():
            group.sort(key=lambda r: (_created(r), r.get("id", "")))
            strategies.append(
                {
                    "key": key,
                    "name": strategy_label(group[-1]),
                    "count": len(group),
                    "pinned": sum(1 for r in group if r.get("pinned")),
                    "limit": int(prefs["per_strategy_limit"]),
                    "over_limit": len(group) > int(prefs["per_strategy_limit"]),
                }
            )
        strategies.sort(key=lambda x: (-x["count"], x["name"]))
        result["strategies"] = strategies
    return result


def record_rows_for_user(
    store: Any,
    user_id: str,
    *,
    type_key: str | None = None,
    include_legacy: bool = False,
    q: str | None = None,
    before_time: float | None = None,
    before_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    multipliers = record_multipliers(store)
    rows = []
    term = (q or "").strip().casefold()
    for item, row in _typed_rows(store, user_id, include_legacy=include_legacy):
        if type_key and item.key != type_key:
            continue
        out = {
            **row,
            "record_type": item.key,
            "record_type_label": item.label,
            "record_title": _record_title(item, row),
            "record_units": multipliers[item.key],
            "approx_bytes": item.approx_bytes,
        }
        if term and not any(
            term in str(out.get(key) or "").casefold()
            for key in ("id", "record_title", "record_type", "record_type_label", "strategy_name", "slug")
        ):
            continue
        rows.append(out)
    rows.sort(key=lambda row: (_record_time(row), str(row.get("id") or "")), reverse=True)
    if before_time is not None:
        cursor = (float(before_time), before_id or "")
        rows = [row for row in rows if (_record_time(row), str(row.get("id") or "")) < cursor]
    return rows[: max(1, min(limit, 1000))]


def delete_record(store: Any, workspace_id: str, item: RecordType, row: dict[str, Any]) -> bool:
    if item.key == "backtest":
        return bool(delete_backtest_record(store, workspace_id, row).get("deleted"))
    rid = str(row.get("id") or "")
    if not rid:
        return False
    for blob in item.blob_kinds:
        store.delete_blob(blob, rid)
    return store.delete(item.collection, rid)


def toggle_record_pin(store: Any, item: RecordType, row: dict[str, Any]) -> dict[str, Any]:
    row["pinned"] = not bool(row.get("pinned"))
    row["pinned_at"] = time.time() if row["pinned"] else None
    return store.upsert(item.collection, row)


def _unpinned_by_age(store: Any, user_id: str, *, include_legacy: bool) -> list[tuple[RecordType, dict[str, Any]]]:
    rows = [
        (item, row) for item, row in _typed_rows(store, user_id, include_legacy=include_legacy) if not row.get("pinned")
    ]
    rows.sort(key=lambda pair: _record_time(pair[1]))
    return rows


def _require_room(usage: dict[str, Any], requested: float, max_units: int, prefs: dict[str, Any]) -> float:
    over = float(usage["total_units"]) + requested - max_units
    if over <= 0:
        return 0.0
    if not prefs.get("auto_delete_oldest", False):
        raise _blocked(
            f"你的研究记录已占用 {usage['total_units']:.2f} / {max_units:,} 记录单位，"
            f"本次记录需要 {requested:.2f} 单位。请删除旧记录，或在设置中开启自动清理最久远记录。",
            reason="record_units",
            used=usage["total_units"],
            limit=max_units,
            requested=requested,
        )
    return over


def guard_new_record(
    store: Any,
    auth: Any,
    workspace_id: str,
    user_id: str,
    role: str | None,
    type_key: str,
    *,
    max_units: float | None = None,
) -> list[dict[str, Any]]:
    """新记录入队 / 保存前的统一硬校验；自动清理开启时先删最旧的未 PIN 记录。"""
    if not user_id or type_key not in RECORD_TYPE_BY_KEY:
        return []
    include_legacy = role == "admin"
    cap = max_units if max_units is not None else workspace_limit(auth, workspace_id)
    prefs = user_prefs(store, user_id)
    usage = record_usage(store, user_id, include_legacy=include_legacy)
    requested = record_multipliers(store)[type_key]
    over = _require_room(usage, requested, cap, prefs)
    deleted: list[dict[str, Any]] = []
    if over <= 0:
        return deleted
    freed = 0.0
    multiples = record_multipliers(store)
    for item, row in _unpinned_by_age(store, user_id, include_legacy=include_legacy):
        if freed >= over:
            break
        if delete_record(store, workspace_id, item, row):
            freed += multiples[item.key]
            deleted.append({**row, "record_type": item.key, "record_type_label": item.label})
    if freed + 1e-9 < over:
        raise _blocked(
            f"研究记录达到上限 {cap:,} 单位，但没有足够的未 PIN 记录可自动清理。请取消部分 PIN 或手动删除。",
            reason="record_units_pinned",
            used=usage["total_units"],
            limit=cap,
            requested=requested,
            deletable_units=round(freed, 3),
        )
    return deleted


def effective_limit(
    auth: Any,
    model_hub: Any,
    workspace_id: str,
    user_id: str,
    role: str | None,
) -> float:
    """返回成员可用的记录单位上限：工作区上限与成员记录限额取较小值。"""
    cap = workspace_limit(auth, workspace_id)
    if not user_id or role == "admin" or model_hub is None:
        return cap
    try:
        limits = model_hub.policy(workspace_id, user_id, role).get("limits") or []
    except Exception:
        return min(float(cap), float(DEFAULT_MEMBER_RECORD_UNITS))
    for rule in limits:
        if rule.get("metric") != "record_units":
            continue
        try:
            value = float(rule.get("limit"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return min(cap, value)
    # 没有单独的 record_units 规则时使用成员默认 200 单位。
    return min(float(cap), float(DEFAULT_MEMBER_RECORD_UNITS))


def cleanup_total(
    store: Any, workspace_id: str, user_id: str, *, max_units: int, role: str | None = None
) -> list[dict[str, Any]]:
    include_legacy = role == "admin"
    usage = record_usage(store, user_id, include_legacy=include_legacy)
    over = float(usage["total_units"]) - max_units
    if over <= 0:
        return []
    multiples = record_multipliers(store)
    freed = 0.0
    deleted: list[dict[str, Any]] = []
    for item, row in _unpinned_by_age(store, user_id, include_legacy=include_legacy):
        if freed >= over:
            break
        if delete_record(store, workspace_id, item, row):
            freed += multiples[item.key]
            deleted.append({**row, "record_type": item.key, "record_type_label": item.label})
    return deleted


def enforce_total_after_persist(
    store: Any, auth: Any, workspace_id: str, user_id: str, role: str | None, *, max_units: float | None = None
) -> list[dict[str, Any]]:
    if not user_id:
        return []
    prefs = user_prefs(store, user_id)
    if not prefs.get("auto_delete_oldest", False):
        return []
    cap = max_units if max_units is not None else workspace_limit(auth, workspace_id)
    return cleanup_total(store, workspace_id, user_id, max_units=cap, role=role)


def delete_backtest_record(store: Any, workspace_id: str, row: dict[str, Any]) -> dict[str, Any]:
    bt_id = str(row.get("id") or "")
    if not bt_id:
        return {"deleted": False}
    store.delete(Collections.BACKTESTS, bt_id)
    store.delete_blob("backtests", bt_id)
    store.delete_blob("research_snapshots", bt_id)
    run_id = str(row.get("run_id") or "")
    if run_id:
        store.delete_result("run_results", run_id)
    return {"deleted": True, "id": bt_id, "workspace_id": workspace_id}


def enforce_after_persist(
    store: Any, auth: Any, workspace_id: str, user_id: str, row: dict[str, Any]
) -> list[dict[str, Any]]:
    if not user_id:
        return []
    rows = user_rows(store, user_id)
    deletions = plan_for_new(
        rows,
        row,
        max_records=workspace_limit(auth, workspace_id),
        prefs=user_prefs(store, user_id),
        check_total=False,
    )
    for old in deletions:
        delete_backtest_record(store, workspace_id, old)
    return deletions


def cleanup_existing(
    store: Any, workspace_id: str, user_id: str, *, max_records: int, prefs: dict[str, Any]
) -> list[dict[str, Any]]:
    """显式清理：按单策略上限和总量上限删除最旧的未 PIN 记录。"""
    rows = user_rows(store, user_id)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(strategy_key(row), []).append(row)
    deletions: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_limit = int(prefs["per_strategy_limit"])
    for group in grouped.values():
        overflow = max(0, len(group) - per_limit)
        if overflow:
            for row in _oldest_unpinned(group, seen, overflow):
                seen.add(str(row.get("id")))
                deletions.append(row)
    remaining_after = len(rows) - len(deletions)
    if remaining_after > max_records:
        for row in _oldest_unpinned(rows, seen, remaining_after - max_records):
            seen.add(str(row.get("id")))
            deletions.append(row)
    for row in deletions:
        delete_backtest_record(store, workspace_id, row)
    return deletions
