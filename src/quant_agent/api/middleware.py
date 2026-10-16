"""平台中间件：trace_id、会话认证、工作区成员校验、CSRF、角色权限、幂等键。

处理顺序（每个 /api 请求）：
1. 生成或继承 trace_id，写入请求上下文与响应头 X-Trace-Id；
2. 公共端点（登录、注册、健康检查）直接放行；
3. 从 Cookie `qa_session` 或 `Authorization: Bearer` 解析会话 → 用户；无效则 401；
4. 读取客户端声明的目标工作区（请求头 X-Workspace-Id，下载链接可用 ?ws=），
   **在服务端校验成员关系**；客户端给的 id 只表示目标，不是授权证据；
5. 写操作：Cookie 认证必须带 X-Requested-With 头（阻断跨站表单提交）；审阅者只读；
6. 幂等：带 Idempotency-Key 头（或 JSON 体里的 idempotency_key）的写请求，
   同键同内容直接回放首次结果，同键不同内容返回 IDEMPOTENCY_CONFLICT。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

import anyio

from ..platform import context
from ..platform.context import RequestContext
from ..platform.db import IntegrityError, now_ms
from ..platform.errors import ErrorCode, error_body

log = logging.getLogger("quantlab.api")

SESSION_COOKIE = "qa_session"
PUBLIC_PATHS = {
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/status",
    "/api/auth/logout",
    "/api/health",
    "/api/healthz",
}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# 审阅者也允许的写操作（只涉及自己的账号）
REVIEWER_WRITABLE = ("/api/auth/", "/api/workspaces/join", "/api/inbox", "/api/issues", "/api/uploads")
IDEMPOTENCY_TTL_MS = 24 * 3600 * 1000
MAX_BODY = 25 * 1024 * 1024


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _cookie(headers: dict[str, str], name: str) -> str:
    raw = headers.get("cookie", "")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def _query_param(scope, name: str) -> str:
    from urllib.parse import parse_qs

    qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    return (qs.get(name) or [""])[0]


async def _send_json(send, status: int, body: dict[str, Any], trace_id: str) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode()),
                (b"x-trace-id", trace_id.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class PlatformMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = _headers(scope)
        trace_id = (headers.get("x-request-id") or uuid.uuid4().hex[:16])[:64]
        client = scope.get("client") or ("", 0)
        ctx = RequestContext(
            trace_id=trace_id, ip=headers.get("x-forwarded-for", client[0] or "").split(",")[0].strip()
        )
        token = context.set_context(ctx)
        try:
            path = scope.get("path", "")
            if (
                scope["type"] == "websocket"
                or not path.startswith("/api/")
                or path in PUBLIC_PATHS
                or path.startswith("/api/bot/hook/")
            ):  # 聊天软件的回调：靠各家签名校验，不走登录
                if scope["type"] == "http":
                    return await self.app(scope, receive, _with_trace(send, trace_id))
                return await self.app(scope, receive, send)
            return await self._handle_api(scope, receive, send, headers, ctx)
        finally:
            context.reset_context(token)

    async def _handle_api(self, scope, receive, send, headers, ctx: RequestContext):
        from .deps import get_services

        services = get_services()
        method = scope.get("method", "GET")
        path = scope.get("path", "")
        trace_id = ctx.trace_id

        # ---------- 会话 ----------
        bearer = headers.get("authorization", "")
        session_token = bearer[7:].strip() if bearer.lower().startswith("bearer ") else _cookie(headers, SESSION_COOKIE)
        user = await anyio.to_thread.run_sync(services.auth.resolve, session_token) if session_token else None
        if user is None:
            return await _send_json(send, 401, error_body(ErrorCode.UNAUTHENTICATED, "请先登录", trace_id), trace_id)
        ctx.user_id, ctx.user_email, ctx.user_name = user.user_id, user.email, user.name
        ctx.is_admin, ctx.session_id = user.is_admin, user.session_id

        # ---------- CSRF：Cookie 认证的写请求必须带自定义头 ----------
        if method in UNSAFE and not bearer and not headers.get("x-requested-with"):
            return await _send_json(
                send,
                403,
                error_body(ErrorCode.PERMISSION_DENIED, "缺少 X-Requested-With 请求头（跨站请求防护）", trace_id),
                trace_id,
            )

        # ---------- 工作区：客户端声明目标，服务端校验成员关系 ----------
        requested = headers.get("x-workspace-id") or _query_param(scope, "ws")
        memberships = await anyio.to_thread.run_sync(services.auth.memberships, user.user_id)
        if requested:
            match = next((m for m in memberships if m["id"] == requested), None)
            if match is None:
                # 不区分“不存在”和“无权限”，避免探测其他工作区是否存在
                return await _send_json(
                    send,
                    403,
                    error_body(ErrorCode.PERMISSION_DENIED, "无权访问该工作区", trace_id, {"workspace_id": requested}),
                    trace_id,
                )
        else:
            match = memberships[0] if memberships else None
        if match is not None:
            ctx.workspace_id, ctx.role = match["id"], match["role"]

        # ---------- 角色 ----------
        if method in UNSAFE and ctx.role == "reviewer" and not path.startswith(REVIEWER_WRITABLE):
            return await _send_json(
                send, 403, error_body(ErrorCode.PERMISSION_DENIED, "审阅者只能查看，不能修改或运行", trace_id), trace_id
            )

        # ---------- 幂等 ----------
        if method in UNSAFE and ctx.workspace_id and not path.startswith("/api/auth/"):
            body, receive = await _read_body(receive)
            if body is None:
                return await _send_json(
                    send, 413, error_body(ErrorCode.INVALID_PARAMETER, "请求体过大", trace_id), trace_id
                )
            key = headers.get("idempotency-key", "").strip()
            if not key and body[:1] == b"{":
                try:
                    parsed = json.loads(body)
                    if isinstance(parsed, dict) and isinstance(parsed.get("idempotency_key"), str):
                        key = parsed["idempotency_key"].strip()
                except ValueError:
                    pass
            if key:
                return await self._idempotent(scope, receive, send, ctx, key[:128], method, path, body)

        return await self.app(scope, receive, _with_trace(send, trace_id))

    async def _idempotent(self, scope, receive, send, ctx, key, method, path, body):
        from .deps import get_services

        db = get_services().db
        trace_id = ctx.trace_id
        query = scope.get("query_string", b"")
        request_hash = hashlib.sha256(method.encode() + b" " + path.encode() + b"?" + query + b"\n" + body).hexdigest()

        def claim():
            now = now_ms()
            with db.tx() as t:
                row = t.fetchone(
                    "SELECT * FROM idempotency_keys WHERE workspace_id=? AND user_id=? AND idem_key=?",
                    (ctx.workspace_id, ctx.user_id, key),
                )
                if row and row["created_at"] < now - IDEMPOTENCY_TTL_MS:
                    t.execute(
                        "DELETE FROM idempotency_keys WHERE workspace_id=? AND user_id=? AND idem_key=?",
                        (ctx.workspace_id, ctx.user_id, key),
                    )
                    row = None
                if row is None:
                    try:
                        t.execute(
                            "INSERT INTO idempotency_keys (workspace_id, user_id, idem_key, method, path, request_hash"
                            ", created_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (ctx.workspace_id, ctx.user_id, key, method, path, request_hash, now),
                        )
                    except IntegrityError:
                        return {"state": "busy"}
                    return {"state": "new"}
                return {"state": "seen", "row": row}

        res = await anyio.to_thread.run_sync(claim)
        if res["state"] == "busy":
            return await _send_json(
                send, 409, error_body(ErrorCode.CONFLICT, "相同幂等键的请求正在处理中", trace_id), trace_id
            )
        if res["state"] == "seen":
            row = res["row"]
            if row["request_hash"] != request_hash:
                return await _send_json(
                    send,
                    409,
                    error_body(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "该幂等键已用于另一份不同内容的请求",
                        trace_id,
                        {"idempotency_key": key},
                    ),
                    trace_id,
                )
            if row["status_code"] is None:
                return await _send_json(
                    send, 409, error_body(ErrorCode.CONFLICT, "相同幂等键的请求正在处理中", trace_id), trace_id
                )
            payload = (row["response"] or "").encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": int(row["status_code"]),
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(payload)).encode()),
                        (b"x-trace-id", trace_id.encode()),
                        (b"idempotent-replay", b"true"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return None

        captured: dict[str, Any] = {"status": 500, "chunks": []}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
            elif message["type"] == "http.response.body":
                captured["chunks"].append(message.get("body", b""))
            await _with_trace(send, trace_id)(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            status = captured["status"]
            body_out = b"".join(captured["chunks"]).decode("utf-8", "replace")

            def finish():
                if status >= 500:
                    # 服务端错误不固化，允许用同一个键重试
                    db.execute(
                        "DELETE FROM idempotency_keys WHERE workspace_id=? AND user_id=? AND idem_key=?",
                        (ctx.workspace_id, ctx.user_id, key),
                    )
                else:
                    db.execute(
                        "UPDATE idempotency_keys SET status_code=?, response=? WHERE workspace_id=? AND user_id=? AND "
                        "idem_key=?",
                        (status, body_out[:2_000_000], ctx.workspace_id, ctx.user_id, key),
                    )

            await anyio.to_thread.run_sync(finish)


async def _read_body(receive):
    chunks: list[bytes] = []
    size = 0
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > MAX_BODY:
            return None, receive
        chunks.append(chunk)
        more = message.get("more_body", False)
    body = b"".join(chunks)
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


def _with_trace(send, trace_id: str):
    async def wrapped(message):
        if message["type"] == "http.response.start":
            hdrs = list(message.get("headers", []))
            if not any(k.lower() == b"x-trace-id" for k, _ in hdrs):
                hdrs.append((b"x-trace-id", trace_id.encode()))
            message = {**message, "headers": hdrs}
        await send(message)

    return wrapped
