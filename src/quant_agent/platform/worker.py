"""独立 worker：认领运行、持有租约、写检查点、应用命令。

部署时 API（控制面）与 worker 是两个进程（docker-compose 里是两个服务）。API 只登记 run 与命令，
不执行回测、优化或实时策略。

任务分两类：
- **批处理**（backtest / optimize / arena …）：在线程池里执行，期间每隔 LEASE/3 心跳续租；
  进度与中间结果写进检查点，崩溃后由其他 worker 从检查点继续（优化已算完的组合不重算）。
- **常驻**（paper）：每个 run 一个“驱动器”，主循环按节拍推进；每处理完一根 bar 就写检查点。
  worker 崩溃 → 租约过期 → RECOVERING → 另一个 worker 认领并从检查点恢复，已处理的事件和
  信号不会再成交一次；停机期间的 bar 只推进数据、不补成交。

测试与单机调试可以用 inline 模式（`QAGENT_WORKER=inline`）：API 请求线程里直接调用同一套
认领 / 执行 / 检查点代码，行为与独立 worker 一致，只是不跨进程。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from . import context
from .db import jdump, now_ms
from .errors import AppError, ErrorCode
from .runs import CANCELED, COMPLETED, FAILED, LEASE_MS, PAUSED, RUNNING, RunService, worker_id

log = logging.getLogger("quantlab.worker")


class LeaseLost(RuntimeError):
    """租约已被其他 worker 接手：立即停止，不再写任何东西。"""


class Canceled(RuntimeError):
    pass


@dataclass
class TaskCtl:
    """交给任务处理函数的控制句柄。"""

    run: dict[str, Any]
    runs: RunService
    owner: str
    services: Any
    _last_beat: float = field(default_factory=time.monotonic)

    @property
    def run_id(self) -> str:
        return self.run["id"]

    @property
    def manifest(self) -> dict[str, Any]:
        return self.run.get("manifest") or {}

    @property
    def checkpoint_data(self) -> dict[str, Any] | None:
        return self.run.get("checkpoint")

    def beat(self, *, force: bool = False) -> None:
        if force or time.monotonic() - self._last_beat > LEASE_MS / 3000:
            if not self.runs.heartbeat(self.run_id, self.owner):
                raise LeaseLost(self.run_id)
            self._last_beat = time.monotonic()
            for cmd in self.runs.pending_commands(self.run_id):
                if cmd["command"] == "cancel":
                    self.runs.ack_command(cmd["id"], message="已取消")
                    raise Canceled("用户取消")

    def progress(self, done: int, total: int) -> None:
        if not self.runs.progress(self.run_id, self.owner, done, total):
            raise LeaseLost(self.run_id)
        self.beat()

    def checkpoint(self, data: dict[str, Any], **kw: Any) -> None:
        if not self.runs.checkpoint(self.run_id, self.owner, data, **kw):
            raise LeaseLost(self.run_id)
        self.run["checkpoint"] = data

    def event(self, type_: str, data: dict[str, Any] | None = None) -> None:
        if self.runs.event(self.run_id, type_, data, owner=self.owner) is None:
            raise LeaseLost(self.run_id)


BatchHandler = Callable[[TaskCtl], dict[str, Any]]


class ResidentDriver:
    """常驻任务驱动器接口（实时模拟实现它）。"""

    tick_seconds: float = 30.0

    def start(self, ctl: TaskCtl) -> None: ...
    def tick(self, ctl: TaskCtl) -> None: ...
    def command(self, ctl: TaskCtl, command: str) -> str | None:
        """应用命令，返回新的 run 状态（None 表示不变）。"""
        return None

    def close(self) -> None: ...


class Worker:
    def __init__(
        self,
        services: Any,
        *,
        batch: dict[str, BatchHandler] | None = None,
        resident: dict[str, Callable[[], ResidentDriver]] | None = None,
        owner: str | None = None,
        slots: int = 2,
        poll_seconds: float = 3.0,
        periodic: list[tuple[float, Callable[[Any], None]]] | None = None,
    ) -> None:
        self.services = services
        self.runs: RunService = services.runs
        self.batch = batch or {}
        self.resident = resident or {}
        self.owner = owner or worker_id()
        self.slots = slots
        self.poll_seconds = poll_seconds
        self.pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="run")
        self.busy: dict[str, Any] = {}
        self.drivers: dict[str, tuple[ResidentDriver, TaskCtl, float]] = {}
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.started_at = now_ms()
        self.periodic = [[interval, fn, 0.0] for interval, fn in (periodic or [])]
        self.max_resident = int(os.environ.get("QAGENT_WORKER_MAX_RESIDENT", "50"))

    # ------------------------------------------------------------------ 主循环
    def run_forever(self) -> None:
        log.info("worker %s 启动：批处理 %s，常驻 %s", self.owner, list(self.batch), list(self.resident))
        wake = getattr(self.runs, "wake", None)
        from .watchdog import HangGuard

        guard = HangGuard()  # 主循环卡死 → 退出进程，docker 重启后从检查点恢复
        guard.start()
        while not self.stop_event.is_set():
            guard.tick()
            try:
                self.register()
                self.loop_once()
            except Exception:
                log.exception("worker 主循环异常")
            if wake is not None:
                wake.wait(self.poll_seconds)
            else:
                self.stop_event.wait(self.poll_seconds)
        guard.stop.set()
        self.shutdown()

    def register(self) -> None:
        info = {
            "busy": list(self.busy),
            "resident": list(self.drivers),
            "kinds": list(self.batch) + list(self.resident),
        }
        db = self.runs.db
        now = now_ms()
        if not db.execute("UPDATE workers SET last_seen=?, info=? WHERE id=?", (now, jdump(info), self.owner)):
            db.execute(
                "INSERT INTO workers (id, host, pid, kinds, started_at, last_seen, info) VALUES (?,?,?,?,?,?,?)",
                (
                    self.owner,
                    socket.gethostname(),
                    os.getpid(),
                    jdump(info["kinds"]),
                    self.started_at,
                    now,
                    jdump(info),
                ),
            )
        wake = getattr(self.runs, "wake", None)
        if wake is not None:
            wake.worker_alive(self.owner, {"host": socket.gethostname(), "pid": os.getpid(), **info})

    def run_periodic(self) -> None:
        for item in self.periodic:
            interval, fn, last = item
            if time.monotonic() - last >= interval:
                item[2] = time.monotonic()
                try:
                    fn(self.services)
                except Exception:
                    log.exception("周期任务失败：%s", getattr(fn, "__name__", fn))

    def loop_once(self) -> None:
        self.run_periodic()
        # 1) 认领新任务
        while True:
            allow = list(self.resident) if len(self.drivers) < self.max_resident else []
            if len(self.busy) < self.slots:
                allow += list(self.batch)
            if not allow:
                break
            run = self.runs.claim(self.owner, kinds=allow)
            if run is None:
                break
            self._dispatch(run, background=True)
        # 2) 推进常驻任务
        for run_id in list(self.drivers):
            self.drive(run_id)

    def _dispatch(self, run: dict[str, Any], *, background: bool) -> None:
        kind = run["kind"]
        ctl = TaskCtl(run=run, runs=self.runs, owner=self.owner, services=self.services)
        if kind in self.resident:
            self._start_resident(ctl)
            return
        if kind not in self.batch:
            self.runs.set_state(
                run["id"],
                self.owner,
                FAILED,
                error=f"worker 不支持任务类型 {kind}",
                error_code=ErrorCode.UNSUPPORTED_CAPABILITY,
            )
            return
        if background:
            with self._lock:
                self.busy[run["id"]] = self.pool.submit(self._execute_batch, ctl)
        else:
            self._execute_batch(ctl)

    # ------------------------------------------------------------------ 批处理
    def _execute_batch(self, ctl: TaskCtl) -> None:
        run = ctl.run
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(LEASE_MS / 3000):
                try:
                    if not self.runs.heartbeat(run["id"], self.owner):
                        return
                except Exception:
                    pass

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        started = time.monotonic()
        log.info(
            "run %s 开始：%s（工作区 %s，第 %s 次尝试）",
            run["id"],
            run["kind"],
            run["workspace_id"],
            run.get("attempts", 1),
        )
        try:
            with context.bind(run["workspace_id"], user_id=run["created_by"], role="member"):
                summary = self.batch[run["kind"]](ctl)
            self.runs.set_state(run["id"], self.owner, COMPLETED, summary=summary or {})
            log.info("run %s 完成，用时 %.1fs", run["id"], time.monotonic() - started)
        except LeaseLost:
            log.warning("run %s 租约丢失，停止执行", run["id"])
        except Canceled:
            self.runs.set_state(run["id"], self.owner, CANCELED)
        except AppError as exc:
            self.runs.set_state(run["id"], self.owner, FAILED, error=exc.message, error_code=exc.code)
            self._alert_failed(run, error=exc.message, error_code=exc.code)
        except Exception as exc:
            code = _error_code(exc)
            log.warning("run %s 失败：%s\n%s", run["id"], exc, traceback.format_exc(limit=5))
            msg = f"{exc}" if code != "INTERNAL_ERROR" else f"{type(exc).__name__}: {exc}"
            self.runs.set_state(
                run["id"],
                self.owner,
                FAILED,
                error=msg,
                error_code=code,
            )
            self._alert_failed(run, error=msg, error_code=code)
        finally:
            stop.set()
            with self._lock:
                self.busy.pop(run["id"], None)

    # ------------------------------------------------------------------ 常驻
    def _alert_failed(self, run: dict, *, error: str, error_code: str) -> None:
        """run 变 FAILED 时发告警（T09）。

        单独抽一个方法而不是在三处各写一遍：告警是**旁路**，绝不能因为通知出问题
        而影响状态机，所以统一由 `raise_alert_safe` 吞异常，并且放在 `set_state` 之后
        ——状态已经落库了才通知，通知失败也不影响"run 确实失败了"这个事实。
        """
        try:
            from ..notify import alerts as _alerts

            _alerts.raise_for_run_failure(
                self.runs.db,
                workspace_id=run["workspace_id"],
                run_id=run["id"],
                run_kind=run["kind"],
                mode=run["mode"],
                error=error,
                error_code=error_code,
            )
        except Exception:
            log.exception("run 失败告警上报异常：%s", run["id"])

    def _start_resident(self, ctl: TaskCtl) -> None:
        run = ctl.run
        driver = self.resident[run["kind"]]()
        try:
            with context.bind(run["workspace_id"], user_id=run["created_by"], role="member"):
                driver.start(ctl)
        except LeaseLost:
            driver.close()
            return
        except Exception as exc:
            driver.close()
            code = exc.code if isinstance(exc, AppError) else _error_code(exc)
            msg = exc.message if isinstance(exc, AppError) else str(exc)
            self.runs.set_state(run["id"], self.owner, FAILED, error=msg, error_code=code)
            self._alert_failed(run, error=msg, error_code=code)
            return
        self.drivers[run["id"]] = (driver, ctl, 0.0)

    def drive(self, run_id: str, *, force_tick: bool = False) -> None:
        """处理该 run 的待办命令；到节拍（或 force）就推进一次。"""
        item = self.drivers.get(run_id)
        if item is None:
            return
        driver, ctl, last = item
        try:
            with context.bind(ctl.run["workspace_id"], user_id=ctl.run["created_by"], role="member"):
                state = self.runs.get(run_id)
                if state is None:
                    raise LeaseLost(run_id)
                for cmd in self.runs.pending_commands(run_id):
                    if cmd["command"] == "tick":
                        force_tick = True
                        self.runs.ack_command(cmd["id"], message="已安排立即采集")
                        continue
                    new_state = driver.command(ctl, cmd["command"])
                    self.runs.ack_command(cmd["id"], message=f"已应用：{cmd['command']}")
                    if new_state:
                        self.runs.set_state(run_id, self.owner, new_state)
                        if new_state in (COMPLETED, CANCELED, FAILED):
                            self._drop(run_id)
                            return
                state = self.runs.get(run_id)
                if state is None or state.get("lease_owner") != self.owner:
                    raise LeaseLost(run_id)
                due = time.monotonic() - last >= driver.tick_seconds
                if state["state"] in (RUNNING, PAUSED) and (force_tick or due):
                    finished = driver.tick(ctl)
                    self.drivers[run_id] = (driver, ctl, time.monotonic())
                    if finished:
                        self.runs.set_state(run_id, self.owner, COMPLETED, summary=finished)
                        self._drop(run_id)
                        return
                ctl.beat(force=True)
        except LeaseLost:
            log.warning("常驻 run %s 租约丢失，释放驱动器", run_id)
            self._drop(run_id)
        except Canceled:
            self.runs.set_state(run_id, self.owner, CANCELED)
            self._drop(run_id)
        except Exception as exc:
            log.exception("常驻 run %s 推进失败", run_id)
            ctl.run.setdefault("_errors", 0)
            ctl.run["_errors"] += 1
            try:
                ctl.event("error", {"message": f"{type(exc).__name__}: {exc}"[:500]})
            except Exception:
                pass
            if ctl.run["_errors"] >= 5:
                msg = f"连续失败：{exc}"
                self.runs.set_state(run_id, self.owner, FAILED, error=msg, error_code=_error_code(exc))
                self._alert_failed(ctl.run, error=msg, error_code=_error_code(exc))
                self._drop(run_id)

    def _drop(self, run_id: str) -> None:
        item = self.drivers.pop(run_id, None)
        if item:
            try:
                item[0].close()
            except Exception:
                pass

    # ------------------------------------------------------------------ inline（测试 / 单机）
    def execute_now(self, run_id: str) -> dict[str, Any] | None:
        """在当前线程认领并执行指定 run（批处理执行完；常驻任务完成启动）。"""
        run = self._claim_specific(run_id)
        if run is None:
            return self.runs.get(run_id)
        self._dispatch(run, background=False)
        return self.runs.get(run_id)

    def _claim_specific(self, run_id: str) -> dict[str, Any] | None:
        from .runs import QUEUED, RECOVERING

        self.runs.recover_expired()
        now = now_ms()
        changed = self.runs.db.execute(
            "UPDATE runs SET state=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1, started_at=COALESCE(star"
            "ted_at, ?), "
            "updated_at=? WHERE id=? AND state IN (?,?)",
            (RUNNING, self.owner, now + LEASE_MS, now, now, run_id, QUEUED, RECOVERING),
        )
        if not changed:
            return None
        self.runs.event(run_id, "state", {"state": RUNNING, "owner": self.owner})
        return self.runs.get(run_id, checkpoint=True)

    def simulate_crash(self, run_id: str) -> None:
        """测试 / 演示：丢掉内存里的驱动器并让租约立即过期，模拟 worker 崩溃。"""
        item = self.drivers.pop(run_id, None)
        if item:
            try:
                item[0].close()
            except Exception:
                pass
        self.runs.db.execute("UPDATE runs SET lease_expires_at=? WHERE id=?", (now_ms() - 1, run_id))
        self.runs.recover_expired()

    def shutdown(self) -> None:
        for run_id in list(self.drivers):
            self.runs.release(run_id, self.owner)
            self._drop(run_id)
        self.pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.runs.db.execute("DELETE FROM workers WHERE id=?", (self.owner,))
        except Exception:
            pass


def _error_code(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "DataUnavailable":
        return ErrorCode.UPSTREAM_UNAVAILABLE
    if name in ("StrategyLoadError", "ValueError"):
        return ErrorCode.INVALID_PARAMETER
    if name == "SandboxError":
        kind = getattr(exc, "kind", "")
        if kind in ("StrategyLoadError", "ValueError", "StrategyTimeout"):
            return ErrorCode.INVALID_PARAMETER
        if kind == "DataUnavailable":
            return ErrorCode.UPSTREAM_UNAVAILABLE
        return ErrorCode.TASK_FAILED
    return ErrorCode.TASK_FAILED
