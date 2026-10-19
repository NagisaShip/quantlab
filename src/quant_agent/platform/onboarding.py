"""首次注册后的步骤式新手引导状态。

状态按用户保存，而不是按工作区保存：用户在多个工作区之间切换时，不应重复
完整引导。已有的老用户由迁移标记为 completed；新注册用户在注册时写入
not_started，只有第一次进入工作台才展示欢迎层。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .db import Database

ONBOARDING_VERSION = 1
STEPS = ("welcome", "flow", "goal", "agents", "backtest", "finish")
STATUSES = ("not_started", "in_progress", "completed", "skipped")
TRACKS = ("owner", "member")


def _now() -> int:
    return int(time.time() * 1000)


def _decode(row: dict[str, Any] | None, *, is_admin: bool = False) -> dict[str, Any]:
    row = row or {}
    try:
        completed = json.loads(row.get("completed_steps") or "[]")
    except (TypeError, ValueError):
        completed = []
    if not isinstance(completed, list):
        completed = []
    completed = [str(x) for x in completed if str(x) in STEPS]
    track = row.get("track")
    if track not in TRACKS:
        track = "owner" if is_admin else "member"
    step = row.get("current_step")
    if step not in STEPS:
        step = "welcome"
    status = row.get("status")
    if status not in STATUSES:
        status = "not_started"
    return {
        "version": int(row.get("version") or ONBOARDING_VERSION),
        "status": status,
        "current_step": step,
        "track": track,
        "completed_steps": completed,
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "updated_at": row.get("updated_at"),
    }


def ensure(db: Database, user_id: str, *, is_admin: bool = False) -> dict[str, Any]:
    """读取状态；没有记录时按新用户初始化。"""
    row = db.fetchone("SELECT * FROM user_onboarding WHERE user_id=?", (user_id,))
    if row is None:
        now = _now()
        track = "owner" if is_admin else "member"
        db.execute(
            "INSERT INTO user_onboarding "
            "(user_id, version, status, current_step, track, completed_steps, started_at, completed_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, ONBOARDING_VERSION, "not_started", "welcome", track, "[]", None, None, now),
        )
        row = db.fetchone("SELECT * FROM user_onboarding WHERE user_id=?", (user_id,))
    elif not row.get("track"):
        db.execute(
            "UPDATE user_onboarding SET track=?, updated_at=? WHERE user_id=?",
            ("owner" if is_admin else "member", _now(), user_id),
        )
        row = db.fetchone("SELECT * FROM user_onboarding WHERE user_id=?", (user_id,))
    return _decode(row, is_admin=is_admin)


def get(db: Database, user_id: str, *, is_admin: bool = False) -> dict[str, Any]:
    return ensure(db, user_id, is_admin=is_admin)


def update(
    db: Database,
    user_id: str,
    *,
    current_step: str | None = None,
    completed_step: str | None = None,
    status: str | None = None,
    track: str | None = None,
    is_admin: bool = False,
) -> dict[str, Any]:
    state = ensure(db, user_id, is_admin=is_admin)
    completed = list(state["completed_steps"])
    if completed_step in STEPS and completed_step not in completed:
        completed.append(completed_step)
    if current_step is not None:
        if current_step not in STEPS:
            raise ValueError(f"unknown onboarding step: {current_step}")
        state["current_step"] = current_step
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"unknown onboarding status: {status}")
        state["status"] = status
    if track is not None:
        if track not in TRACKS:
            raise ValueError(f"unknown onboarding track: {track}")
        state["track"] = track
    now = _now()
    started_at = state["started_at"]
    if state["status"] == "in_progress" and not started_at:
        started_at = now
    completed_at = state["completed_at"]
    if state["status"] == "completed":
        completed_at = completed_at or now
    db.execute(
        "UPDATE user_onboarding SET version=?, status=?, current_step=?, track=?, "
        "completed_steps=?, started_at=?, completed_at=?, updated_at=? WHERE user_id=?",
        (
            ONBOARDING_VERSION,
            state["status"],
            state["current_step"],
            state["track"],
            json.dumps(completed, ensure_ascii=False),
            started_at,
            completed_at,
            now,
            user_id,
        ),
    )
    return get(db, user_id, is_admin=is_admin)


def reset(db: Database, user_id: str, *, is_admin: bool = False) -> dict[str, Any]:
    ensure(db, user_id, is_admin=is_admin)
    now = _now()
    db.execute(
        "UPDATE user_onboarding SET status='in_progress', current_step='welcome', "
        "completed_steps='[]', started_at=?, completed_at=NULL, updated_at=? WHERE user_id=?",
        (now, now, user_id),
    )
    return get(db, user_id, is_admin=is_admin)
