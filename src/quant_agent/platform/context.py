"""请求上下文：当前用户、工作区、角色与 trace_id。

身份只来自服务端验证过的会话；客户端传来的 workspace_id 只表示“想访问哪个工作区”，
必须经过成员关系校验后才会写进这里。业务代码只从这里取工作区，不信任请求参数。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RequestContext:
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_id: str | None = None
    user_name: str = ""
    user_email: str = ""
    is_admin: bool = False
    workspace_id: str | None = None
    role: str | None = None  # admin | member | reviewer
    ip: str = ""
    session_id: str | None = None

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "member")

    @property
    def is_ws_admin(self) -> bool:
        return self.role == "admin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "role": self.role,
        }


_ctx: ContextVar[RequestContext | None] = ContextVar("quantlab_request_context", default=None)


def current() -> RequestContext:
    ctx = _ctx.get()
    if ctx is None:
        ctx = RequestContext()
        _ctx.set(ctx)
    return ctx


def set_context(ctx: RequestContext):
    return _ctx.set(ctx)


def reset_context(token) -> None:
    _ctx.reset(token)


def current_workspace() -> str:
    ws = current().workspace_id
    if not ws:
        from .errors import AppError, ErrorCode

        raise AppError(ErrorCode.PERMISSION_DENIED, "当前请求没有绑定工作区")
    return ws


@contextmanager
def bind(
    workspace_id: str, *, user_id: str = "system", role: str = "admin", trace_id: str | None = None
) -> Iterator[RequestContext]:
    """在请求之外（worker、CLI、测试）以指定工作区身份执行代码。"""
    ctx = RequestContext(user_id=user_id, workspace_id=workspace_id, role=role, user_name=user_id)
    if trace_id:
        ctx.trace_id = trace_id
    token = _ctx.set(ctx)
    try:
        yield ctx
    finally:
        _ctx.reset(token)
