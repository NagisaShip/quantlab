"""部署级 worker 调度配置：并发槽位与自动排队说明。"""

from __future__ import annotations

import time
from typing import Any

from ..store import Collections

DEFAULT_WORKER_SLOTS = 8
MIN_WORKER_SLOTS = 1
MAX_WORKER_SLOTS = 32
WORKER_CONFIG_ID = "worker_config"


def _slots(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WORKER_SLOTS
    return max(MIN_WORKER_SLOTS, min(MAX_WORKER_SLOTS, n))


def read_worker_config(global_store: Any) -> dict[str, Any]:
    """读取部署级配置；工人进程每个新进程会读一次并周期刷新。"""
    row: dict[str, Any] = {}
    if global_store is not None:
        try:
            row = global_store.get(Collections.SETTINGS, WORKER_CONFIG_ID) or {}
        except Exception:
            row = {}
    configured = bool(row.get("updated_at"))
    return {
        "slots": _slots(row.get("slots", DEFAULT_WORKER_SLOTS)),
        "min": MIN_WORKER_SLOTS,
        "max": MAX_WORKER_SLOTS,
        "default": DEFAULT_WORKER_SLOTS,
        "configured": configured,
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by") or "",
    }


def write_worker_config(global_store: Any, slots: int, *, user_id: str = "") -> dict[str, Any]:
    if global_store is None:
        raise RuntimeError("当前部署没有全局配置存储")
    value = _slots(slots)
    now = time.time()
    global_store.upsert(
        Collections.SETTINGS,
        {
            "id": WORKER_CONFIG_ID,
            "slots": value,
            "updated_at": now,
            "updated_by": user_id,
        },
    )
    return read_worker_config(global_store)


__all__ = [
    "DEFAULT_WORKER_SLOTS",
    "MAX_WORKER_SLOTS",
    "MIN_WORKER_SLOTS",
    "WORKER_CONFIG_ID",
    "read_worker_config",
    "write_worker_config",
]
