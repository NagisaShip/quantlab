"""模型配置：工作区 / 个人模型服务商、用途路由、连通性测试、用量统计、成员模型权限与额度申请。

服务商、路由与用量都按工作区隔离（见 llm/hub.py）：
- 工作区管理员维护「工作区共享」服务商与工作区默认路由，并给成员设置模型权限与限额；
- 成员可以添加「个人」服务商（管理员可关闭），设置自己的路由覆盖，查看自己的用量并申请提升额度。
联网搜索密钥仍是部署级配置，只有部署管理员可以修改。
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..llm.base import LLMError, Message
from ..llm.providers import openai_headers
from ..llm.router import default_config_dict
from ..platform import context
from ..platform.errors import AppError, ErrorCode
from ..platform.storage_usage import upload_storage_by_user
from .deps import get_services
from .schemas import LimitRuleIn, ProviderIn, ProviderTestIn, RoutingIn

router = APIRouter(prefix="/api/config", tags=["配置"])


def _preset(
    name: str,
    label: str,
    base_url: str,
    models: list[str],
    *,
    currency: str = "CNY",
    type: str = "openai_compatible",
    key_env: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": type,
        "base_url": base_url,
        "models": models,
        "key_env": key_env,
        "currency": currency,
    }


PROVIDER_PRESETS = [
    _preset(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        ["deepseek-chat", "deepseek-reasoner"],
        key_env="DEEPSEEK_API_KEY",
    ),
    _preset(
        "openai",
        "[OI]",
        "https://api.openai.com/v1",
        ["gpt-4o", "gpt-4o-mini"],
        currency="USD",
        key_env="OPENAI_API_KEY",
    ),
    _preset(
        "anthropic",
        "Anthropic",
        "https://api.anthropic.com",
        ["claude-sonnet-5-5"],
        currency="USD",
        type="anthropic",
        key_env="ANTHROPIC_API_KEY",
    ),
    _preset(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        ["gemini-2.5-flash", "gemini-2.5-pro"],
        currency="USD",
        key_env="GEMINI_API_KEY",
    ),
    _preset(
        "dashscope",
        "通义千问（阿里云）",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ["qwen-max", "qwen-plus"],
        key_env="DASHSCOPE_API_KEY",
    ),
    _preset(
        "dashscope-intl",
        "通义千问（国际站）",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        ["qwen-max", "qwen-plus"],
        currency="USD",
        key_env="DASHSCOPE_API_KEY",
    ),
    _preset(
        "moonshot",
        "Kimi（月之暗面）",
        "https://api.moonshot.cn/v1",
        ["moonshot-v1-8k", "moonshot-v1-32k"],
        key_env="MOONSHOT_API_KEY",
    ),
    _preset(
        "zhipu",
        "智谱 GLM",
        "https://open.bigmodel.cn/api/paas/v4",
        ["glm-4-plus", "glm-4-flash"],
        key_env="ZHIPU_API_KEY",
    ),
    _preset(
        "minimax",
        "MiniMax",
        "https://api.minimax.chat/v1",
        ["MiniMax-Text-01", "abab6.5s-chat"],
        key_env="MINIMAX_API_KEY",
    ),
    _preset(
        "baichuan",
        "百川智能",
        "https://api.baichuan-ai.com/v1",
        ["Baichuan4", "Baichuan3-Turbo"],
        key_env="BAICHUAN_API_KEY",
    ),
    _preset(
        "stepfun",
        "阶跃星辰 StepFun",
        "https://api.stepfun.com/v1",
        ["step-2-16k", "step-1-8k"],
        key_env="STEPFUN_API_KEY",
    ),
    _preset(
        "siliconflow",
        "硅基流动 SiliconFlow",
        "https://api.siliconflow.cn/v1",
        ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        key_env="SILICONFLOW_API_KEY",
    ),
    _preset(
        "volcengine",
        "火山引擎方舟（豆包）",
        "https://ark.cn-beijing.volces.com/api/v3",
        ["doubao-seed-1-6-250615", "doubao-1-5-pro-32k-250115"],
        key_env="ARK_API_KEY",
    ),
    _preset(
        "hunyuan",
        "腾讯混元",
        "https://api.hunyuan.cloud.tencent.com/v1",
        ["hunyuan-turbos-latest", "hunyuan-large"],
        key_env="HUNYUAN_API_KEY",
    ),
    _preset(
        "qianfan",
        "百度千帆（文心）",
        "https://qianfan.baidubce.com/v2",
        ["ernie-4.0-8k-latest", "ernie-speed-128k"],
        key_env="QIANFAN_API_KEY",
    ),
    _preset(
        "modelscope",
        "魔搭 ModelScope",
        "https://api-inference.modelscope.cn/v1",
        ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
        key_env="MODELSCOPE_API_KEY",
    ),
    _preset(
        "opencode",
        "OpenCode Go",
        "https://opencode.ai/inference/go/v1",
        ["deepseek-v4.1-flash", "deepseek-v4-pro"],
        currency="USD",
        type="openai_responses",
        key_env="OPENCODE_API_KEY",
    ),
    _preset(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
        currency="USD",
        key_env="OPENROUTER_API_KEY",
    ),
    _preset(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
        currency="USD",
        key_env="GROQ_API_KEY",
    ),
    _preset(
        "mistral",
        "Mistral AI",
        "https://api.mistral.ai/v1",
        ["mistral-large-latest", "mistral-small-latest"],
        currency="USD",
        key_env="MISTRAL_API_KEY",
    ),
    _preset("xai", "xAI Grok", "https://api.x.ai/v1", ["grok-4.6", "grok-4.7"], currency="USD", key_env="XAI_API_KEY"),
    _preset(
        "together",
        "Together AI",
        "https://api.together.xyz/v1",
        ["meta-llama/Llama-3.3-70B-Instruct-Turbo", "mistralai/Mixtral-8x7B-Instruct-v0.1"],
        currency="USD",
        key_env="TOGETHER_API_KEY",
    ),
    _preset(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
        ["accounts/fireworks/models/llama-v3p3-70b-instruct", "accounts/fireworks/models/qwen2p5-72b-instruct"],
        currency="USD",
        key_env="FIREWORKS_API_KEY",
    ),
    _preset(
        "cerebras",
        "Cerebras",
        "https://api.cerebras.ai/v1",
        ["llama-3.3-70b", "llama3.1-8b"],
        currency="USD",
        key_env="CEREBRAS_API_KEY",
    ),
    _preset(
        "perplexity",
        "Perplexity",
        "https://api.perplexity.ai",
        ["sonar", "sonar-pro"],
        currency="USD",
        key_env="PERPLEXITY_API_KEY",
    ),
    _preset(
        "cohere",
        "Cohere",
        "https://api.cohere.ai/compatibility/v1",
        ["command-r-plus", "command-r"],
        currency="USD",
        key_env="COHERE_API_KEY",
    ),
    _preset(
        "nvidia",
        "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        ["meta/llama-3.3-70b-instruct", "qwen/qwen2.5-coder-32b-instruct"],
        currency="USD",
        key_env="NVIDIA_API_KEY",
    ),
    _preset(
        "github",
        "GitHub Models",
        "https://models.github.ai/inference",
        ["openai/gpt-4o-mini", "openai/gpt-4.1"],
        currency="USD",
        key_env="GITHUB_TOKEN",
    ),
    _preset(
        "azure-openai",
        "Azure OpenAI（替换资源名）",
        "https://YOUR-RESOURCE.openai.azure.com/openai/v1",
        ["gpt-4.1-mini"],
        currency="USD",
        key_env="AZURE_OPENAI_API_KEY",
    ),
    _preset(
        "cloudflare",
        "Cloudflare Workers AI（替换账号 ID）",
        "https://api.cloudflare.com/client/v4/accounts/YOUR_ACCOUNT_ID/ai/v1",
        ["@cf/meta/llama-3.1-8b-instruct"],
        currency="USD",
        key_env="CLOUDFLARE_API_TOKEN",
    ),
    _preset(
        "nebius",
        "Nebius AI Studio",
        "https://api.studio.nebius.ai/v1",
        ["meta-llama/Llama-3.3-70B-Instruct", "Qwen/Qwen2.5-72B-Instruct"],
        currency="USD",
        key_env="NEBIUS_API_KEY",
    ),
    _preset(
        "sambanova",
        "SambaNova",
        "https://api.sambanova.ai/v1",
        ["Meta-Llama-3.3-70B-Instruct", "Qwen2.5-72B-Instruct"],
        currency="USD",
        key_env="SAMBANOVA_API_KEY",
    ),
    _preset(
        "upstage",
        "Upstage Solar",
        "https://api.upstage.ai/v1",
        ["solar-pro2", "solar-mini"],
        currency="USD",
        key_env="UPSTAGE_API_KEY",
    ),
    _preset(
        "local", "本地模型（Ollama / vLLM / LM Studio）", "http://127.0.0.1:11434/v1", ["qwen2.5-coder:14b"], key_env=""
    ),
]


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
        v["editable"] = (
            v["scope"] == "personal"
            or ctx.role == "admin"
            or (v["scope"] == "group" and s.model_hub.can_manage_group(ws, v["group_id"]))
        )
        where = {"provider": v["name"], "scope": v["scope"]}
        if v["scope"] == "personal":
            where["user_id"] = v["owner_id"]
        if v["scope"] == "group":
            where["group_id"] = v["group_id"]
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
    return {"presets": PROVIDER_PRESETS}


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
        headers = openai_headers(base, key) if key else {}
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
def delete_provider(
    name: str, scope: Literal["workspace", "personal", "group"] | None = None, group_id: str = ""
) -> dict:
    s = get_services()
    if not s.model_hub.delete(name, scope=_scope(scope), group_id=group_id):
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


@router.get("/provider-quotas", summary="按模型来源查询 5 小时 / 周 / 月 / 累计额度与用量")
def provider_quotas(refresh: bool = Query(False)) -> dict:
    s = get_services()
    ctx, ws = _ctx()
    return s.model_hub.provider_quota_cards(ws, ctx.user_id, ctx.role, refresh=refresh)


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


@router.get("/members", summary="成员权限、限额、存储占用与近 30 天用量（管理员）")
def member_policies() -> dict:
    s = get_services()
    ws = _require_ws_admin()
    stats = {r["user_id"]: r for r in s.model_hub.stats(ws, days=30)["by_user"]}
    storage = upload_storage_by_user(s.db, ws)
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
                "storage": storage.get(
                    m["user_id"],
                    {
                        "used_bytes": 0,
                        "original_bytes": 0,
                        "thumbnail_bytes": 0,
                        "file_count": 0,
                        "latest_at": None,
                    },
                ),
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


class DefaultLimitsIn(BaseModel):
    limits: list[LimitRuleIn] = Field(default_factory=list)


@router.get("/member-defaults", summary="新成员默认限额：没有单独设置限额的成员使用工作区共享模型时按它计算")
def get_member_defaults() -> dict:
    s = get_services()
    _, ws = _ctx()
    return {"limits": s.model_hub.default_limits(ws)}


@router.put("/member-defaults", summary="设置新成员默认限额（管理员；支持每日 / 每周 / 每月 / 累计与滚动时间段）")
def put_member_defaults(payload: DefaultLimitsIn) -> dict:
    s = get_services()
    ctx, ws = _ctx()
    if ctx.role != "admin":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以设置默认限额")
    limits = s.model_hub.set_default_limits(ws, [r.model_dump() for r in payload.limits])
    s.audit.record("config.default_limits", object_type="workspace", object_id=ws, details={"limits": limits})
    return {"limits": limits}


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
    provider: Literal["tavily", "serper", "google_cse", "brave"] = "tavily"
    api_key: str = Field("", max_length=400, description="留空表示删除")
    extra: str | None = Field(None, max_length=200, description="附加参数，例如 Google 可编程搜索的 cx")


class SearchSourceIn(BaseModel):
    label: str = Field("", max_length=40)
    url: str = Field(..., min_length=8, max_length=400, description="SearXNG 实例根地址，或带 {q} 的 RSS 搜索地址")
    kind: Literal["searxng", "rss"] = "searxng"


@router.get("/search", summary="联网搜索配置状态（密钥只回显脱敏值）")
def read_search() -> dict:
    s = get_services()
    return s.websearch.status() if s.websearch else {"configured": False, "providers": []}


@router.put("/search", summary="设置联网搜索密钥（部署级，加密存放；留空删除）")
def update_search(payload: SearchKeyIn) -> dict:
    from ..llm.websearch import PROVIDERS, SECRET_PREFIX

    _require_deploy_admin()
    s = get_services()
    secrets = s.read_secrets()
    key = SECRET_PREFIX + payload.provider
    if payload.api_key.strip():
        secrets[key] = payload.api_key.strip()
    else:
        secrets.pop(key, None)
    extra = PROVIDERS[payload.provider].get("extra")
    if extra and payload.extra is not None:
        if payload.extra.strip():
            secrets[f"{key}_{extra}"] = payload.extra.strip()
        else:
            secrets.pop(f"{key}_{extra}", None)
    s.write_secrets(secrets)
    s.audit.record(
        "config.search",
        object_type="web_search",
        object_id=payload.provider,
        details={"configured": bool(payload.api_key.strip())},
    )
    return s.websearch.status()


@router.post("/search/sources", summary="添加自定义搜索来源（SearXNG / RSS）")
def add_search_source(payload: SearchSourceIn) -> dict:
    _require_deploy_admin()
    s = get_services()
    out = s.websearch.add_custom(s.write_secrets, label=payload.label, url=payload.url, kind=payload.kind, trusted=True)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "添加失败")
    s.audit.record(
        "config.search_source", object_type="web_search", object_id=out["source"]["id"], details=out["source"]
    )
    return s.websearch.status()


@router.delete("/search/sources/{source_id}", summary="删除自定义搜索来源")
def delete_search_source(source_id: str) -> dict:
    _require_deploy_admin()
    s = get_services()
    if not s.websearch.remove_custom(s.write_secrets, source_id):
        raise HTTPException(404, "来源不存在")
    s.audit.record("config.search_source_delete", object_type="web_search", object_id=source_id)
    return s.websearch.status()
