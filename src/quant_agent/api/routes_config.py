"""模型配置：工作区 / 个人模型服务商、用途路由、连通性测试、用量统计、成员模型权限与额度申请。

服务商、路由与用量都按工作区隔离（见 llm/hub.py）：
- 工作区管理员维护「工作区共享」服务商与工作区默认路由，并给成员设置模型权限与限额；
- 成员可以添加「个人」服务商（管理员可关闭），设置自己的路由覆盖，查看自己的用量并申请提升额度。
联网搜索密钥仍是部署级配置，只有部署管理员可以修改。
"""

from __future__ import annotations

from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..llm.base import LLMError, Message
from ..llm.router import default_config_dict
from ..platform import context
from ..platform.errors import AppError, ErrorCode
from .deps import get_services
from .schemas import LimitRuleIn, ProviderIn, ProviderTestIn, RoutingIn

router = APIRouter(prefix="/api/config", tags=["配置"])


def _require_deploy_admin() -> None:
    """联网搜索密钥是部署级配置，只有部署管理员（首个注册用户）可以修改。"""
    if not context.current().is_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有部署管理员可以修改联网搜索密钥")


def _require_ws_admin() -> str:
    ctx = context.current()
    if ctx.role != "admin":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以执行该操作")
    return context.current_workspace()


def _ctx():
    ctx = context.current()
    return ctx, context.current_workspace()


@router.get("", summary="当前成员可见的模型服务商、路由与自己的模型权限（密钥脱敏）")
def read_config() -> dict:
    s = get_services()
    ctx, ws = _ctx()
    pol = s.model_hub.policy(ws, ctx.user_id or "", ctx.role)
    providers = []
    for rec in s.model_hub.visible(ws, ctx.user_id, ctx.role):
        v = s.model_hub.view(rec)
        v["custom"] = True  # 兼容旧前端：界面登记的都可以删除
        v["editable"] = v["scope"] == "personal" or ctx.role == "admin"
        where = {"provider": v["name"], "scope": v["scope"]}
        if v["scope"] == "personal":
            where["user_id"] = v["owner_id"]
        v["limit_status"] = s.model_hub.rule_status(ws, v["limits"], **where)
        providers.append(v)
    return {
        "providers": providers,
        "routing": s.model_hub.routing(ws, ctx.user_id),
        "routing_layers": s.model_hub.routing_layers(ws, ctx.user_id),
        "me": {
            "role": ctx.role,
            "is_ws_admin": ctx.role == "admin",
            "use_workspace_models": pol["use_workspace_models"] or ctx.role == "admin",
            "allow_personal_models": pol["allow_personal_models"] and ctx.role != "reviewer",
        },
        "limits": s.config.limits,
        "config_path": s.config_path,
    }


@router.get("/defaults", summary="默认配置模板（不含任何服务商）")
def read_defaults() -> dict:
    return default_config_dict()


@router.get("/models", summary="当前成员可用的 provider/model 组合")
def list_models() -> dict:
    s = get_services()
    return {"models": s.router.available_models(), "routing": s.router.config.routing}


@router.get("/presets", summary="常见模型服务商模板（添加服务商时一键填充，不会自动启用）")
def list_presets() -> dict:
    return {
        "presets": [
            {
                "name": "deepseek",
                "label": "DeepSeek",
                "type": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "models": ["deepseek-chat", "deepseek-reasoner"],
                "key_env": "",
            },
            {
                "name": "openai",
                "label": "[OI]",
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "models": ["gpt-4o", "gpt-4o-mini"],
                "key_env": "",
            },
            {
                "name": "anthropic",
                "label": "Anthropic",
                "type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "models": ["claude-sonnet-5-5"],
                "key_env": "",
            },
            {
                "name": "dashscope",
                "label": "通义千问（阿里云）",
                "type": "openai_compatible",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "models": ["qwen-max", "qwen-plus"],
                "key_env": "",
            },
            {
                "name": "moonshot",
                "label": "Kimi（月之暗面）",
                "type": "openai_compatible",
                "base_url": "https://api.moonshot.cn/v1",
                "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
                "key_env": "",
            },
            {
                "name": "zhipu",
                "label": "智谱 GLM",
                "type": "openai_compatible",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "models": ["glm-4-plus", "glm-4-flash"],
                "key_env": "",
            },
            {
                "name": "local",
                "label": "本地模型（Ollama / vLLM）",
                "type": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "models": ["qwen2.5-coder:14b"],
                "key_env": "",
            },
        ]
    }


