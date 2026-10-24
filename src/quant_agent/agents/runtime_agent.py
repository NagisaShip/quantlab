"""Durable execution shared by generic Agents and Team role hosts."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from typing import Any

from ..platform import context
from ..platform.runtime_adapters import QuantLabAgentModelPort, QuantLabToolPort, RuntimeToolBinding
from .base import AgentRun, AgentStep
from .tool_guards import clean_protocol_residue


def run_agent(
    agent: Any, task: str, *, journal: Any, retrieved: list | None = None, run_id: str | None = None
) -> AgentRun:
    from agent_runtime import AgentRuntime
    from agent_runtime.contracts import AgentSpec, ToolSpec

    existing_id = run_id
    run_id = run_id or (f"team-{uuid.uuid4().hex}" if agent.role == "reasoning" else f"agent-{uuid.uuid4().hex}")
    started = int(time.time() * 1000)
    tools = list(agent.tools.values())

    def bind(tool: Any) -> RuntimeToolBinding:
        def execute(arguments: dict[str, Any], effect_id: str) -> Any:
            if tool.handler is None:
                raise ValueError(f"unbound tool: {tool.name}")
            # An exception after an external effect must remain uncertain.
            result = tool.handler(**arguments)
            return json.loads(json.dumps(result, ensure_ascii=False, default=str))

        return RuntimeToolBinding(execute=execute)

    model = QuantLabAgentModelPort(
        agent.router,
        workspace_id=context.current_workspace(),
        user_id=context.current().user_id or "system",
        context_role=context.current().role or "member",
        role=agent.role,
        model=agent.model,
        temperature=agent.temperature,
        effort=agent.effort,
        tools=tools,
    )
    runtime = AgentRuntime(journal, model, QuantLabToolPort({tool.name: bind(tool) for tool in tools}))
    if existing_id:
        saved = journal.run(run_id)
        if saved is None or saved["tenant_id"] != context.current_workspace():
            raise ValueError("Runtime run is not owned by this workspace")
        spec = saved["spec"]
        if spec["name"] != agent.name or spec.get("metadata", {}).get("role") != agent.role:
            raise ValueError("Runtime run belongs to another agent")
        metadata = spec.get("metadata", {})
        if metadata.get("user_id") != (context.current().user_id or "system"):
            raise ValueError("Runtime run belongs to another user")
        if metadata.get("model") != agent.model or metadata.get("effort") != agent.effort:
            raise ValueError("Runtime model binding has changed")
        if {item["name"] for item in spec["tools"]} != set(agent.tools):
            raise ValueError("Runtime tool bindings have changed")
        started = saved["created_at"]
    else:
        initial_messages = [asdict(message) for message in agent.context.build(task, retrieved=retrieved)]
        runtime.start(
            task,
            AgentSpec(
                name=agent.name,
                version="1",
                tools=tuple(ToolSpec(tool.name, effect="external_effect") for tool in tools),
                max_steps=agent.max_steps,
                metadata={
                    "host": "generic_agent",
                    "role": agent.role,
                    "user_id": context.current().user_id or "system",
                    "model": agent.model,
                    "effort": agent.effort,
                    "context_hash": agent.context.snapshot_hash(),
                },
            ),
            run_id=run_id,
            tenant_id=context.current_workspace(),
            initial_messages=initial_messages,
        )
    state = runtime.resume(run_id)
    run = AgentRun(
        run_id=run_id,
        role=agent.role,
        final=clean_protocol_residue(state.get("output") or state.get("error") or f"Runtime 状态：{state['state']}"),
        status={"COMPLETED": "done", "WAITING": "blocked"}.get(state["state"], "failed"),
        context_hash=journal.run(run_id)["spec"]["metadata"]["context_hash"],
        model=agent.model or "Runtime",
        started_at=started,
        ended_at=int(time.time() * 1000),
    )
    for index, step in enumerate(journal.steps(run_id)):
        result = step.get("result")
        if result is None:
            continue
        if step["kind"] == "model":
            run.model = f"{result.get('provider') or ''}/{result.get('model') or ''}".strip("/") or run.model
            run.steps.append(AgentStep(index=index, kind="llm", content=result.get("text", ""), model=run.model))
        else:
            payload = step["payload"]
            run.steps.append(
                AgentStep(
                    index=index,
                    kind="tool",
                    tool_name=payload["name"],
                    tool_args=payload["arguments"],
                    tool_result=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False),
                )
            )
    return run
