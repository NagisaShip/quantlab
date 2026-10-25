"""QuantLab execution-plane adapters for the independent Agent Runtime.

The model adapter calls one selected provider exactly once. ModelHub owns
quota reservation and usage settlement; ModelRouter supplies only the scoped
provider configuration and client, without its legacy hidden retry loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from ..llm.base import Message, Tool
from ..llm.base import ToolCall as HostToolCall
from ..llm.hub import ModelHub
from ..llm.router import ModelRouter, ProviderConfig
from . import context


def tool_resource_classes(names) -> dict[str, str]:
    """Freeze capability groups into the host's AgentSpec, not kernel policy."""
    groups = {
        "local_preview_document": "class:document_render",
        "local_export_docx": "class:document_authoring",
        "local_export_docx_file": "class:document_authoring",
        "local_export_pptx": "class:document_authoring",
        "local_export_pptx_file": "class:document_authoring",
    }
    return {name: groups[name] for name in names if name in groups}


class QuantLabModelPort:
    def __init__(
        self,
        hub: ModelHub,
        router: ModelRouter,
        *,
        workspace_id: str,
        user_id: str,
        role: str = "member",
        model_role: str = "default",
        tool_schemas: list[Tool] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        model_override: str | None = None,
        effort: str | None = None,
        on_delta: Any = None,
    ) -> None:
        self.hub = hub
        self.router = router
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.role = role
        self.model_role = model_role
        self.tool_schemas = tool_schemas or []
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.model_override = model_override
        self.effort = effort
        self.on_delta = on_delta

    @staticmethod
    def _messages(items: list[dict[str, Any]]) -> list[Message]:
        out = []
        for item in items:
            calls = [
                HostToolCall(id=call["id"], name=call["name"], arguments=call["arguments"])
                for call in item.get("tool_calls", [])
            ]
            out.append(
                Message(
                    role=item["role"],
                    content=str(item.get("content") or ""),
                    name=item.get("name"),
                    tool_call_id=item.get("tool_call_id"),
                    tool_calls=calls,
                    provider_blocks=list(item.get("provider_blocks") or []),
                    reasoning=str(item.get("reasoning") or ""),
                    images=list(item.get("images") or []),
                )
            )
        return out

    def generate(self, messages: list[dict[str, Any]], *, request_id: str) -> Any:
        from agent_runtime.contracts import ModelAdmissionPending, ModelResult, ModelUnavailable, ToolCall

        with context.bind(self.workspace_id, user_id=self.user_id, role=self.role):
            try:
                cfg = self.router.config
                targets = self.router._targets_for(self.model_role, override=self.model_override, cfg=cfg)
                selected = next(
                    (
                        (pc, model)
                        for provider, model in targets
                        if (pc := cfg.providers.get(provider)) and self.router._has_credentials(provider, cfg)
                    ),
                    None,
                )
                if selected is None:
                    raise ModelUnavailable("没有配置可用的模型，Runtime 未发出请求")
                pc, model = selected
                host_messages = self._messages(messages)
                estimated_input = max(1, sum(len(item.content) for item in host_messages) // 3)
                estimated_tokens = estimated_input + self.max_tokens
                estimated_micro = self.hub.cost_micro(pc, model, estimated_input, self.max_tokens)
                run_id = request_id.rsplit(":", 1)[0]
                try:
                    self.hub.admit(
                        pc,
                        model,
                        request_id=request_id,
                        run_id=run_id,
                        step_id=request_id,
                        reserved_tokens=estimated_tokens,
                        reserved_micro=estimated_micro,
                    )
                    dispatched = self.hub.mark_dispatched(request_id)
                except Exception as exc:
                    from .errors import AppError, ErrorCode

                    if (
                        isinstance(exc, AppError)
                        and exc.code == ErrorCode.QUOTA_EXHAUSTED
                        and exc.details.get("reason") == "model_concurrency"
                    ):
                        raise ModelAdmissionPending(
                            "模型并发额度暂不可用", retry_after_ms=exc.details["retry_after_ms"]
                        ) from exc
                    raise ModelUnavailable(f"模型请求在发出前被拒绝：{exc}") from exc
                if not dispatched:
                    raise ModelUnavailable("模型请求已被派发或结算，不能再次发送")
            except (ModelUnavailable, ModelAdmissionPending):
                raise
            except Exception as exc:
                raise ModelUnavailable(f"模型准入失败：{exc}") from exc

            try:
                result = self.router._client(pc.name, pc).chat(
                    host_messages,
                    model=pc.request_model(model),
                    temperature=self.temperature,
                    tools=self.tool_schemas or None,
                    max_tokens=self.max_tokens,
                    **({"effort": self.effort} if self.effort else {}),
                    **({"on_delta": self.on_delta} if self.on_delta else {}),
                )
                usage = result.usage or None
                self.hub.settle_admission(pc, model, request_id, usage, response_confirmed=True)
            except Exception:
                self.hub.settle_admission(pc, model, request_id, None)
                raise
            admission = self.hub.admission(request_id)
            return ModelResult(
                text=result.content,
                tool_calls=[ToolCall(call.name, call.arguments) for call in result.tool_calls],
                usage_micro=admission["actual_micro"] if admission and admission["status"] == "SETTLED" else None,
                provider_request_id=request_id,
                provider=pc.name,
                model=model,
                provider_blocks=list(getattr(result, "provider_blocks", []) or []),
                thinking=getattr(result, "thinking", "") or "",
            )


class QuantLabAgentModelPort:
    """Use single-dispatch ModelHub requests for existing Agent/Team hosts.

    Custom routers without ModelHub retain their host execution contract; they
    report zero local usage because their billing authority lives outside Runtime.
    """

    def __init__(
        self,
        router: ModelRouter,
        *,
        workspace_id: str,
        user_id: str,
        role: str = "reasoning",
        context_role: str = "member",
        model: str | None = None,
        temperature: float = 0.2,
        effort: str | None = None,
        tools: list[Tool] | None = None,
        prefix_messages: list[Message] | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.router = router
        self.workspace_id = workspace_id
        self.user_id = user_id
        self.role = role
        self.context_role = context_role
        self.model = model
        self.temperature = temperature
        self.effort = effort
        self.tools = tools or []
        self.prefix_messages = prefix_messages or []
        self.max_tokens = max_tokens
        self.child_output_chars: int | None = None

    def generate(self, messages: list[dict[str, Any]], *, request_id: str) -> Any:
        from agent_runtime.contracts import ModelResult, ToolCall

        # Rebuild document inspection inputs from committed tool output. This
        # survives host restart and never repeats the document-producing tool.
        from ..local.documents import preview_images

        messages = list(messages)
        if self.child_output_chars is not None:
            for index, message in enumerate(messages):
                if message.get("role") != "user":
                    continue
                try:
                    body = json.loads(message.get("content") or "")
                except (TypeError, ValueError):
                    continue
                if not isinstance(body, dict) or not isinstance(body.get("child_results"), list):
                    continue
                results = []
                for child in body["child_results"]:
                    output = child.get("output")
                    if isinstance(output, str) and len(output) > self.child_output_chars:
                        child = {**child, "output": output[: self.child_output_chars] + "……（已截断）"}
                    results.append(child)
                messages[index] = {
                    **message,
                    "content": json.dumps({**body, "child_results": results}, ensure_ascii=False),
                }
        images = []
        for message in reversed(messages):
            if message.get("role") != "tool":
                break
            try:
                images.extend(preview_images(message.get("name", ""), json.loads(message.get("content") or "{}")))
            except (ValueError, KeyError, OSError):
                continue
        if images:
            messages.append(
                {"role": "user", "content": "检查文档页面的排版，修正问题后重新预览。", "images": images[:8]}
            )
        hub = getattr(getattr(self.router, "admission", None), "hub", None)
        if hub is not None:
            port = QuantLabModelPort(
                hub,
                self.router,
                workspace_id=self.workspace_id,
                user_id=self.user_id,
                role=self.context_role,
                model_role=self.role,
                model_override=self.model,
                temperature=self.temperature,
                effort=self.effort,
                tool_schemas=self.tools,
                max_tokens=self.max_tokens or 8192,
            )
            return port.generate(
                [*[asdict(message) for message in self.prefix_messages], *messages], request_id=request_id
            )
        host_messages = [*self.prefix_messages, *QuantLabModelPort._messages(messages)]
        with context.bind(self.workspace_id, user_id=self.user_id, role=self.context_role):
            result = self.router.chat(
                host_messages,
                role=self.role,
                model=self.model,
                temperature=self.temperature,
                tools=self.tools or None,
                max_tokens=self.max_tokens,
                effort=self.effort,
            )
        return ModelResult(
            text=result.content,
            tool_calls=[ToolCall(call.name, call.arguments) for call in result.tool_calls],
            usage_micro=0,
            provider_request_id=request_id,
            provider=result.provider,
            model=result.model,
            provider_blocks=list(getattr(result, "provider_blocks", []) or []),
            thinking=getattr(result, "thinking", "") or "",
        )


@dataclass(frozen=True)
class RuntimeToolBinding:
    execute: Callable[[dict[str, Any], str], Any]
    reconcile: Callable[[dict[str, Any], str], tuple[bool, Any] | None] | None = None


class QuantLabToolPort:
    """Only explicitly reviewed tool bindings enter the Runtime capability set."""

    def __init__(self, bindings: dict[str, RuntimeToolBinding]) -> None:
        self.bindings = dict(bindings)

    def execute(self, call: Any, *, effect_id: str) -> Any:
        binding = self.bindings.get(call.name)
        if binding is None:
            raise KeyError(call.name)
        result = binding.execute(json.loads(json.dumps(call.arguments)), effect_id)
        from ..local.runtime_artifacts import attach_artifacts

        return attach_artifacts(call.name, result, effect_id=effect_id)

    def reconcile(self, call: Any, *, effect_id: str) -> tuple[bool, Any] | None:
        binding = self.bindings.get(call.name)
        if binding is None or binding.reconcile is None:
            return None
        return binding.reconcile(json.loads(json.dumps(call.arguments)), effect_id)


class QuantLabRuntimeFacade:
    """Rebind host identity and scoped model config for each durable run."""

    def __init__(
        self,
        *,
        journal: Any,
        runs: Any,
        hub: ModelHub,
        router: ModelRouter,
        bindings: dict[str, RuntimeToolBinding] | None = None,
        worker_id: str = "quantlab",
    ) -> None:
        self.journal = journal
        self.runs = runs
        self.hub = hub
        self.router = router
        self.bindings = bindings or {}
        self.worker_id = worker_id

    def load_spec(self, data: dict[str, Any]) -> Any:
        from agent_runtime.contracts import AgentSpec, ToolSpec

        tools = tuple(ToolSpec(**item) for item in data.get("tools", ()))
        unknown = {tool.name for tool in tools} - set(self.bindings)
        if unknown:
            raise ValueError(f"unbound Runtime tools: {', '.join(sorted(unknown))}")
        return AgentSpec(**{**data, "tools": tools})

    def _engine(self, run_id: str) -> Any:
        from agent_runtime import AgentRuntime

        ticket = self.runs.get(run_id)
        if ticket is None or not ticket.get("runtime_version"):
            raise KeyError(run_id)
        model = QuantLabModelPort(
            self.hub, self.router, workspace_id=ticket["workspace_id"], user_id=ticket["created_by"]
        )
        return AgentRuntime(self.journal, model, QuantLabToolPort(self.bindings), worker_id=self.worker_id)

    def start(self, prompt: str, spec: Any, *, run_id: str, tenant_id: str) -> str:
        return self._engine(run_id).start(prompt, spec, run_id=run_id, tenant_id=tenant_id)

    def get(self, run_id: str) -> dict[str, Any] | None:
        return self._engine(run_id).get(run_id)

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self._engine(run_id).events(run_id, after)

    def signal(self, run_id: str, type_: str, data: dict[str, Any], *, signal_id: str, actor: str) -> bool:
        return self._engine(run_id).signal(run_id, type_, data, signal_id=signal_id, actor=actor)

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._engine(run_id).resume(run_id)

    def reconcile_model_usage(
        self,
        run_id: str,
        step_id: str,
        *,
        actor: str,
        usage: dict[str, Any] | None = None,
        no_charge_confirmed: bool = False,
    ) -> bool:
        """Record provider-confirmed billing first, then release Runtime's reservation.

        A retry after a crash between the two SQLite commits is safe: ModelHub's
        admission is idempotent, and Runtime only settles a reconciling step.
        """
        return reconcile_model_usage(
            self._engine(run_id),
            self.hub,
            run_id,
            step_id,
            actor=actor,
            usage=usage,
            no_charge_confirmed=no_charge_confirmed,
        )


def reconcile_model_usage(
    engine: Any,
    hub: ModelHub,
    run_id: str,
    step_id: str,
    *,
    actor: str,
    usage: dict[str, Any] | None = None,
    no_charge_confirmed: bool = False,
) -> bool:
    """Settle the single host ledger before closing the durable execution step."""
    if not actor or (usage is None and not no_charge_confirmed):
        raise ValueError("actor and confirmed usage or no-charge evidence are required")
    if usage is not None and no_charge_confirmed:
        raise ValueError("usage and no-charge confirmation are mutually exclusive")
    current = engine.get(run_id)
    step = next((item for item in current["steps"] if item["id"] == step_id), None) if current else None
    if step is None or step["kind"] != "model" or step["state"] != "RECONCILING":
        return False
    admission = hub.admission(step_id)
    if admission is None or admission["run_id"] != run_id or admission["step_id"] != step_id:
        raise ValueError("model admission does not belong to this Runtime step")
    if admission["workspace_id"] != current["tenant_id"]:
        raise ValueError("model admission tenant mismatch")
    if usage is None:
        if admission["status"] == "SETTLED":
            raise ValueError("settled usage cannot be replaced with no-charge confirmation")
        hub.release_admission(step_id)
    else:
        if admission["status"] == "RELEASED":
            raise ValueError("released admission cannot be charged")
        if not admission["pricing_json"]:
            raise ValueError("missing original pricing snapshot")
        # Pricing is read from the admission snapshot, even if this provider
        # has since been removed from the workspace's current configuration.
        provider = ProviderConfig(name=admission["provider"], type="openai_compatible", base_url="", api_key="")
        hub.settle_admission(provider, admission["model"], step_id, usage)
    confirmed = hub.admission(step_id)
    if confirmed["actual_micro"] is None:
        raise ValueError("confirmed provider usage is still missing")
    return engine.settle_model_uncertainty(run_id, step_id, usage_micro=confirmed["actual_micro"], actor=actor)
