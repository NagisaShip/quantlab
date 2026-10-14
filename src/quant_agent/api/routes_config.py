"""配置与模型 Provider 路由。"""

from __future__ import annotations

import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..llm.base import LLMError, Message
from ..llm.router import default_config_dict
from ..platform import context
from ..platform.errors import AppError, ErrorCode
from ..store import Collections
from .deps import get_services
from .schemas import ProviderIn, ProviderTestIn, RoutingIn


def _require_deploy_admin() -> None:
    """模型服务与密钥是部署级配置，只有部署管理员（首个注册用户）可以修改。"""
    if not context.current().is_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有部署管理员可以修改模型服务与密钥")


router = APIRouter(prefix="/api/config", tags=["配置"])


def _key_state(name: str, raw: str, ui_secrets: dict[str, str]) -> tuple[bool, str, str]:
    """(是否真的有密钥, 来源, 脱敏值)。`${ENV}` 占位符只有在环境变量存在时才算有。"""
    from ..llm.providers import _resolve_env

    if ui_secrets.get(name):
        return True, "界面配置", _mask(ui_secrets[name])
    resolved = _resolve_env(raw or "")
    if raw.startswith("${"):
        return bool(resolved), (f"环境变量 {raw[2:-1]}" if resolved else f"未设置环境变量 {raw[2:-1]}"), _mask(resolved)
    if raw == "not-needed":
        return True, "本地模型无需密钥", ""
    return bool(resolved), "配置文件" if resolved else "", _mask(resolved)


@router.get("")
def read_config() -> dict:
    s = get_services()
    secrets = s.read_secrets()
    ui_names = {r.get("name") for r in s.global_store.list(Collections.PROVIDERS)}
    providers = []
    for p in s.config.providers.values():
        has_key, source, masked = _key_state(p.name, p.api_key, secrets)
        providers.append(
            {
                "name": p.name,
                "type": p.type,
                "base_url": p.base_url,
                "has_key": has_key,
                "key_source": source,
                "key_masked": masked,
                "models": p.models,
                "custom": p.name in ui_names,
            }
        )
    return {
        "providers": providers,
        "routing": s.config.routing,
        "limits": s.config.limits,
        "trace": s.config.trace,
        "notify": {k: (v if k == "channel" else _mask(str(v))) for k, v in s.config.notify.items()},
        "exchange": s.config.exchange,
        "config_path": s.config_path,
    }


@router.get("/defaults")
def read_defaults() -> dict:
    """无配置文件时的默认模板，前端"一键恢复默认"用。"""
    return default_config_dict()


@router.get("/models")
def list_models() -> dict:
    """所有可用 provider/model 组合，供角色配置下拉框使用。"""
    s = get_services()
    models = s.router.available_models()
    return {"models": models, "routing": s.config.routing}


