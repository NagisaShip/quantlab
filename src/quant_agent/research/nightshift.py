"""夜班：无人值守地按队列跑研究任务，有试验次数、token 和时长上限，第二天早上给一份简报。

一个"班次"按顺序处理队列里的任务。每个任务 = 在 AI 研究室里新建一个项目并自动迭代
（因子 / 模型 / 组合 / 执行 / 策略都可以）。预算在班次级别统计：
- 试验次数：所有任务新增的版本数之和；
- token：班次开始以来工作区的 LLM token 用量增量；
- 时长：墙钟分钟数。
任一预算用完，就让正在跑的项目停下来（项目自己的停止机制，当前版本保留），后面的任务不再开始。

红线：夜班只产出候选成果，结束时停在"是否启用 / 入库"的提问上，等人第二天决定。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

CONFIG = "rd_nightshift"  # id=config：预算与时间
TASKS = "rd_nightshift_tasks"  # 队列
SHIFTS = "rd_nightshift_shifts"  # 班次记录

KINDS = {"strategy": "策略", "factor": "因子", "model": "模型", "portfolio": "组合", "execution": "执行"}
DEFAULT_CONFIG = {
    "id": "config",
    "enabled": False,
    "hour": 1,
    "minute": 0,
    "max_trials": 40,
    "max_tokens": 400_000,
    "max_minutes": 240,
    "rounds": 4,
    "brief": True,
    "channel": "console",
}


def _now() -> float:
    return time.time()


def config(store: Any) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **(store.get(CONFIG, "config") or {})}


def save_config(store: Any, patch: dict[str, Any]) -> dict[str, Any]:
    cur = config(store)
    for k in ("enabled", "brief"):
        if k in patch:
            cur[k] = bool(patch[k])
    for k, lo, hi in (
        ("hour", 0, 23),
        ("minute", 0, 59),
        ("max_trials", 1, 500),
        ("max_tokens", 1000, 50_000_000),
        ("max_minutes", 5, 24 * 60),
        ("rounds", 1, 8),
    ):
        if k in patch and patch[k] is not None:
            cur[k] = max(lo, min(hi, int(patch[k])))
    if "channel" in patch:
        cur["channel"] = str(patch["channel"] or "console")[:40]
    store.upsert(CONFIG, cur)
    return cur


def tasks(store: Any, *, status: str | None = None) -> list[dict[str, Any]]:
    rows = store.list(TASKS)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return sorted(rows, key=lambda r: (r.get("status") != "queued", r.get("order") or 0, r.get("created_at") or 0))


def add_task(
    store: Any,
    *,
    kind: str,
    idea: str,
    executor: str = "solo",
    symbols: list[str] | None = None,
    timeframe: str | None = None,
    rounds: int | None = None,
    user: str = "",
    data_source: str = "auto",
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"未知的研究类型：{kind}")
    if not idea.strip():
        raise ValueError("写一句研究想法")
    rec = {
        "id": f"nt_{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "idea": idea.strip()[:600],
        "executor": "team" if executor == "team" else "solo",
        "symbols": symbols or [],
        "timeframe": timeframe,
        "rounds": rounds,
        "data_source": data_source or "auto",
        "status": "queued",
        "order": int(_now() * 1000),
        "created_at": _now(),
        "created_by": user,
        "project_id": None,
        "result": None,
    }
    store.upsert(TASKS, rec)
    return rec


def remove_task(store: Any, tid: str) -> bool:
    rec = store.get(TASKS, tid)
    if rec is None or rec.get("status") == "running":
        return False
    return store.delete(TASKS, tid)


def shifts(store: Any, limit: int = 30) -> list[dict[str, Any]]:
    return sorted(store.list(SHIFTS), key=lambda r: -(r.get("started_at") or 0))[:limit]


def shift(store: Any, sid: str) -> dict[str, Any] | None:
    return store.get(SHIFTS, sid)


def running_shift(store: Any) -> dict[str, Any] | None:
    for r in store.list(SHIFTS):
        if r.get("status") == "running":
            # 进程重启后遗留的"运行中"记录：心跳停了 15 分钟就视为中断
            if _now() - (r.get("heartbeat") or r.get("started_at") or 0) > 900:
                store.upsert(
                    SHIFTS, {**r, "status": "interrupted", "ended_at": _now(), "stop_reason": "服务重启，班次中断"}
                )
                continue
            return r
    return None


def request_stop(store: Any) -> bool:
    cur = running_shift(store)
    if not cur:
        return False
    store.upsert(SHIFTS, {**cur, "stop_requested": True})
    return True


class Budget:
    def __init__(self, cfg: dict[str, Any], tokens_fn: Callable[[], float]) -> None:
        self.cfg = cfg
        self.tokens_fn = tokens_fn
        self.t0 = time.monotonic()
        self.tok0 = self._tokens()
        self.trials = 0

    def _tokens(self) -> float:
        try:
            return float(self.tokens_fn())
        except Exception:
            return 0.0

    def used(self, live_trials: int = 0) -> dict[str, Any]:
        return {
            "trials": self.trials + live_trials,
            "tokens": max(0.0, self._tokens() - self.tok0),
            "minutes": (time.monotonic() - self.t0) / 60,
        }

    def exceeded(self, live_trials: int = 0) -> str | None:
        u = self.used(live_trials)
        if u["trials"] >= self.cfg["max_trials"]:
            return f"试验次数用完（{u['trials']}/{self.cfg['max_trials']}）"
        if u["tokens"] >= self.cfg["max_tokens"]:
            return f"token 预算用完（{int(u['tokens'])}/{self.cfg['max_tokens']}）"
        if u["minutes"] >= self.cfg["max_minutes"]:
            return f"时长用完（{u['minutes']:.0f}/{self.cfg['max_minutes']} 分钟）"
        return None


def run_shift(
    store: Any,
    *,
    start_task: Callable[[dict[str, Any], int], str],
    run_task: Callable[[str], None],
    versions_of: Callable[[str], int],
    stop_project: Callable[[str], None],
    summarize: Callable[[str], dict[str, Any]],
    tokens_fn: Callable[[], float],
    trigger: str = "manual",
    on_done: Callable[[dict[str, Any]], None] | None = None,
    poll: float = 5.0,
) -> dict[str, Any]:
    """同步执行一个班次（调用方决定放在线程还是 worker 里）。

    start_task(task, rounds) → 新建项目并返回项目 id；run_task(pid) 阻塞直到该项目的自动迭代结束；
    versions_of(pid) → 项目当前版本数；stop_project(pid) 让项目尽快停下；summarize(pid) → 结果摘要。
    """
    if running_shift(store):
        raise RuntimeError("已经有一个班次在跑")
    cfg = config(store)
    queue = tasks(store, status="queued")
    sid = f"ns_{uuid.uuid4().hex[:8]}"
    rec: dict[str, Any] = {
        "id": sid,
        "status": "running",
        "trigger": trigger,
        "started_at": _now(),
        "heartbeat": _now(),
        "budget": {k: cfg[k] for k in ("max_trials", "max_tokens", "max_minutes", "rounds")},
        "items": [],
        "used": {},
        "stop_requested": False,
        "stop_reason": None,
    }
    store.upsert(SHIFTS, rec)
    budget = Budget(cfg, tokens_fn)
    reason: str | None = None if queue else "队列是空的"
    for task in queue:
        cur = store.get(SHIFTS, sid) or rec
        if cur.get("stop_requested"):
            reason = "手动停止"
            break
        reason = budget.exceeded()
        if reason:
            break
        task = {**task, "status": "running", "started_at": _now()}
        store.upsert(TASKS, task)
        item: dict[str, Any] = {"task_id": task["id"], "kind": task["kind"], "idea": task["idea"], "status": "running"}
        try:
            pid = start_task(task, int(task.get("rounds") or cfg["rounds"]))
        except Exception as exc:  # 建项目失败：记下来，继续下一个
            item.update(status="failed", error=str(exc)[:300])
            store.upsert(TASKS, {**task, "status": "failed", "result": item})
            rec = {**(store.get(SHIFTS, sid) or rec), "items": [*(store.get(SHIFTS, sid) or rec)["items"], item]}
            store.upsert(SHIFTS, rec)
            continue
        item["project_id"] = pid
        v0 = versions_of(pid)
        stop_hit: dict[str, Any] = {}
        done = threading.Event()

        def watchdog(done=done, pid=pid, v0=v0, stop_hit=stop_hit) -> None:  # 绑定本轮的值
            while not done.wait(poll):
                live = max(0, versions_of(pid) - v0)
                cur_shift = store.get(SHIFTS, sid) or {}
                why = "手动停止" if cur_shift.get("stop_requested") else budget.exceeded(live)
                store.upsert(SHIFTS, {**cur_shift, "heartbeat": _now(), "used": budget.used(live)})
                if why and not stop_hit.get("why"):
                    stop_hit["why"] = why
                    stop_project(pid)

        import contextvars

        ctx = contextvars.copy_context()
        th = threading.Thread(target=ctx.run, args=(watchdog,), daemon=True, name=f"nightshift-{sid}")
        th.start()
        try:
            run_task(pid)
            item["status"] = "stopped" if stop_hit.get("why") else "done"
        except Exception as exc:
            item.update(status="failed", error=str(exc)[:300])
        finally:
            done.set()
            th.join(timeout=poll + 30)
        budget.trials += max(0, versions_of(pid) - v0)
        try:
            item.update(summarize(pid))
        except Exception as exc:
            item["summary_error"] = str(exc)[:200]
        item["versions"] = max(0, versions_of(pid) - v0)
        store.upsert(
            TASKS,
            {
                **task,
                "status": item["status"],
                "project_id": pid,
                "ended_at": _now(),
                "result": {k: item.get(k) for k in ("status", "headline", "passed", "admitted")},
            },
        )
        cur = store.get(SHIFTS, sid) or rec
        store.upsert(
            SHIFTS, {**cur, "items": [*cur.get("items", []), item], "used": budget.used(), "heartbeat": _now()}
        )
        if stop_hit.get("why"):
            reason = stop_hit["why"]
            break
    cur = store.get(SHIFTS, sid) or rec
    left = len(tasks(store, status="queued"))
    final = {
        **cur,
        "status": "done",
        "ended_at": _now(),
        "used": budget.used(),
        "stop_reason": reason or ("全部任务完成" if not left else None),
        "left": left,
    }
    store.upsert(SHIFTS, final)
    if on_done:
        try:
            on_done(final)
        except Exception:
            pass
    return final


def due(cfg: dict[str, Any], now: time.struct_time, last_started: float | None) -> bool:
    """到点了吗：每天一次，在设定的时刻之后的第一次检查触发。"""
    if not cfg.get("enabled"):
        return False
    if (now.tm_hour, now.tm_min) < (cfg["hour"], cfg["minute"]):
        return False
    if last_started:
        last = time.localtime(last_started)
        if (last.tm_year, last.tm_yday) == (now.tm_year, now.tm_yday):
            return False
    return True


__all__ = [
    "DEFAULT_CONFIG",
    "KINDS",
    "Budget",
    "add_task",
    "config",
    "due",
    "remove_task",
    "request_stop",
    "run_shift",
    "running_shift",
    "save_config",
    "shift",
    "shifts",
    "tasks",
]
