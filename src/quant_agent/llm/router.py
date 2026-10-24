"""配置加载 + 模型路由 + 降级链。

读取 agent.config.toml（项目级）与 ~/.quant_agent/config.toml（用户级），
用户级覆盖项目级。负责按角色挑选 provider，并在失败/超额时降级。
"""

from __future__ import annotations

import random
import time
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
    # Web 平台的服务商来自工作区（见 llm/hub.py）：scope 为 workspace / personal，
    # 价格币种与汇率、限额规则用于计量与超额判定；配置文件里的 provider 保持默认值。
    scope: str = "file"
    owner_id: str = ""
    group_id: str = ""
    currency: str = "CNY"
    fx_rate: str = "1"
    limits: list[dict[str, Any]] = field(default_factory=list)

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
        # 不内置任何服务商：模型由用户自行添加，或由工作区管理员共享（见 llm/hub.py）
        "providers": {},
        "routing": {"fallback": []},
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


#: 同一个模型调用失败后默认自动重试的次数
DEFAULT_RETRIES = 2


class ModelRouter:
    """按角色选模型；失败按 fallback 链降级。参考 codex/cc 的 provider 抽象。"""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._clients: dict[str, Any] = {}
        # Web 平台注入：按请求上下文（工作区 + 成员）解析出可用的服务商与路由
        self.resolver: Any = None
        # 调用前的限额检查（返回原因则跳过该服务商）与调用后的用量记账
        self.guard: Any = None
        self.on_usage: Any = None
        self.admission: Any = None
        self.sleep = time.sleep  # 重试等待（测试里替换掉，不真的睡）

    @property
    def config(self) -> AgentConfig:
        return self.resolver(self._config) if self.resolver else self._config

    @config.setter
    def config(self, value: AgentConfig) -> None:
        self._config = value

    def _client(self, provider_name: str, pc: ProviderConfig | None = None):
        pc = pc or self.config.providers.get(provider_name)
        if pc is None:
            raise LLMError(f"未知 provider: {provider_name}")
        # 不同工作区可能有同名服务商：按连接信息区分缓存
        cache_key = provider_name
        if self.resolver is not None:
            import hashlib

            fp = hashlib.sha256(f"{pc.type}|{pc.base_url}|{pc.api_key}".encode()).hexdigest()[:16]
            cache_key = f"{provider_name}|{fp}"
        if cache_key in self._clients:
            return self._clients[cache_key]
        if pc.type == "anthropic":
            client = AnthropicProvider(pc.name, pc.base_url, pc.api_key)
        elif pc.type == "openai_responses":
            client = OpenAIResponsesProvider(pc.name, pc.base_url, pc.api_key)
        else:
            client = OpenAICompatibleProvider(pc.name, pc.base_url, pc.api_key)
        self._clients[cache_key] = client
        return client

    def _targets_for(
        self, role: str, override: str | None = None, cfg: AgentConfig | None = None
    ) -> list[tuple[str, str]]:
        cfg = cfg or self.config
        primary = override or cfg.routing.get(role) or cfg.routing.get("default")
        targets: list[tuple[str, str]] = []
        if primary:
            provider, _, model = primary.partition("/")
            targets.append((provider, model))
        for fb in cfg.routing.get("fallback", []):
            provider, _, model = fb.partition("/")
            if (provider, model) not in targets:
                targets.append((provider, model))
        return targets

    def _has_credentials(self, provider_name: str, cfg: AgentConfig | None = None) -> bool:
        """未配置密钥的 provider 直接跳过，让降级链立刻失败而不是逐个等超时。"""
        pc = (cfg or self.config).providers.get(provider_name)
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
        on_retry: Any = None,
    ) -> ChatResult:
        """model 形如 "provider/model"；给定则优先，失败仍按 fallback 链降级。

        effort：low / medium / high / max，按模型映射成原生参数（不支持的模型忽略）。
        on_delta(kind, text)：流式回调；换到下一个候选模型前会先回调 ("reset", "")，
        让界面清掉上一个模型没说完的半句话。
        on_retry(info)：同一个模型遇到临时性错误（限流、5xx、超时、断流）准备重试前回调，
        info 含 provider、model、attempt、retries、delay（秒）、status、error。
        """
        errors: list[str] = []
        emitted = {"any": False}

        def relay(kind: str, text: str) -> None:
            emitted["any"] = True
            on_delta(kind, text)

        cfg = self.config
        retries = self.retries_for(cfg)
        for provider_name, model_name in self._targets_for(role, override=model or None, cfg=cfg):
            pc = cfg.providers.get(provider_name)
            if pc is None:
                errors.append(f"[{provider_name}] 没有这个模型服务商（或你没有使用权限），已跳过")
                continue
            if not self._has_credentials(provider_name, cfg):
                errors.append(f"[{provider_name}] 未配置 API Key，已跳过")
                continue
            blocked = self.guard(pc, model_name) if self.guard else None
            if blocked:
                errors.append(f"[{provider_name}] {blocked}，已跳过")
                continue
            target_effort = effort or (pc.setting(model_name).get("effort") if pc else None) or None
            for candidate in self._effort_variants(provider_name, model_name, target_effort, pc):
                for attempt in range(retries + 1):
                    if emitted["any"] and on_delta:
                        on_delta("reset", "")
                        emitted["any"] = False
                    try:
                        client = self._client(provider_name, pc)
                        admission_id = (
                            self.admission.reserve(pc, candidate, messages, max_tokens) if self.admission else None
                        )
                    except LLMError as e:
                        errors.append(str(e))
                        break
                    try:
                        extra: dict[str, Any] = {}
                        if target_effort:
                            extra["effort"] = target_effort
                        if on_delta:
                            extra["on_delta"] = relay
                        result = client.chat(
                            messages,
                            model=pc.request_model(candidate),
                            temperature=temperature,
                            tools=tools,
                            max_tokens=max_tokens,
                            **extra,
                        )
                    except LLMError as e:
                        if admission_id is not None:
                            self.admission.unknown(pc, candidate, admission_id)
                        delay = self._retry_delay(e, attempt) if attempt < retries else None
                        if delay is None:  # 不可重试、次数用完或要等太久：换下一个候选模型
                            errors.append(str(e) + (f"（已重试 {attempt} 次）" if attempt else ""))
                            break
                        if on_retry:
                            # 回调可以抛出异常（例如研究者按了停止）来放弃重试
                            on_retry(
                                {
                                    "provider": provider_name,
                                    "model": candidate,
                                    "attempt": attempt + 1,
                                    "retries": retries,
                                    "delay": round(delay, 1),
                                    "status": e.status,
                                    "error": str(e)[:200],
                                }
                            )
                        self.sleep(delay)
                    except BaseException:
                        if admission_id is not None:
                            self.admission.unknown(pc, candidate, admission_id)
                        raise
                    else:
                        if admission_id is not None:
                            self.admission.settle(pc, candidate, admission_id, result.usage)
                        if self.on_usage:
                            try:
                                self.on_usage(pc, candidate, result.usage)
                            except Exception:
                                pass
                        return result
        if not errors:
            # 一个候选都没有（服务商 / 路由没配）：不要抛「所有模型均失败:」后面跟空行这种谜语
            raise LLMError("没有可用的模型：请在 设置 → 模型 里添加服务商，并在 路由 里选择默认模型")
        raise LLMError("所有模型均失败:\n" + "\n".join(errors))

    #: 单次重试最长等待；服务端要求等更久（Retry-After）时不等，直接换下一个模型
    RETRY_MAX_WAIT = 30.0

    def _retry_delay(self, e: LLMError, attempt: int) -> float | None:
        """第 attempt 次失败后等多久再试：指数退避 1s、2s、4s…（上限 20s）加 ±20% 抖动；
        服务端给了 Retry-After 就按它来。不可重试的错误返回 None。"""
        if not getattr(e, "retryable", False):
            return None
        if e.retry_after is not None:
            return e.retry_after if e.retry_after <= self.RETRY_MAX_WAIT else None
        base = min(20.0, 1.0 * (2**attempt))
        return base * random.uniform(0.8, 1.2)

    def retries_for(self, cfg: AgentConfig | None = None) -> int:
        """同一个模型失败后的自动重试次数（不含第一次），路由设置里可改，默认 2。"""
        raw = (cfg or self.config).routing.get("retries")
        try:
            return max(0, min(5, int(raw)))
        except (TypeError, ValueError):
            return DEFAULT_RETRIES

    def _effort_variants(
        self, provider_name: str, model_name: str, effort: str | None, pc: ProviderConfig | None = None
    ) -> list[str]:
        """DeepSeek 这类靠换模型调思考深度的 provider：高努力先试 reasoner，失败再回到原模型。"""
        pc = pc or self.config.providers.get(provider_name)
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
        """给界面用：这个模型的思考努力程度怎么生效（native / budget / switch / auto / none）。
        auto：认不出的模型先带 reasoning_effort 试，服务商不接受就自动去掉——界面上照样可以调。"""
        provider_name, _, model_name = target.partition("/")
        pc = self.config.providers.get(provider_name)
        if pc is None:
            return "none"
        model_name = pc.request_model(model_name)
        if pc.type == "anthropic":
            return anthropic_effort_mode(model_name)
        mode = openai_effort_mode(model_name, pc.base_url)
        if mode == "switch" and not any("reason" in m or m.endswith("-r1") for m in pc.models):
            return "auto"
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

    def default_target(self) -> str:
        """默认模型：当前模型清单里顺序第一个（优先已配置密钥的），没有就返回清单第一个。

        AI 研究室等“没显式选模型”的入口用它作为默认，而不是跟随会被改动的全局路由；
        这样设置页里的模型顺序就是默认顺序，行为可预期。完全没有模型时返回空串。
        """
        cfg = self.config
        fallback = ""
        for name, pc in cfg.providers.items():
            for model in pc.models:
                if not model:
                    continue
                target = f"{name}/{model}"
                if not fallback:
                    fallback = target
                if self._has_credentials(name, cfg):
                    return target
        return fallback

    def available_models(self) -> list[dict[str, Any]]:
        """给前端模型下拉框用的清单。"""
        out: list[dict[str, Any]] = []
        cfg = self.config
        for name, pc in cfg.providers.items():
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
                            "ready": self._has_credentials(name, cfg),
                            "scope": pc.scope,
                            "effort": self.effort_mode(target),
                            "context_window": self.context_window_for(target),
                            "label": st.get("label") or "",
                            "request_model": pc.request_model(m),
                            "default_effort": st.get("effort") or "",
                            "inputs": st.get("inputs") or ["text"],
                        }
                    )
        return out