@router.get("/presets")
def list_presets() -> dict:
    """常见模型服务商预设，前端"添加 Provider"时一键填充。"""
    return {
        "presets": [
            {
                "name": "deepseek",
                "label": "DeepSeek",
                "type": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "models": ["deepseek-chat", "deepseek-reasoner"],
                "key_env": "DEEPSEEK_API_KEY",
            },
            {
                "name": "openai",
                "label": "[OI]",
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "models": ["gpt-4o", "gpt-4o-mini"],
                "key_env": "OPENAI_API_KEY",
            },
            {
                "name": "anthropic",
                "label": "Anthropic",
                "type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "models": [""],
                "key_env": "ANTHROPIC_API_KEY",
            },
            {
                "name": "dashscope",
                "label": "通义千问（阿里云）",
                "type": "openai_compatible",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "models": ["qwen-max", "qwen-plus"],
                "key_env": "DASHSCOPE_API_KEY",
            },
            {
                "name": "moonshot",
                "label": "Kimi（月之暗面）",
                "type": "openai_compatible",
                "base_url": "https://api.moonshot.cn/v1",
                "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
                "key_env": "MOONSHOT_API_KEY",
            },
            {
                "name": "zhipu",
                "label": "智谱 GLM",
                "type": "openai_compatible",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "models": ["glm-4-plus", "glm-4-flash"],
                "key_env": "ZHIPU_API_KEY",
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


@router.post("/providers")
def upsert_provider(payload: ProviderIn) -> dict:
    """登记或更新一个 Provider。密钥写入本机 secrets.json（接口只回显脱敏值）；留空表示不修改密钥。"""
    _require_deploy_admin()
    s = get_services()
    name = payload.name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,30}", name):
        raise HTTPException(400, "Provider 名称只能包含小写字母、数字、-、_")
    if not payload.base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "base_url 需要以 http:// 或 https:// 开头")
    models = [m.strip() for m in payload.models if m.strip()]
    if not models:
        raise HTTPException(400, "至少填写一个模型名")
    record = {
        "id": name,
        "name": name,
        "type": "anthropic" if payload.type == "anthropic" else "openai_compatible",
        "base_url": payload.base_url.rstrip("/"),
        "models": models,
    }
    s.global_store.upsert(Collections.PROVIDERS, record)
    if payload.api_key.strip():
        secrets = s.read_secrets()
        secrets[name] = payload.api_key.strip()
        s.write_secrets(secrets)
    s.apply_runtime_config()
    s.audit.record(
        "config.provider_upsert",
        object_type="llm_provider",
        object_id=name,
        details={"base_url": record["base_url"], "models": models, "key_changed": bool(payload.api_key.strip())},
    )
    has_key, source, masked = _key_state(name, s.config.providers[name].api_key, s.read_secrets())
    return {"ok": True, "provider": {**record, "has_key": has_key, "key_source": source, "key_masked": masked}}


@router.delete("/providers/{name}")
def delete_provider(name: str) -> dict:
    """删除界面登记的 Provider 与其密钥。配置文件里的 Provider 只清除界面填写的密钥。"""
    _require_deploy_admin()
    s = get_services()
    s.audit.record("config.provider_delete", object_type="llm_provider", object_id=name)
    removed = s.global_store.delete(Collections.PROVIDERS, name)
    secrets = s.read_secrets()
    had_secret = secrets.pop(name, None) is not None
    s.write_secrets(secrets)
    if not removed and not had_secret:
        raise HTTPException(404, "没有界面登记的该 Provider")
    # 重新从文件加载，去掉被删除的运行时覆盖
    from ..llm.router import AgentConfig

    fresh = AgentConfig.load(s.config_path)
    s.config.providers = fresh.providers
    s.config.routing = fresh.routing
    s.apply_runtime_config()
    return {"ok": True}


@router.put("/routing")
def update_routing(payload: RoutingIn) -> dict:
    """按用途指定模型（代码生成 / 推理 / 报告）与降级链。"""
    _require_deploy_admin()
    s = get_services()
    known = {m["target"] for m in s.router.available_models()}
    targets = [payload.default, payload.code_gen, payload.reasoning, payload.report, *payload.fallback]
    unknown = [t for t in targets if t and t not in known]
    if unknown:
        raise HTTPException(400, f"未登记的模型：{', '.join(unknown)}")
    s.global_store.upsert(Collections.SETTINGS, {"id": "routing", **payload.model_dump()})
    s.apply_runtime_config()
    s.audit.record("config.routing", object_type="llm_routing", object_id="routing", details=payload.model_dump())
    return {"ok": True, "routing": s.config.routing}


NO_MODEL_HINT = "未检测到可用的云端模型密钥；本地模型需自行启动 Ollama / vLLM。AI 功能将使用模板降级。"


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


@router.get("/status")
def llm_status() -> dict:
    """当前哪些用途有可用模型。前端用它决定显示"AI"还是"模板降级"。"""
    s = get_services()
    usable = []
    for role in ("default", "code_gen", "reasoning", "report"):
        targets = s.router._targets_for(role)
        ok = [f"{p}/{m}" for p, m in targets if s.router._has_credentials(p) and p != "local"]
        local = [f"{p}/{m}" for p, m in targets if p == "local"]
        usable.append({"role": role, "available": ok, "local": local})
    any_remote = any(u["available"] for u in usable)
    return {"roles": usable, "ready": any_remote, "hint": "" if any_remote else NO_MODEL_HINT}


@router.post("/test")
def test_provider(payload: ProviderTestIn) -> dict:
    """真实调用一次模型，验证密钥与连通性。"""
    s = get_services()
    try:
        result = s.router.chat(
            [Message(role="user", content=payload.prompt)],
            model=payload.target,
            temperature=0.0,
            max_tokens=64,
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


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"
