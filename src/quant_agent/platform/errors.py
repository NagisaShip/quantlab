"""统一错误契约。

所有 API 错误返回同一形状：

    {"code": "INVALID_PARAMETER", "message": "人能读懂的中文说明", "trace_id": "…", "details": {...}}

`code` 是稳定的机器可读枚举，前端与脚本按它分支；`message` 面向使用者；
`trace_id` 与日志、审计记录对应；`details` 放结构化补充信息（例如校验失败的字段列表）。
错误响应中绝不包含堆栈、密钥或内部路径。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class ErrorCode:
    # v2.0 文档要求区分的 9 类
    PERMISSION_DENIED = "PERMISSION_DENIED"  # 权限不足
    DATA_INCOMPLETE = "DATA_INCOMPLETE"  # 数据不完整
    MARKET_DATA_STALE = "MARKET_DATA_STALE"  # 行情过期
    UPSTREAM_RATE_LIMITED = "UPSTREAM_RATE_LIMITED"  # 上游限流
    INVALID_PARAMETER = "INVALID_PARAMETER"  # 参数无效
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"  # 配额耗尽
    TASK_FAILED = "TASK_FAILED"  # 任务失败
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"  # 密钥撤销
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    # 通用
    UNAUTHENTICATED = "UNAUTHENTICATED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


STATUS_TO_CODE = {
    400: ErrorCode.INVALID_PARAMETER,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.INVALID_PARAMETER,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.INVALID_PARAMETER,
    422: ErrorCode.INVALID_PARAMETER,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.UPSTREAM_UNAVAILABLE,
    503: ErrorCode.UPSTREAM_UNAVAILABLE,
}

CODE_TO_STATUS = {
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.DATA_INCOMPLETE: 422,
    ErrorCode.MARKET_DATA_STALE: 409,
    ErrorCode.UPSTREAM_RATE_LIMITED: 503,
    ErrorCode.INVALID_PARAMETER: 400,
    ErrorCode.QUOTA_EXHAUSTED: 429,
    ErrorCode.TASK_FAILED: 422,
    ErrorCode.CREDENTIAL_REVOKED: 409,
    ErrorCode.UNSUPPORTED_CAPABILITY: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.UPSTREAM_UNAVAILABLE: 502,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(HTTPException):
    """带错误码的业务异常。也是 HTTPException，旧代码的 `except HTTPException` 依然有效。"""

    def __init__(self, code: str, message: str, *, details: Any = None, status: int | None = None) -> None:
        super().__init__(status_code=status or CODE_TO_STATUS.get(code, 400), detail=message)
        self.code = code
        self.message = message
        self.details = details or {}


def error_body(code: str, message: str, trace_id: str, details: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "trace_id": trace_id, "details": details or {}}
