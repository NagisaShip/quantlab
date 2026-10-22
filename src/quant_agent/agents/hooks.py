"""进程内 hooks 注册表与执行器：默认关闭，不启动进程、不执行外部命令。

- 注册表保存元数据 + 进程内可调用 handler；HTTP 只能切换开关/查看，不能注册任意代码。
- ``run_hooks(trigger, payload)`` 由 Lab 工具执行器调用；handler 异常只记录，不影响工具结果。
- hooks 是可信进程内代码，不能作为外部命令入口。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

TRIGGERS: dict[str, dict[str, Any]] = {
    "before_tool": {
        "label": "工具调用前",
        "description": "工具执行前触发，可用于审计、参数检查；只执行进程内注册的受信 handler。",
    },
    "after_tool": {
        "label": "工具调用后",
        "description": "工具执行后触发，可用于审计、结果脱敏；只执行进程内注册的受信 handler。",
    },
}

EXECUTION_STATUS = "in_process"
_EXTERNAL_COMMANDS = False
_LOCK = threading.Lock()
_REGISTRY: dict[str, dict[str, Any]] = {}
_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {}
_EVENTS: list[dict[str, Any]] = []
MAX_EVENTS = 200


def _record(
    hook_id: str,
    *,
    name: str,
    trigger: str,
    description: str,
    enabled: bool = False,
    source: str = "builtin",
) -> dict[str, Any]:
    if trigger not in TRIGGERS:
        raise ValueError(f"不支持的 hook 触发点：{trigger}（可用：{'、'.join(TRIGGERS)}）")
    if not str(hook_id or "").strip():
        raise ValueError("hook id 不能为空")
    return {
        "id": str(hook_id)[:64],
        "name": str(name or hook_id)[:80],
        "trigger": trigger,
        "enabled": bool(enabled),
        "source": str(source or "builtin")[:32],
        "description": str(description or "")[:300],
        "executes_external_commands": _EXTERNAL_COMMANDS,
        "execution": EXECUTION_STATUS,
    }


def register_hook(
    hook_id: str,
    *,
    name: str = "",
    trigger: str,
    description: str = "",
    enabled: bool = False,
    source: str = "builtin",
    handler: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """注册一个进程内 hook；handler 只能由受信代码传入，HTTP 没有注册入口。"""
    record = _record(hook_id, name=name, trigger=trigger, description=description, enabled=enabled, source=source)
    with _LOCK:
        existing = _REGISTRY.get(record["id"])
        record["created_at"] = existing.get("created_at") if existing is not None else time.time()
        record["updated_at"] = time.time()
        _REGISTRY[record["id"]] = record
        if handler is not None:
            _HANDLERS[record["id"]] = handler
        record["handler_registered"] = record["id"] in _HANDLERS
        return dict(record)


def unregister_hook(hook_id: str) -> bool:
    with _LOCK:
        _HANDLERS.pop(str(hook_id), None)
        return _REGISTRY.pop(str(hook_id), None) is not None


def set_enabled(hook_id: str, enabled: bool) -> dict[str, Any] | None:
    with _LOCK:
        record = _REGISTRY.get(str(hook_id))
        if record is None:
            return None
        record["enabled"] = bool(enabled)
        record["updated_at"] = time.time()
        return dict(record)


def _audit(payload: dict[str, Any]) -> dict[str, Any]:
    event = {"at": time.time(), **{k: payload.get(k) for k in ("pid", "tool", "ok", "elapsed_ms")}}
    with _LOCK:
        _EVENTS.append(event)
        del _EVENTS[:-MAX_EVENTS]
    return {"recorded": True}


def run_hooks(trigger: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """执行某触发点已启用的 handler；单个 handler 失败只记录，不抛给工具调用。"""
    results: list[dict[str, Any]] = []
    with _LOCK:
        records = [dict(record) for record in _REGISTRY.values() if record["trigger"] == trigger and record["enabled"]]
    for record in records:
        handler = _HANDLERS.get(record["id"])
        if handler is None:
            continue
        started = time.perf_counter()
        try:
            value = handler(dict(payload))
            item = {"id": record["id"], "ok": True, "result": value}
        except Exception as exc:
            item = {"id": record["id"], "ok": False, "error": str(exc)[:300]}
        item["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        results.append(item)
    return results


def recent_events(limit: int = 50) -> list[dict[str, Any]]:
    with _LOCK:
        return list(_EVENTS[-max(1, min(int(limit or 50), MAX_EVENTS)) :])


def _register_builtins() -> None:
    register_hook(
        "builtin.audit_before_tool",
        name="工具调用审计（前）",
        trigger="before_tool",
        description="记录工具名与参数摘要；默认关闭，启用后只写进程内事件，不执行外部命令。",
        handler=_audit,
    )
    register_hook(
        "builtin.audit_after_tool",
        name="工具调用审计（后）",
        trigger="after_tool",
        description="记录工具结果状态与耗时；默认关闭，启用后只写进程内事件。",
        handler=_audit,
    )


def list_hooks() -> dict[str, Any]:
    with _LOCK:
        hooks = [dict(record, handler_registered=record["id"] in _HANDLERS) for record in _REGISTRY.values()]
    hooks.sort(key=lambda record: (record["trigger"], record["id"]))
    enabled = [record["id"] for record in hooks if record["enabled"]]
    return {
        "hooks": hooks,
        "count": len(hooks),
        "enabled": enabled,
        "triggers": {name: dict(meta) for name, meta in TRIGGERS.items()},
        "execution": EXECUTION_STATUS,
        "external_commands": _EXTERNAL_COMMANDS,
        "events": recent_events(20),
        "note": (
            "hooks 是进程内受信 handler：可在工具调用前后审计；默认全部关闭，"
            "HTTP 只能切换开关/查看，不能注册任意代码，也不执行外部命令。"
        ),
    }


_register_builtins()

__all__ = [
    "EXECUTION_STATUS",
    "TRIGGERS",
    "list_hooks",
    "recent_events",
    "register_hook",
    "run_hooks",
    "set_enabled",
    "unregister_hook",
]
