"""配置加载 + 模型路由 + 降级链。

读取 agent.config.toml（项目级）与 ~/.quant_agent/config.toml（用户级），
用户级覆盖项目级。负责按角色挑选 provider，并在失败/超额时降级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from .base import ChatResult, LLMError, Message, Tool
from .providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    OpenAIResponsesProvider,
    anthropic_effort_mode,
    openai_effort_mode,
)


@dataclass
class ProviderConfig:
    name: str
    type: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    # 按目录模型 ID 的可选设置：label 菜单显示名、request_model 实际请求的模型、
    # context_window 上下文窗口、effort 默认思考等级、inputs 支持的输入类型
    model_settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    def setting(self, model: str) -> dict[str, Any]:
        return self.model_settings.get(model) or {}

    def request_model(self, model: str) -> str:
        return str(self.setting(model).get("request_model") or "").strip() or model


@dataclass
class AgentConfig:
    providers: dict[str, ProviderConfig]
    routing: dict[str, Any]
    limits: dict[str, Any]
    trace: dict[str, Any]
    notify: dict[str, Any] = field(default_factory=dict)
    exchange: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, project_path: str | Path | None = None, *, allow_default: bool = True) -> AgentConfig:
        merged: dict[str, Any] = {}
        candidates = []
        if project_path:
            candidates.append(Path(project_path))
        candidates.append(Path.cwd() / "agent.config.toml")
        candidates.append(Path.home() / ".quant_agent" / "config.toml")
        # 用户级在后，覆盖项目级
        candidates = list(reversed(candidates))
        for path in candidates:
            if path and Path(path).exists():
                with open(path, "rb") as f:
                    _deep_merge(merged, tomllib.load(f))
        if not merged:
            if not allow_default:
                raise LLMError("未找到 agent.config.toml，请运行 `qagent init` 生成配置")
            merged = default_config_dict()

        providers = {
            name: ProviderConfig(
                name=name,
                type=cfg.get("type", "openai_compatible"),
                base_url=cfg.get("base_url", ""),
                api_key=cfg.get("api_key", ""),
                models=cfg.get("models", []),
                model_settings=cfg.get("model_settings", {}),
            )
            for name, cfg in merged.get("providers", {}).items()
        }
        return cls(
            providers=providers,
            routing=merged.get("routing", {}),
            limits=merged.get("limits", {}),
            trace=merged.get("trace", {}),
            notify=merged.get("notify", {}),
            exchange=merged.get("exchange", {}),
        )


def default_config_dict() -> dict[str, Any]:
    """无配置文件时的默认配置：只登记常见的本地/国产 provider，密钥从环境变量读。

    没有密钥也能跑通全平台（行情走合成数据、Agent 走模板降级），
    这样交付时不会因为"还没配 key"就一片红。
    """
    return {
        "providers": {
            "deepseek": {
                "type": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "${DEEPSEEK_API_KEY}",
                "models": ["deepseek-chat", "deepseek-reasoner"],
            },
            "openai": {
                "type": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "${OPENAI_API_KEY}",
                "models": ["gpt-4o", "gpt-4o-mini"],
            },
            "anthropic": {
                "type": "anthropic",
                "base_url": "https://api.anthropic.com",
                "api_key": "${ANTHROPIC_API_KEY}",
                "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
            },
            "local": {
                "type": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "not-needed",
                "models": ["qwen2.5-coder:14b"],
            },
        },
        "routing": {
            "default": "deepseek/deepseek-chat",
            "code_gen": "deepseek/deepseek-chat",
            "reasoning": "deepseek/deepseek-reasoner",
            "report": "deepseek/deepseek-chat",
            "fallback": ["openai/gpt-4o-mini", "local/qwen2.5-coder:14b"],
        },
        "limits": {"max_tokens_per_run": 200000, "max_cost_per_day_cny": 50, "allowed_for_code": True},
        "trace": {"store_prompts": True, "redact_keys": True},
        "notify": {"channel": "console"},
        "exchange": {"primary": "binance", "simulated": True},
    }


def context_window(model: str) -> int:
    """常见模型的上下文窗口（tokens），用来画上下文用量；不认识的按 32k 保守估计。"""
    m = model.lower()
    if m.startswith("claude-"):
        old = ("haiku-4-5", "sonnet-4-5", "opus-4-5", "opus-4-1", "opus-4-0", "sonnet-4-0", "claude-3")
        return 200_000 if any(k in m for k in old) else 1_000_000
    if "gpt-5" in m or m.startswith(("o3", "o4")):
        return 400_000 if "gpt-5" in m else 200_000
    if "gpt-4.1" in m:
        return 1_000_000
    if "gpt-4o" in m:
        return 128_000
    if "deepseek" in m:
        return 128_000
    if "qwen" in m or "kimi" in m or "moonshot" in m:
        return 128_000
    return 32_000


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


class ModelRouter:
    """按角色选模型；失败按 fallback 链降级。参考 codex/cc 的 provider 抽象。"""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self._clients: dict[str, Any] = {}

    def _client(self, provider_name: str):
        if provider_name in self._clients:
            return self._clients[provider_name]
        pc = self.config.providers.get(provider_name)
        if pc is None:
            raise LLMError(f"未知 provider: {provider_name}")
        if pc.type == "anthropic":
            client = AnthropicProvider(pc.name, pc.base_url, pc.api_key)
        elif pc.type == "openai_responses":
            client = OpenAIResponsesProvider(pc.name, pc.base_url, pc.api_key)
        else:
            client = OpenAICompatibleProvider(pc.name, pc.base_url, pc.api_key)
        self._clients[provider_name] = client
        return client

    def _targets_for(self, role: str, override: str | None = None) -> list[tuple[str, str]]:
        primary = override or self.config.routing.get(role) or self.config.routing.get("default")
        targets: list[tuple[str, str]] = []
        if primary:
            provider, _, model = primary.partition("/")
            targets.append((provider, model))
        for fb in self.config.routing.get("fallback", []):
            provider, _, model = fb.partition("/")
            if (provider, model) not in targets:
                targets.append((provider, model))
        return targets

    def _has_credentials(self, provider_name: str) -> bool:
        """未配置密钥的 provider 直接跳过，让降级链立刻失败而不是逐个等超时。"""
        pc = self.config.providers.get(provider_name)
        if pc is None:
            return False
        from .providers import _resolve_env

        return bool(_resolve_env(pc.api_key))

    def chat(
        self,
        messages: list[Message],
        *,
        role: str = "default",
        model: str | None = None,
        temperature: float = 0.2,
        tools: list[Tool] | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        on_delta: Any = None,
    ) -> ChatResult:
        """model 形如 "provider/model"；给定则优先，失败仍按 fallback 链降级。

        effort：low / medium / high / max，按模型映射成原生参数（不支持的模型忽略）。
        on_delta(kind, text)：流式回调；换到下一个候选模型前会先回调 ("reset", "")，
        让界面清掉上一个模型没说完的半句话。
        """
        errors: list[str] = []
        emitted = {"any": False}

        def relay(kind: str, text: str) -> None:
            emitted["any"] = True
            on_delta(kind, text)

        for provider_name, model_name in self._targets_for(role, override=model or None):
            if not self._has_credentials(provider_name):
                errors.append(f"[{provider_name}] 未配置 API Key，已跳过")
                continue
            pc = self.config.providers.get(provider_name)
            target_effort = effort or (pc.setting(model_name).get("effort") if pc else None) or None
            for candidate in self._effort_variants(provider_name, model_name, target_effort):
                if emitted["any"] and on_delta:
                    on_delta("reset", "")
                    emitted["any"] = False
                try:
                    client = self._client(provider_name)
                    extra: dict[str, Any] = {}
                    if target_effort:
                        extra["effort"] = target_effort
                    if on_delta:
                        extra["on_delta"] = relay
                    return client.chat(
                        messages,
                        model=pc.request_model(candidate) if pc else candidate,
                        temperature=temperature,
                        tools=tools,
                        max_tokens=max_tokens,
                        **extra,
                    )
                except LLMError as e:
                    errors.append(str(e))
                    continue
        raise LLMError("所有模型均失败:\n" + "\n".join(errors))

    def _effort_variants(self, provider_name: str, model_name: str, effort: str | None) -> list[str]:
        """DeepSeek 这类靠换模型调思考深度的 provider：高努力先试 reasoner，失败再回到原模型。"""
        pc = self.config.providers.get(provider_name)
        if not effort or pc is None or pc.type == "anthropic":
            return [model_name]
        if openai_effort_mode(model_name, pc.base_url) != "switch":
            return [model_name]
        reasoner = next((m for m in pc.models if "reason" in m or m.endswith("-r1")), None)
        chat = next((m for m in pc.models if "chat" in m), None)
        if effort in ("high", "max") and reasoner and model_name != reasoner:
            return [reasoner, model_name]
        if effort == "low" and chat and model_name == reasoner:
            return [chat, model_name]
        return [model_name]

    def effort_mode(self, target: str) -> str:
        """给界面用：这个模型的思考努力程度怎么生效（native / budget / switch / none）。"""
        provider_name, _, model_name = target.partition("/")
        pc = self.config.providers.get(provider_name)
        if pc is None:
            return "none"
        model_name = pc.request_model(model_name)
        if pc.type == "anthropic":
            return anthropic_effort_mode(model_name)
        mode = openai_effort_mode(model_name, pc.base_url)
        if mode == "switch" and not any("reason" in m or m.endswith("-r1") for m in pc.models):
            return "none"
        return mode

    def context_window_for(self, target: str) -> int:
        """目录里手动设置的上下文窗口优先，否则按实际请求的模型名估算。"""
        provider_name, _, model_name = target.partition("/")
        pc = self.config.providers.get(provider_name)
        if pc is None:
            return context_window(model_name or target)
        return int(pc.setting(model_name).get("context_window") or 0) or context_window(pc.request_model(model_name))

    def current_target(self, role: str = "default") -> str:
        primary = self.config.routing.get(role) or self.config.routing.get("default") or ""
        return primary if isinstance(primary, str) else ""

    def available_models(self) -> list[dict[str, Any]]:
        """给前端模型下拉框用的清单。"""
        out: list[dict[str, Any]] = []
        for name, pc in self.config.providers.items():
            for m in pc.models:
                if m:
                    target = f"{name}/{m}"
                    st = pc.setting(m)
                    out.append(
                        {
                            "provider": name,
                            "model": m,
                            "target": target,
                            "type": pc.type,
                            "ready": self._has_credentials(name),
                            "effort": self.effort_mode(target),
                            "context_window": self.context_window_for(target),
                            "label": st.get("label") or "",
                            "request_model": pc.request_model(m),
                            "default_effort": st.get("effort") or "",
                            "inputs": st.get("inputs") or ["text"],
                        }
                    )
        return out
