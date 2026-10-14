"""进程内后台任务：参数优化、团队调研等耗时操作不阻塞请求，前端轮询进度。"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

MAX_JOBS = 50


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, kind: str, fn: Callable[[Callable[[int, int], None]], Any], *, title: str = "") -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "kind": kind,
            "title": title,
            "status": "running",
            "done": 0,
            "total": 0,
            "result": None,
            "error": "",
            "started_at": int(time.time() * 1000),
            "ended_at": 0,
        }
        with self._lock:
            self._jobs[job_id] = job
            if len(self._jobs) > MAX_JOBS:
                for old in sorted(self._jobs.values(), key=lambda j: j["started_at"])[: len(self._jobs) - MAX_JOBS]:
                    if old["status"] != "running":
                        self._jobs.pop(old["id"], None)

        def progress(done: int, total: int) -> None:
            job["done"], job["total"] = done, total

        def run() -> None:
            try:
                job["result"] = fn(progress)
                job["status"] = "done"
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = f"{type(exc).__name__}: {exc}"[:500]
            job["ended_at"] = int(time.time() * 1000)

        # 带上提交时的请求上下文（工作区、用户），后台线程里的存储访问才能落到正确的工作区
        ctx = contextvars.copy_context()
        threading.Thread(target=lambda: ctx.run(run), daemon=True, name=f"job-{kind}-{job_id}").start()
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(job_id)
        return dict(job) if job else None

    def wait(self, job_id: str, timeout: float = 60.0) -> dict[str, Any] | None:
        """测试与 CLI 用：阻塞等待完成。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self._jobs.get(job_id)
            if job and job["status"] != "running":
                return dict(job)
            time.sleep(0.05)
        return self.get(job_id)


jobs = JobRegistry()
