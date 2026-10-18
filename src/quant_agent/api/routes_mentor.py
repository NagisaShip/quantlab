"""研究导师课程 API。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import context
from ..platform.features import require
from ..research.mentor import course_payload, lesson_ids
from .deps import get_services

router = APIRouter(prefix="/api/mentor", tags=["研究导师"])
COLLECTION = "mentor_progress"


class ProgressIn(BaseModel):
    completed: list[str] = Field(default_factory=list, max_length=32)


def _record() -> dict:
    uid = context.current().user_id or ""
    return get_services().store.get(COLLECTION, uid) or {"id": uid, "completed": []}


@router.get("/course", summary="研究导师课程与当前进度")
def course() -> dict:
    require("research.mentor")
    return course_payload(list(_record().get("completed") or []))


@router.put("/progress", summary="更新研究导师课程完成进度")
def progress(payload: ProgressIn) -> dict:
    require("research.mentor")
    unknown = sorted(set(payload.completed) - lesson_ids())
    if unknown:
        raise HTTPException(400, f"未知课程：{'、'.join(unknown)}")
    uid = context.current().user_id or ""
    rec = {**_record(), "id": uid, "completed": sorted(set(payload.completed))}
    get_services().store.upsert(COLLECTION, rec)
    return course_payload(rec["completed"])


__all__ = ["router"]
