"""首次注册新手引导：跨设备保存步骤进度。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..platform import context, onboarding
from ..platform.errors import AppError, ErrorCode
from .deps import get_services

router = APIRouter(prefix="/api/onboarding", tags=["新手引导"])


class OnboardingIn(BaseModel):
    current_step: str | None = Field(None, max_length=32)
    completed_step: str | None = Field(None, max_length=32)
    status: str | None = Field(None, max_length=32)
    track: str | None = Field(None, max_length=16)


@router.get("", summary="读取当前账号的新手引导进度")
def get_onboarding() -> dict:
    ctx = context.current()
    s = get_services()
    return {"onboarding": onboarding.get(s.auth.db, ctx.user_id, is_admin=ctx.is_admin)}


@router.put("", summary="更新新手引导进度")
def put_onboarding(payload: OnboardingIn) -> dict:
    ctx = context.current()
    s = get_services()
    try:
        state = onboarding.update(
            s.auth.db,
            ctx.user_id,
            current_step=payload.current_step,
            completed_step=payload.completed_step,
            status=payload.status,
            track=payload.track,
            is_admin=ctx.is_admin,
        )
    except ValueError as exc:
        raise AppError(ErrorCode.INVALID_PARAMETER, str(exc)) from exc
    s.audit.record(
        "onboarding.step",
        object_type="user",
        object_id=ctx.user_id,
        details={
            "step": state["current_step"],
            "completed_step": payload.completed_step,
            "status": state["status"],
            "track": state["track"],
        },
    )
    return {"onboarding": state}


@router.post("/reset", summary="重新开始新手引导")
def reset_onboarding() -> dict:
    ctx = context.current()
    s = get_services()
    state = onboarding.reset(s.auth.db, ctx.user_id, is_admin=ctx.is_admin)
    s.audit.record(
        "onboarding.reset",
        object_type="user",
        object_id=ctx.user_id,
        details={"step": state["current_step"], "track": state["track"]},
    )
    return {"onboarding": state}
