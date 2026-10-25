"""Stable public contracts for host-provided model, tool and admission adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelResult:
    text: str = ""
    tool_call: ToolCall | None = None
    usage_micro: int | None = None
    provider_request_id: str | None = None
    provider: str | None = None
    model: str | None = None
    provider_blocks: list[dict[str, Any]] = field(default_factory=list)
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.tool_call is not None and self.tool_calls:
            raise ValueError("use tool_call or tool_calls, not both")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: str = "read_only"  # read_only | idempotent_write | external_effect
    requires_approval: bool = False
    retry_safe: bool = False
    pause_after: bool = False
    approval_ttl_ms: int = 86400000

    def __post_init__(self) -> None:
        if self.approval_ttl_ms <= 0:
            raise ValueError("approval_ttl_ms must be positive")
        if self.effect not in {"read_only", "idempotent_write", "external_effect"}:
            raise ValueError(f"unsupported effect type: {self.effect}")
        if self.effect == "external_effect" and self.retry_safe:
            raise ValueError("external_effect cannot be marked retry_safe without reconciliation")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    version: str
    tools: tuple[ToolSpec, ...] = ()
    max_steps: int = 12
    budget_limit_micro: int = 0
    model_reservation_micro: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    max_tool_calls: int | None = None
    max_children: int = 32
    max_descendants: int = 256
    max_child_depth: int = 16

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if isinstance(self.metadata, dict) and "tool_resource_classes" in self.metadata:
            classes = self.metadata["tool_resource_classes"]
            if not isinstance(classes, dict) or any(
                not isinstance(name, str) or not isinstance(resource, str) or not resource or len(resource) > 256
                for name, resource in classes.items()
            ):
                raise ValueError("tool_resource_classes must map tool names to bounded resource identities")
        model_limit = self.metadata.get("max_model_calls") if isinstance(self.metadata, dict) else None
        if model_limit is not None and (type(model_limit) is not int or model_limit < 1):
            raise ValueError("max_model_calls must be a positive integer")
        finish = self.metadata.get("finish_after_tools", []) if isinstance(self.metadata, dict) else []
        if not isinstance(finish, list) or any(name not in {tool.name for tool in self.tools} for name in finish):
            raise ValueError("finish_after_tools must name registered tools")
        if min(self.max_children, self.max_descendants, self.max_child_depth) < 0:
            raise ValueError("child task bounds cannot be negative")
        if self.max_tool_calls is not None and self.max_tool_calls < 0:
            raise ValueError("max_tool_calls cannot be negative")
        if self.budget_limit_micro < 0 or self.model_reservation_micro < 0:
            raise ValueError("budgets cannot be negative")
        if self.budget_limit_micro and not self.model_reservation_micro:
            raise ValueError("budgeted runs require a model reservation estimate")


@dataclass(frozen=True)
class ChildTask:
    prompt: str
    spec: AgentSpec
    initial_messages: list[dict[str, Any]] | None = None
    depends_on: tuple[int, ...] = ()
    dependency_policy: str = "all_success"


class ModelAdmissionPending(RuntimeError):
    """Temporary pre-dispatch backpressure; safe to retry the same logical Step."""

    def __init__(self, message: str = "model capacity is temporarily unavailable", *, retry_after_ms: int = 1000):
        if isinstance(retry_after_ms, bool) or not isinstance(retry_after_ms, int) or not 1 <= retry_after_ms <= 60000:
            raise ValueError("retry_after_ms must be an integer between 1 and 60000")
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class ModelUnavailable(RuntimeError):
    """The model step was rejected before any provider request was dispatched."""


class ModelPort(Protocol):
    def generate(self, messages: list[dict[str, Any]], *, request_id: str) -> ModelResult: ...


class ToolPort(Protocol):
    def execute(self, call: ToolCall, *, effect_id: str) -> Any: ...

    def reconcile(self, call: ToolCall, *, effect_id: str) -> tuple[bool, Any] | None: ...


class AdmissionPort(Protocol):
    def reserve(self, run_id: str, step_id: str, amount_micro: int) -> bool: ...

    def settle(self, run_id: str, step_id: str, amount_micro: int | None) -> None: ...


RUN_STATES = frozenset({"QUEUED", "RUNNING", "WAITING", "RECOVERING", "CANCELING", "COMPLETED", "FAILED", "CANCELED"})
STEP_STATES = frozenset(
    {
        "READY",
        "WAITING_ADMISSION",
        "IN_FLIGHT",
        "RECONCILING",
        "WAITING_APPROVAL",
        "WAITING_TIMER",
        "WAITING_INPUT",
        "WAITING_CHILDREN",
        "WAITING_DEPENDENCIES",
        "SUCCEEDED",
        "FAILED",
        "CANCELED",
    }
)
