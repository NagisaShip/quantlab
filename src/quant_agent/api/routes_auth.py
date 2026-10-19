"""账号、会话、工作区、成员、邀请、设置与审计。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..platform import context, onboarding
from ..platform.errors import AppError, ErrorCode
from .deps import get_services
from .middleware import SESSION_COOKIE

router = APIRouter(prefix="/api", tags=["账号与工作区"])


class RegisterIn(BaseModel):
    """用户名与邮箱至少填一个；注册时填的邮箱不算已验证，之后在账号设置里用验证码绑定。"""

    username: str = Field("", max_length=32)
    email: str = Field("", max_length=200)
    name: str = Field("", max_length=40)
    password: str = Field(..., min_length=8, max_length=200)
    invite_code: str | None = None


class LoginIn(BaseModel):
    #: 用户名或邮箱（字段名沿用 email，兼容旧客户端；也可以用 login）
    email: str = Field("", max_length=200)
    login: str = Field("", max_length=200)
    password: str
    # 已启用 2FA 的账号必须带上验证码（或恢复码）；不带则返回 401 + details.totp_required
    totp_code: str | None = Field(None, max_length=64)


class TotpEnableIn(BaseModel):
    code: str = Field(..., max_length=64)


class TotpDisableIn(BaseModel):
    """关闭 2FA 需要密码 + 当前验证码（或恢复码）：只凭会话就能关，等于 2FA 白开。"""

    password: str = Field(..., max_length=200)
    code: str = Field(..., max_length=64)


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=200)


class WorkspaceIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


class JoinIn(BaseModel):
    code: str


class InviteIn(BaseModel):
    role: Literal["admin", "member", "reviewer"] = "member"
    email: str | None = None
    expires_in_hours: int = Field(168, ge=1, le=8760, description="邀请码有效期（小时），默认 7 天")
    max_uses: int = Field(10, ge=0, le=1000, description="最多邀请人数；0 表示不限")


class RoleIn(BaseModel):
    role: Literal["admin", "member", "reviewer"]


class ArchiveIn(BaseModel):
    """审计归档请求。不传 retention_days 时用工作区设置里的值（默认 365 天）。"""

    retention_days: int | None = Field(None, ge=1, le=3650)


class FeaturesIn(BaseModel):
    features: dict[str, bool] = {}


class SettingsIn(BaseModel):
    name: str | None = None
    timezone: str | None = None
    quotas: dict | None = None
    features: dict | None = None
    feedback: dict | None = None
    privacy: dict | None = None
    storage: dict | None = None


def _set_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=14 * 24 * 3600,
        secure=request.url.scheme == "https",
        path="/",
    )


def _require_ws_admin() -> str:
    ctx = context.current()
    if not ctx.workspace_id or ctx.role != "admin":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以执行该操作")
    return ctx.workspace_id


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
@router.get("/auth/status")
def auth_status() -> dict:
    """部署状态：是否已有用户、是否开放注册。登录页据此显示“初始化管理员”或“凭邀请码注册”。"""
    s = get_services()
    return {"bootstrapped": s.auth.user_count() > 0, "open_registration": s.auth.open_registration()}


@router.post("/auth/register")
def register(payload: RegisterIn, request: Request, response: Response) -> dict:
    s = get_services()
    res = s.auth.register(
        email=payload.email,
        username=payload.username,
        name=payload.name,
        password=payload.password,
        invite_code=payload.invite_code,
    )
    onboarding.ensure(s.auth.db, res["user_id"], is_admin=res["is_admin"])
    token, _ = s.auth.login(
        email=payload.email or payload.username,
        password=payload.password,
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    _set_cookie(response, token, request)
    s.audit.record(
        "user.register",
        object_type="user",
        object_id=res["user_id"],
        details={"via_invite": bool(payload.invite_code), "bootstrap_admin": res["is_admin"]},
        workspace_id=res["workspace_id"],
        user_id=res["user_id"],
    )
    return {"ok": True, **res}


@router.post("/auth/login")
def login(payload: LoginIn, request: Request, response: Response) -> dict:
    """密码登录。

    已启用 TOTP 的账号：密码正确后不会直接拿到会话。带了 `totp_code` 就当场校验，
    否则返回 401 + `details.totp_required=true`，前端据此提示用户输入验证码并重发本请求。
    （第二步复用本端点而不是单开一个端点：本端点是公共路径，不需要会话；
    密码每次都会被重新校验，口令限速同样生效。）
    """
    s = get_services()
    ident = (payload.login or payload.email).strip()
    if not ident:
        raise AppError(ErrorCode.INVALID_PARAMETER, "请输入用户名或邮箱")
    token, user = s.auth.login(
        email=ident,
        password=payload.password,
        ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
        totp_code=payload.totp_code,
    )
    _set_cookie(response, token, request)
    ws = s.auth.memberships(user["id"])
    s.audit.record(
        "user.login",
        object_type="user",
        object_id=user["id"],
        workspace_id=ws[0]["id"] if ws else None,
        user_id=user["id"],
        details={"mfa": bool(payload.totp_code)},
    )
    return {"ok": True, "token": token, "user": {"id": user["id"], **_public(user)}}


def _public(user: dict) -> dict:
    from ..platform.auth import public_email

    return {"email": public_email(user.get("email")), "username": user.get("username") or "", "name": user.get("name")}


class AccountIn(BaseModel):
    username: str | None = Field(None, max_length=32)
    name: str | None = Field(None, max_length=40)


class QuantlabIdIn(BaseModel):
    password: str = Field(..., max_length=200)


class EmailCodeIn(BaseModel):
    email: str = Field(..., max_length=200)


class EmailVerifyIn(BaseModel):
    email: str = Field(..., max_length=200)
    code: str = Field(..., max_length=12)


@router.get("/auth/account", summary="账号资料：用户名、显示名、绑定的邮箱与是否已验证")
def get_account() -> dict:
    return {"account": get_services().auth.account(context.current().user_id)}


@router.put("/auth/account", summary="修改用户名 / 显示名")
def put_account(payload: AccountIn) -> dict:
    ctx = context.current()
    s = get_services()
    acc = s.auth.update_account(ctx.user_id, username=payload.username, name=payload.name)
    s.audit.record(
        "user.account", object_type="user", object_id=ctx.user_id, details=payload.model_dump(exclude_none=True)
    )
    return {"account": acc}


@router.post("/auth/quantlab-id/rotate", summary="销毁并重新生成 QuantLab UID")
def rotate_quantlab_uid(payload: QuantlabIdIn) -> dict:
    ctx = context.current()
    s = get_services()
    account = s.auth.rotate_quantlab_uid(ctx.user_id, password=payload.password)
    s.audit.record("user.quantlab_uid_rotate", object_type="user", object_id=ctx.user_id)
    return {"account": account}


@router.post("/auth/email/code", summary="给要绑定的邮箱发验证码（60 秒冷却，每小时 5 次）")
def send_email_code(payload: EmailCodeIn) -> dict:
    ctx = context.current()
    s = get_services()
    out = s.auth.send_email_code(ctx.user_id, payload.email)
    s.audit.record("user.email_code", object_type="user", object_id=ctx.user_id, details={"email": out["email"]})
    return out


@router.post("/auth/email/verify", summary="填验证码完成绑定（之后可以用这个邮箱登录）")
def verify_email(payload: EmailVerifyIn) -> dict:
    ctx = context.current()
    s = get_services()
    acc = s.auth.verify_email_code(ctx.user_id, payload.email, payload.code)
    s.audit.record("user.email_bind", object_type="user", object_id=ctx.user_id, details={"email": acc["email"]})
    return {"account": acc}


@router.get("/auth/totp")
def totp_status() -> dict:
    """双因素认证状态。

    只回显是否启用、剩余恢复码数与锁定截止时间——**绝不回显密钥或恢复码**。
    """
    ctx = context.current()
    return {"totp": get_services().auth.totp_status(ctx.user_id)}


@router.post("/auth/totp/setup")
def totp_setup() -> dict:
    """开始启用双因素认证：生成密钥并返回 otpauth:// 链接。

    明文密钥只在这一个响应里出现一次（用户要扫码）；此时尚未启用，
    必须再调 `/api/auth/totp/enable` 用一次真实验证码确认。
    """
    ctx = context.current()
    s = get_services()
    res = s.auth.totp_setup(ctx.user_id, workspace_id=ctx.workspace_id)
    # 审计只记"发起了绑定"，不记密钥本身
    s.audit.record("user.totp_setup", object_type="user", object_id=ctx.user_id)
    return res


@router.post("/auth/totp/enable")
def totp_enable(payload: TotpEnableIn) -> dict:
    """用一次真实验证码确认绑定，并一次性返回恢复码明文。"""
    ctx = context.current()
    s = get_services()
    res = s.auth.totp_enable(ctx.user_id, payload.code)
    s.audit.record(
        "user.totp_enable",
        object_type="user",
        object_id=ctx.user_id,
        details={"recovery_codes": len(res.get("recovery_codes") or [])},
    )
    return res


@router.post("/auth/totp/disable")
def totp_disable(payload: TotpDisableIn) -> dict:
    """关闭双因素认证（需要密码 + 当前验证码或恢复码）。"""
    ctx = context.current()
    s = get_services()
    res = s.auth.totp_disable(ctx.user_id, password=payload.password, code=payload.code)
    s.audit.record("user.totp_disable", object_type="user", object_id=ctx.user_id)
    return res


@router.post("/auth/totp/recovery-codes")
def totp_recovery_codes() -> dict:
    """重新生成恢复码（旧的立即全部作废）。明文只返回这一次。"""
    ctx = context.current()
    s = get_services()
    codes = s.auth.totp_recovery_codes(ctx.user_id)
    s.audit.record("user.totp_recovery_codes", object_type="user", object_id=ctx.user_id, details={"count": len(codes)})
    return {"recovery_codes": codes, "note": "恢复码只显示这一次，请离线保存；旧的恢复码已全部作废。"}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if token:
        get_services().auth.logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me() -> dict:
    ctx = context.current()
    s = get_services()
    workspaces = s.auth.memberships(ctx.user_id)
    current = s.auth.workspace(ctx.workspace_id) if ctx.workspace_id else None
    acc = s.auth.account(ctx.user_id)
    onboarding_state = onboarding.get(s.auth.db, ctx.user_id, is_admin=ctx.is_admin)
    return {
        "user": {
            "id": ctx.user_id,
            "email": acc["email"],
            "username": acc["username"],
            "email_verified": acc["email_verified"],
            "quantlab_uid": acc.get("quantlab_uid", ""),
            "name": ctx.user_name,
            "is_admin": ctx.is_admin,
            "is_primary_admin": bool(ctx.is_admin and s.auth.primary_admin_id() == ctx.user_id),
            "avatar_color": s.social.profile(ctx.workspace_id or "", ctx.user_id or "").get("avatar_color", "#2E5C9A")
            if s.social and ctx.workspace_id
            else "#2E5C9A",
        },
        "workspaces": workspaces,
        "workspace": current
        and {
            "id": current["id"],
            "name": current["name"],
            "role": ctx.role,
            "is_demo": current["is_demo"],
            "owner_id": current.get("owner_id") or "",
            "is_primary_admin": str(current.get("owner_id") or "") == (ctx.user_id or ""),
            "settings": current["settings"],
        },
        "onboarding": onboarding_state,
        "live_enabled": False,
    }


@router.post("/auth/password")
def change_password(payload: PasswordIn) -> dict:
    ctx = context.current()
    s = get_services()
    s.auth.change_password(ctx.user_id, payload.old_password, payload.new_password)
    s.audit.record("user.password_change", object_type="user", object_id=ctx.user_id)
    return {"ok": True, "note": "所有会话已失效，请重新登录"}


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------
@router.get("/workspaces")
def list_workspaces() -> dict:
    ctx = context.current()
    return {"workspaces": get_services().auth.memberships(ctx.user_id), "current": ctx.workspace_id}


@router.post("/workspaces")
def create_workspace(payload: WorkspaceIn) -> dict:
    ctx = context.current()
    s = get_services()
    ws_id = s.auth.create_workspace(owner_id=ctx.user_id, name=payload.name)
    s.audit.record("workspace.create", object_type="workspace", object_id=ws_id, workspace_id=ws_id)
    return {"id": ws_id}


@router.post("/workspaces/join")
def join_workspace(payload: JoinIn) -> dict:
    ctx = context.current()
    s = get_services()
    ws_id = s.auth.join_with_invite(ctx.user_id, payload.code)
    s.audit.record("member.join", object_type="workspace", object_id=ws_id, workspace_id=ws_id)
    return {"id": ws_id}


@router.get("/workspace")
def workspace_detail() -> dict:
    ctx = context.current()
    s = get_services()
    ws = s.auth.workspace(context.current_workspace())
    return {"workspace": {**ws, "role": ctx.role}}


@router.put("/workspace/settings")
def update_settings(payload: SettingsIn) -> dict:
    ws_id = _require_ws_admin()
    s = get_services()
    patch = payload.model_dump(exclude_none=True)
    ws = s.auth.update_settings(ws_id, patch)
    s.audit.record("workspace.settings", object_type="workspace", object_id=ws_id, details=patch)
    return {"workspace": ws}


@router.get("/workspace/storage", summary="工作区存储分类、磁盘占用与保留策略")
def workspace_storage() -> dict:
    from ..platform import storage

    ws = _require_ws_admin()
    return storage.scan(get_services(), ws)


class StorageCleanupIn(BaseModel):
    category: Literal["conversations", "feedback", "backtests", "runs", "uploads", "cache", "audit"]
    keep_days: int = Field(30, ge=0, le=3650)
    dry_run: bool = True


@router.post("/workspace/storage/cleanup", summary="按分类清理超过保留期的数据（默认只预览）")
def workspace_storage_cleanup(payload: StorageCleanupIn) -> dict:
    from ..platform import storage

    ws = _require_ws_admin()
    return storage.cleanup(get_services(), ws, payload.category, keep_days=payload.keep_days, dry_run=payload.dry_run)


@router.get("/workspace/features", summary="工作区功能开关与实现状态")
def workspace_features() -> dict:
    from ..platform.features import enabled, registry

    ctx = context.current()
    s = get_services()
    ws = s.auth.workspace(ctx.workspace_id)
    return {"enabled": enabled(ws.get("settings")), "registry": registry()}


@router.put("/workspace/features", summary="管理员动态开关现货/永续/真实资金等能力")
def update_workspace_features(payload: FeaturesIn) -> dict:
    from ..platform.features import enabled, validate_patch

    ws_id = _require_ws_admin()
    s = get_services()
    try:
        patch = validate_patch(payload.features)
    except ValueError as exc:
        raise AppError(ErrorCode.INVALID_PARAMETER, str(exc)) from exc
    ws = s.auth.update_settings(ws_id, {"features": patch})
    s.audit.record("workspace.features", object_type="workspace", object_id=ws_id, details=patch)
    return {"enabled": enabled(ws.get("settings")), "workspace": ws}


@router.get("/workspace/members")
def members() -> dict:
    s = get_services()
    ws_id = context.current_workspace()
    return {
        "members": s.auth.members(ws_id),
        "invites": s.auth.invites(ws_id) if context.current().role == "admin" else [],
    }


@router.post("/workspace/invites")
def create_invite(payload: InviteIn) -> dict:
    ws_id = _require_ws_admin()
    s = get_services()
    inv = s.auth.create_invite(
        workspace_id=ws_id,
        role=payload.role,
        created_by=context.current().user_id,
        email=payload.email,
        expires_in_hours=payload.expires_in_hours,
        max_uses=payload.max_uses,
    )
    s.audit.record(
        "member.invite",
        object_type="invite",
        object_id=inv["id"],
        details={
            "role": payload.role,
            "email": payload.email,
            "expires_in_hours": payload.expires_in_hours,
            "max_uses": payload.max_uses,
        },
    )
    return {
        "invite": inv,
        "note": "邀请码可邀请多人，明文只显示这一次；请通过安全渠道发给对方",
    }


@router.delete("/workspace/invites/{invite_id}")
def revoke_invite(invite_id: str) -> dict:
    ws_id = _require_ws_admin()
    s = get_services()
    s.auth.revoke_invite(ws_id, invite_id)
    s.audit.record(
        "member.invite_revoke", object_type="invite", object_id=invite_id, details={"reason": "admin_revoke"}
    )
    return {"ok": True}


@router.put("/workspace/members/{user_id}")
def set_member_role(user_id: str, payload: RoleIn) -> dict:
    ws_id = _require_ws_admin()
    s = get_services()
    s.auth.set_role(ws_id, user_id, payload.role)
    s.audit.record("member.role", object_type="user", object_id=user_id, details={"role": payload.role})
    return {"ok": True}


@router.delete("/workspace/members/{user_id}")
def remove_member(user_id: str) -> dict:
    ws_id = _require_ws_admin()
    s = get_services()
    s.auth.remove_member(ws_id, user_id)
    s.audit.record("member.remove", object_type="user", object_id=user_id)
    return {"ok": True}


@router.get("/workspace/audit")
def audit_log(
    limit: int = Query(200, ge=1, le=1000),
    action: str | None = None,
    before_id: int | None = None,
    since: int | None = None,
    until: int | None = None,
    user_id: str | None = None,
    q: str | None = Query(None, max_length=160),
) -> dict:
    """审计日志查询。只有工作区管理员可读——它记录的是全工作区的操作轨迹。"""
    ws_id = _require_ws_admin()
    rows = get_services().audit.list(
        ws_id,
        limit=min(limit + 1, 1000),
        action=action,
        before_id=before_id,
        since=since,
        until=until,
        user_id=user_id,
        q=q,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return {
        "items": items,
        "has_more": has_more,
        "next_before_id": items[-1]["id"] if has_more and items else None,
    }


@router.get("/workspace/audit/export", response_class=PlainTextResponse)
def audit_export(
    action: str | None = None,
    since: int | None = None,
    until: int | None = None,
    user_id: str | None = None,
    q: str | None = Query(None, max_length=160),
) -> PlainTextResponse:
    """导出审计日志为 CSV。

    单元格经 `platform/csvsafe.py` 中和：以 `=`、`+`、`-`、`@` 开头的字段前加单引号，
    否则用表格软件打开时会被当公式执行。details 里的凭证已在写入时掩码，导出不会二次泄露。
    """
    ws_id = _require_ws_admin()
    s = get_services()
    body = s.audit.export_csv(ws_id, action=action, since=since, until=until, user_id=user_id, q=q)
    s.audit.record("audit.export", object_type="audit_log", details={"action": action, "since": since, "until": until})
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit-log.csv"'},
    )


@router.post("/workspace/audit/archive")
def audit_archive(payload: ArchiveIn | None = None) -> dict:
    """按保留期归档过期审计记录（默认 365 天）。只有工作区管理员可执行。"""
    ws_id = _require_ws_admin()
    s = get_services()
    ws = s.auth.workspace(ws_id)
    default_days = int((ws.get("settings") or {}).get("audit_retention_days", 365))
    days = int((payload.retention_days if payload and payload.retention_days else default_days) or 365)
    if days <= 0:
        raise AppError(ErrorCode.INVALID_PARAMETER, "保留天数必须为正")
    res = s.audit.archive_expired(ws_id, retention_days=days)
    s.audit.record("audit.archive", object_type="audit_log", details={"retention_days": days, **res})
    return {"retention_days": days, **res}


@router.get("/workspace/audit/archived")
def audit_archived(limit: int = Query(200, ge=1, le=1000)) -> dict:
    """查看已归档的审计记录（归档不等于删除，仍可追溯）。"""
    ws_id = _require_ws_admin()
    return {"items": get_services().audit.list_archived(ws_id, limit=limit)}