def _scope(requested: str | None) -> str:
    if requested:
        return requested
    return "workspace" if context.current().role == "admin" else "personal"


@router.post("/providers", summary="登记或更新一个模型服务商（工作区共享 / 个人）")
def upsert_provider(payload: ProviderIn) -> dict:
    s = get_services()
    data = payload.model_dump()
    data["model_settings"] = {k: v.model_dump() for k, v in payload.model_settings.items()}
    rec = s.model_hub.upsert(data, scope=_scope(payload.scope))
    return {"ok": True, "provider": s.model_hub.view(rec)}


class DiscoverIn(BaseModel):
    name: str = Field("", max_length=40, description="已登记的服务商；密钥留空时用它已保存的密钥")
    type: Literal["openai_compatible", "openai_responses", "anthropic", "local"] = "openai_compatible"
    base_url: str = Field(..., max_length=400)
    api_key: str = Field("", max_length=400)


@router.post("/providers/discover", summary="向服务商查询可用模型列表（OpenAI 兼容 /models 或 Anthropic /v1/models）")
def discover_models(payload: DiscoverIn) -> dict:
    s = get_services()
    ctx, ws = _ctx()
    if ctx.role == "reviewer":
        raise AppError(ErrorCode.PERMISSION_DENIED, "审阅者（只读）不能查询服务商")
    base = payload.base_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(400, "API 地址需要以 http:// 或 https:// 开头")
    key = payload.api_key.strip()
    if not key and payload.name:
        rec = s.model_hub.find(ws, ctx.user_id, ctx.role, payload.name.strip().lower())
        # 成员不能借管理员的密钥去探测任意地址：只有地址一致时才使用已保存的密钥
        if rec and rec.get("base_url", "").rstrip("/") == base:
            key = s.model_hub.api_key(rec)
    if payload.type == "anthropic":
        url = base + ("/models" if base.endswith("/v1") else "/v1/models")
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        url = base + "/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        params = {"limit": 1000} if payload.type == "anthropic" else None
        resp = httpx.get(url, headers=headers, params=params, timeout=10.0)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"无法连接 {url}：{e.__class__.__name__}") from e
    if resp.status_code in (401, 403):
        raise HTTPException(400, "服务商拒绝了请求：API 密钥无效或没有权限")
    if resp.status_code >= 400:
        raise HTTPException(502, f"服务商返回 {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise HTTPException(502, "服务商返回的不是 JSON") from e
    items = data.get("data") if isinstance(data, dict) else data
    ids = sorted({str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")})
    return {"models": ids}


@router.delete("/providers/{name}", summary="删除一个模型服务商与其密钥")
def delete_provider(name: str, scope: Literal["workspace", "personal"] | None = None) -> dict:
    s = get_services()
    if not s.model_hub.delete(name, scope=_scope(scope)):
        raise HTTPException(404, "没有这个服务商")
    return {"ok": True}


@router.put("/routing", summary="按用途设置模型路由与降级链（工作区默认 / 我的覆盖）")
def update_routing(payload: RoutingIn, scope: Literal["workspace", "personal"] | None = None) -> dict:
    s = get_services()
    known = {m["target"] for m in s.router.available_models()}
    targets = [payload.default, payload.code_gen, payload.reasoning, payload.report, *payload.fallback]
    unknown = [t for t in targets if t and t not in known]
    if unknown:
        raise HTTPException(400, f"未登记的模型：{', '.join(unknown)}")
    s.model_hub.set_routing(payload.model_dump(), scope=_scope(scope))
    s.audit.record("config.routing", object_type="llm_routing", object_id=_scope(scope), details=payload.model_dump())
    return {"ok": True, "routing": s.router.config.routing}


NO_MODEL_HINT = "还没有可用的模型：在「设置 → 模型」里添加一个服务商，或请工作区管理员共享。AI 功能将使用模板降级。"


@router.get("/status", summary="各用途是否有可用模型（决定 AI 功能是否降级）")
def llm_status() -> dict:
    s = get_services()
    cfg = s.router.config
    usable = []
    for role in ("default", "code_gen", "reasoning", "report"):
        targets = s.router._targets_for(role, cfg=cfg)
        ok = [f"{p}/{m}" for p, m in targets if s.router._has_credentials(p, cfg)]
        usable.append({"role": role, "available": ok, "local": []})
    any_ready = any(u["available"] for u in usable) or any(s.router._has_credentials(p, cfg) for p in cfg.providers)
    return {"roles": usable, "ready": any_ready, "hint": "" if any_ready else NO_MODEL_HINT}


@router.post("/test", summary="真实调用一次模型，验证密钥与连通性")
def test_provider(payload: ProviderTestIn) -> dict:
    s = get_services()
    try:
        result = s.router.chat(
            [Message(role="user", content=payload.prompt)],
            model=payload.target,
            temperature=0.0,
            max_tokens=1024,  # 推理模型会先花掉一部分输出额度在思考上，太小会只剩空回复
            effort="low",
        )
    except LLMError as e:
        return {"ok": False, "error": str(e), "target": payload.target}
    return {
        "ok": True,
        "target": payload.target,
        "model": result.model,
        "provider": result.provider,
        "reply": result.content[:300],
        "usage": result.usage,
    }


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------
@router.get("/usage", summary="模型用量与费用统计（我的 / 整个工作区），以及当前限额状态")
def usage(
    days: int = Query(30, ge=1, le=90),
    view: Literal["me", "workspace"] = "me",
) -> dict:
    s = get_services()
    ctx, ws = _ctx()
    if view == "workspace" and ctx.role != "admin":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以查看整个工作区的用量")
    stats = s.model_hub.stats(ws, days=days, user_id=None if view == "workspace" else ctx.user_id)
    if view == "workspace" and s.auth is not None:
        names = {m["user_id"]: m.get("name") or m.get("email") for m in s.auth.members(ws)}
        for row in stats["by_user"]:
            row["name"] = names.get(row["user_id"], row["user_id"])
    pol = s.model_hub.policy(ws, ctx.user_id or "", ctx.role)
    stats["my_limits"] = s.model_hub.rule_status(ws, pol["limits"], scope="workspace", user_id=ctx.user_id)
    stats["view"] = view
    return stats


# ---------------------------------------------------------------------------
# 成员模型权限与限额（管理员）
# ---------------------------------------------------------------------------
class MemberPolicyIn(BaseModel):
    use_workspace_models: bool | None = Field(None, description="可以使用工作区共享的模型（调用走管理员的 API）")
    allow_personal_models: bool | None = Field(None, description="可以添加自己的个人模型服务商")
    limits: list[LimitRuleIn] | None = Field(None, description="使用工作区共享模型的限额")


@router.get("/members", summary="成员的模型权限、限额与近 30 天用量（管理员）")
def member_policies() -> dict:
    s = get_services()
    ws = _require_ws_admin()
    stats = {r["user_id"]: r for r in s.model_hub.stats(ws, days=30)["by_user"]}
    out = []
    for m in s.auth.members(ws):
        pol = s.model_hub.policy(ws, m["user_id"], m["role"])
        u = stats.get(m["user_id"], {})
        out.append(
            {
                "user_id": m["user_id"],
                "name": m.get("name") or "",
                "email": m.get("email") or "",
                "role": m["role"],
                **pol,
                "limit_status": s.model_hub.rule_status(ws, pol["limits"], scope="workspace", user_id=m["user_id"]),
                "usage_30d": {
                    "calls": int(u.get("calls") or 0),
                    "tokens": int(u.get("tokens") or 0),
                    "cost": u.get("cost", "0.0000"),
                },
            }
        )
    return {"members": out}


@router.put("/members/{user_id}", summary="设置成员的模型权限与限额（管理员）")
def set_member_policy(user_id: str, payload: MemberPolicyIn) -> dict:
    s = get_services()
    ws = _require_ws_admin()
    if not any(m["user_id"] == user_id for m in s.auth.members(ws)):
        raise HTTPException(404, "不是本工作区成员")
    patch = payload.model_dump()
    if payload.limits is not None:
        patch["limits"] = [r.model_dump() for r in payload.limits]
    pol = s.model_hub.set_policy(ws, user_id, patch)
    s.audit.record("member.model_policy", object_type="member", object_id=user_id, details=pol)
    return {"ok": True, "policy": pol}


# ---------------------------------------------------------------------------
# 额度申请
# ---------------------------------------------------------------------------
class QuotaRequestIn(LimitRuleIn):
    reason: str = Field("", max_length=300)


class DecideIn(BaseModel):
    approve: bool
    note: str = Field("", max_length=200)


@router.get("/quota-requests", summary="额度申请：成员看自己的，管理员看全部")
def list_quota_requests() -> dict:
    s = get_services()
    ctx, ws = _ctx()
    return {"requests": s.model_hub.requests(ws, None if ctx.role == "admin" else ctx.user_id)}


@router.post("/quota-requests", summary="申请提升使用工作区模型的额度")
def create_quota_request(payload: QuotaRequestIn) -> dict:
    s = get_services()
    req = s.model_hub.create_request(payload.model_dump())
    s.audit.record(
        "member.quota_request",
        object_type="quota_request",
        object_id=req["id"],
        details={k: req[k] for k in ("metric", "window", "limit")},
    )
    return {"ok": True, "request": req}


@router.post("/quota-requests/{rid}/decide", summary="批准或拒绝额度申请（管理员）；批准后自动更新该成员的限额")
def decide_quota_request(rid: str, payload: DecideIn) -> dict:
    s = get_services()
    req = s.model_hub.decide_request(rid, payload.approve, payload.note)
    s.audit.record("member.quota_decide", object_type="quota_request", object_id=rid, details={"status": req["status"]})
    return {"ok": True, "request": req}


# ---------------------------------------------------------------------------
# 联网搜索（部署级）
# ---------------------------------------------------------------------------
class SearchKeyIn(BaseModel):
    provider: Literal["tavily"] = "tavily"
    api_key: str = Field("", max_length=400, description="留空表示删除")


@router.get("/search", summary="联网搜索配置状态（密钥只回显脱敏值）")
def read_search() -> dict:
    s = get_services()
    return s.websearch.status() if s.websearch else {"configured": False, "providers": []}


@router.put("/search", summary="设置联网搜索密钥（部署级，加密存放；留空删除）")
def update_search(payload: SearchKeyIn) -> dict:
    from ..llm.websearch import SECRET_PREFIX

    _require_deploy_admin()
    s = get_services()
    secrets = s.read_secrets()
    key = SECRET_PREFIX + payload.provider
    if payload.api_key.strip():
        secrets[key] = payload.api_key.strip()
    else:
        secrets.pop(key, None)
    s.write_secrets(secrets)
    s.audit.record(
        "config.search",
        object_type="web_search",
        object_id=payload.provider,
        details={"configured": bool(payload.api_key.strip())},
    )
    return s.websearch.status()
