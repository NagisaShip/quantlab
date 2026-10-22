"""Exclusive local instance and explicit recovery of interrupted UI operations."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager

from ..workflows.environments import write_json


@contextmanager
def instance_lock(home):
    home.mkdir(parents=True, exist_ok=True)
    stream = (home / "instance.lock").open("a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError("此项目已有运行实例，请重新打开项目以连接原服务") from None
    try:
        yield
    finally:
        stream.close()  # OS releases the lock even after abnormal termination.


def recover_ui_operations(services):
    from ..agents.lab_state import COLLECTION

    profile = services.local
    store = services.store.for_workspace(profile.manifest["workspace_id"])
    for record in store.list(COLLECTION):
        if record.get("status") == "running":
            record.update(status="stopped", error="上次服务中断，对话与已保存工作流已保留；请检查证据后继续。")
            record["job"] = None
            store.upsert(COLLECTION, record)
    for path in (store.root / "workflows/management-jobs").glob("*.json"):
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("state") in ("running", "queued"):
            write_json(
                path,
                {
                    **state,
                    "state": "failed",
                    "error": "上次服务中断；旧环境和研究证据仍保留，可重新发起操作",
                    "finished_at": time.time(),
                },
            )
    # Queue recovery remains lease-fenced; don't invent successful completion.
    services.runs.recover_expired()
