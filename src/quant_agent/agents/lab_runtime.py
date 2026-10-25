"""Durable execution of the research lab's interactive model/tool conversation."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, replace
from typing import Any

from ..platform import context
from ..platform.runtime_adapters import QuantLabModelPort, QuantLabToolPort, RuntimeToolBinding, tool_resource_classes


def _engine(lab: Any, pid: str, tools: dict[str, Any]) -> tuple[Any, Any]:
    from agent_runtime import AgentRuntime
    from agent_runtime.contracts import ModelResult, ToolCall

    from .strategy_lab import (
        TOOL_PHASE,
        StopRequested,
        _shrink_context,
        _Streamer,
        _trim_tool_outputs,
        clean_protocol_residue,
        extract_code,
        is_context_length_error,
        stop_event,
    )
    from .tool_guards import approval_input_error

    project = lab.store.get(pid) or {}

    class Model:
        def __init__(self):
            self.streams: dict[str, Any] = {}

        def generate(self, messages, *, request_id):
            lab._check_stop(pid)
            stream = _Streamer(lab, pid, metadata={"runtime_step": request_id})
            self.streams[request_id] = stream
            host = QuantLabModelPort._messages(messages)
            # Screenshots are reconstructed from durable tool results after a restart.
            from ..llm.base import Message
            from ..local.documents import preview_images

            images = []
            for message in host:
                if message.role == "tool":
                    try:
                        images.extend(preview_images(message.name or "", json.loads(message.content)))
                    except (ValueError, TypeError, KeyError, OSError):
                        pass
            if images:
                host.append(
                    Message(
                        role="user", content="请检查这些真实页面截图的排版，发现问题先修正再复查。", images=images[-8:]
                    )
                )
            _trim_tool_outputs(host)
            completed = [message for message in host if message.role == "tool"]
            who = (project.get("model") or lab.model_target(project) or "默认模型").split("/")[-1]
            if completed:
                names = "、".join(TOOL_PHASE.get(message.name, message.name or "工具") for message in completed[-3:])
                lab._phase(pid, f"根据「{names}」的结果继续推理", f"本回合已调用 {len(completed)} 个工具")
            else:
                lab._phase(pid, "阅读对话与上下文，规划怎么回答", f"{who} · 上下文 {len(host)} 段")
            router = lab._router_for(project)
            hub = getattr(getattr(lab.router, "admission", None), "hub", None)
            if hub is not None:
                port = QuantLabModelPort(
                    hub,
                    lab.router,
                    workspace_id=context.current_workspace(),
                    user_id=context.current().user_id or "system",
                    role=context.current().role,
                    model_role="code_gen",
                    model_override=project.get("model") or lab.model_target(project) or None,
                    effort=project.get("effort") or None,
                    tool_schemas=list(tools.values()),
                    max_tokens=8192,
                    on_delta=stream,
                )
                durable = lab._interruptible(
                    pid, lambda: port.generate([asdict(message) for message in host], request_id=request_id), stream
                )
            else:
                # Host-supplied routers without ModelHub (CLI/custom integrations)
                # retain their own execution-plane contract.
                for attempt in range(2):
                    try:
                        result = lab._interruptible(
                            pid,
                            lambda: router.chat(
                                host,
                                role="code_gen",
                                temperature=0.2,
                                tools=list(tools.values()),
                                max_tokens=8192,
                                on_delta=stream,
                                on_retry=lambda info: lab._on_model_retry(pid, info),
                            ),
                            stream,
                        )
                        break
                    except Exception as exc:
                        if isinstance(exc, StopRequested):
                            stop_event(pid).set()
                            cancel_chat(lab, pid)
                            raise
                        if attempt or not is_context_length_error(str(exc)):
                            raise
                        _shrink_context(host)
                durable = ModelResult(
                    text=result.content,
                    tool_calls=[ToolCall(call.name, call.arguments) for call in result.tool_calls],
                    usage_micro=0,
                    provider=result.provider,
                    model=result.model,
                    provider_blocks=list(getattr(result, "provider_blocks", []) or []),
                    thinking=getattr(result, "thinking", "") or "",
                )
            content = clean_protocol_residue(durable.text or "")
            calls = durable.tool_calls or ([durable.tool_call] if durable.tool_call else [])
            code = extract_code(content) if not calls else ""
            if code and "def initialize" in code:
                # Implicit code delivery receives its own durable effect and approval.
                calls = [ToolCall("write_strategy", {"code": code, "note": "模型在回答中给出的代码"})]
            return replace(durable, text=content, tool_call=None, tool_calls=calls)

    def binding(name, tool):
        def execute(arguments, effect_id):
            lab._check_stop(pid)
            if reason := approval_input_error(name, arguments):
                return {"error": reason}
            try:
                output = tool.handler(**arguments)
            except StopRequested:
                raise
            # Other exceptions remain uncertain: the handler may already have
            # produced an external effect. Runtime must pause rather than replay it.
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except ValueError:
                    pass
            return json.loads(json.dumps(output, ensure_ascii=False, default=str))

        return RuntimeToolBinding(execute=execute)

    model = Model()
    return AgentRuntime(
        lab.tools.runtime_journal, model, QuantLabToolPort({name: binding(name, tool) for name, tool in tools.items()})
    ), model


def cancel_chat(lab: Any, pid: str) -> None:
    from agent_runtime import AgentRuntime

    run_id = (lab.store.get(pid) or {}).get("runtime_chat_id")
    if run_id and lab.tools.runtime_journal:
        runtime = AgentRuntime(lab.tools.runtime_journal, None, None)
        current = runtime.get(run_id)
        if current and (
            current["tenant_id"] != context.current_workspace() or current["spec"]["metadata"].get("project_id") != pid
        ):
            raise ValueError("Runtime conversation scope mismatch")
        if current and current["state"] not in {"COMPLETED", "FAILED", "CANCELED", "CANCELING"}:
            runtime.signal(
                run_id, "cancel", {}, signal_id=f"lab-cancel:{run_id}", actor=context.current().user_id or "system"
            )


def prepare_history_change(lab: Any, pid: str, *, deleting: bool = False) -> None:
    """Rewind/delete must not detach a still-uncertain external operation."""
    from agent_runtime import AgentRuntime

    project = lab.store.get(pid) or {}
    runtime = AgentRuntime(lab.tools.runtime_journal, None, None)
    pending = list(project.get("runtime_pending_ids", []))
    run_ids = list(dict.fromkeys([project.get("runtime_chat_id"), *(pending if deleting else [])]))
    for run_id in filter(None, run_ids):
        current = runtime.get(run_id)
        if current is None:
            continue
        if current["tenant_id"] != context.current_workspace() or current["spec"]["metadata"].get("project_id") != pid:
            raise ValueError("Runtime conversation scope mismatch")
        if current["state"] not in {"COMPLETED", "FAILED", "CANCELED", "CANCELING"}:
            runtime.signal(
                run_id, "cancel", {}, signal_id=f"lab-cancel:{run_id}", actor=context.current().user_id or "system"
            )
            current = runtime.get(run_id)
        if current["state"] == "CANCELING":
            if deleting or any(step["kind"] == "tool" and step["state"] == "RECONCILING" for step in current["steps"]):
                raise ValueError("该对话仍有待核对的请求，请先核对后再删除或重试历史消息")
            pending = list(dict.fromkeys([*pending, run_id]))
    if not deleting:
        lab.store.patch(pid, runtime_chat_id=None, runtime_pending_ids=pending)


def run_chat(
    lab: Any,
    pid: str,
    *,
    messages=None,
    tools=None,
    mode=None,
    approval=None,
    project_only=False,
    run_id: str | None = None,
) -> None:
    from agent_runtime.contracts import AgentSpec, ToolSpec

    from .strategy_lab import APPROVE, StopRequested, split_suggestions, split_title, tool_call_limit

    project = lab.store.get(pid) or {}
    mode = mode or project.get("permission") or "auto"
    tools = tools if tools is not None else lab._all_tools(pid)
    run_id = run_id or project.get("runtime_chat_id")
    saved = lab.tools.runtime_journal.run(run_id) if run_id else None
    if saved and saved["state"] not in {"COMPLETED", "FAILED", "CANCELED"}:
        allowed = {tool["name"] for tool in saved["spec"]["tools"]}
        tools = {name: tool for name, tool in tools.items() if name in allowed}
        mode = saved["spec"]["metadata"].get("mode", mode)
    runtime, model = _engine(lab, pid, tools)
    current = runtime.get(run_id) if run_id else None
    if current and current["tenant_id"] != context.current_workspace():
        raise ValueError("Runtime conversation tenant mismatch")
    if current and current["spec"]["metadata"].get("project_id") != pid:
        raise ValueError("Runtime conversation project mismatch")
    actor = context.current().user_id or "system"
    if approval and approval["run_id"] != run_id:
        raise ValueError("approval belongs to a different conversation turn")
    if current is None or (messages is not None and current["state"] in {"COMPLETED", "FAILED", "CANCELED"}):
        if messages is None:
            raise ValueError("a new conversation turn requires initial messages")
        run_id = f"lab-{uuid.uuid4().hex}"
        specs = tuple(
            ToolSpec(
                name,
                effect="external_effect",
                requires_approval=bool(lab._approval_category(project, mode, name)),
                pause_after=name == "ask_user",
            )
            for name in tools
        )
        resource_classes = tool_resource_classes(tools)
        runtime.start(
            "",
            AgentSpec(
                "research-chat",
                "1",
                tools=specs,
                max_steps=tool_call_limit(project) * 2 + 2,
                max_tool_calls=tool_call_limit(project),
                metadata={
                    "project_id": pid,
                    "mode": mode,
                    "actor": actor,
                    **({"tool_resource_classes": resource_classes} if resource_classes else {}),
                },
            ),
            run_id=run_id,
            tenant_id=context.current_workspace(),
            initial_messages=[asdict(message) for message in messages],
        )
        latest_user = max((item["seq"] for item in lab.store.messages(pid) if item["role"] == "user"), default=0)
        lab.store.patch(pid, runtime_chat_id=run_id, runtime_input_after_seq=latest_user)
        current = runtime.get(run_id)
    if approval:
        runtime.signal(
            run_id,
            "approval",
            {
                "step_id": approval["step_id"],
                "arguments_sha256": approval["arguments_sha256"],
                "approved": approval["choice"] == APPROVE,
            },
            signal_id=f"lab-approval:{run_id}:{approval['step_id']}:{approval.get('request_seq', 0)}",
            actor=actor,
        )
    elif not project_only and (
        waiting := next((step for step in current["steps"] if step["state"] == "WAITING_INPUT"), None)
    ):
        user = next((item for item in reversed(lab.store.messages(pid)) if item["role"] == "user"), None)
        if user is not None and user["seq"] > project.get("runtime_input_after_seq", 0):
            images = []
            for reference in (user.get("data") or {}).get("images", []):
                blob = lab.store.get_image(pid, reference["id"]) if isinstance(reference, dict) else None
                if blob:
                    images.append(blob["url"])
                elif isinstance(reference, str):
                    images.append(reference)
            runtime.signal(
                run_id,
                "input",
                {
                    "step_id": waiting["id"],
                    "content": user["content"],
                    "images": images,
                },
                signal_id=f"lab-input:{run_id}:{user['seq']}",
                actor=actor,
            )
            lab.store.patch(pid, runtime_input_after_seq=user["seq"])
    try:
        while True:
            progressed = False
            if not project_only:
                lab._check_stop(pid)
                progressed = runtime.tick(run_id)
                lab._check_stop(pid)
            current = runtime.get(run_id)
            for step in current["steps"]:
                if (
                    step["kind"] != "model"
                    or step["state"] not in {"SUCCEEDED", "RECONCILING"}
                    or not step.get("result")
                ):
                    continue
                existing = next(
                    (
                        item
                        for item in lab.store.messages(pid)
                        if (item.get("data") or {}).get("runtime_step") == step["id"]
                    ),
                    None,
                )
                if existing and not (existing.get("data") or {}).get("streaming"):
                    continue
                result = step["result"]
                content, suggestions = split_suggestions(result["text"])
                content, title = split_title(content)
                if title and lab._needs_title(lab.store.get(pid) or {}) and len(title) >= 2:
                    lab.store.patch(pid, title=title, title_auto=False)
                stream = model.streams.get(step["id"])
                if stream:
                    stream.metadata["suggestions"] = suggestions
                    stream.finish(
                        content,
                        model=f"{result.get('provider')}/{result.get('model')}",
                        thinking=result.get("thinking", ""),
                    )
                elif existing:
                    lab.store.update(
                        pid,
                        existing["seq"],
                        content=content,
                        data={"streaming": False, "suggestions": suggestions, "thinking": result.get("thinking", "")},
                    )
                elif content.strip() or result.get("thinking"):
                    lab._post(
                        pid,
                        role="assistant",
                        kind="text",
                        content=content,
                        mode="llm",
                        model=f"{result.get('provider')}/{result.get('model')}",
                        data={
                            "runtime_step": step["id"],
                            "suggestions": suggestions,
                            "thinking": result.get("thinking", ""),
                        },
                    )
            if not progressed:
                break
    except StopRequested:
        cancel_chat(lab, pid)
        raise
    current = runtime.get(run_id)
    waits = [step for step in current["steps"] if step["state"] == "WAITING_ADMISSION"]
    lab.store.patch(
        pid,
        runtime_wait_reason=("tool_capacity" if any(step["kind"] == "tool" for step in waits) else "admission")
        if waits
        else "",
    )
    runtime.journal.checkpoint(run_id)
    for step in current["steps"]:
        if (
            step["state"] in {"RECONCILING", "WAITING_ADMISSION"}
            and not step.get("result")
            and step["id"] in model.streams
        ):
            model.streams[step["id"]].abort()
    pending = next((step for step in current["steps"] if step["state"] == "WAITING_APPROVAL"), None)
    if pending:
        import hashlib

        from agent_runtime.journal import dump

        already = any(
            (item.get("data") or {}).get("runtime_approval", {}).get("step_id") == pending["id"]
            and (item.get("data") or {}).get("answered") is None
            for item in lab.store.messages(pid)
        )
        if not already:
            call = pending["payload"]
            lab._ask_approval(
                pid,
                [
                    {
                        "tool": call["name"],
                        "args": call["arguments"],
                        "label": lab._describe(call["name"], call["arguments"]),
                        "category": lab._approval_category(project, mode, call["name"]),
                    }
                ],
            )
            lab.store.update(
                pid,
                lab.store.messages(pid)[-1]["seq"],
                data={
                    "runtime_approval": {
                        "run_id": run_id,
                        "step_id": pending["id"],
                        "request_seq": max(
                            event["seq"]
                            for event in runtime.events(run_id)
                            if event["type"] == "approval.required" and event["data"].get("step_id") == pending["id"]
                        ),
                        "arguments_sha256": hashlib.sha256(dump(call).encode("utf-8")).hexdigest(),
                    }
                },
            )
    elif current["state"] in {"WAITING", "CANCELING"}:
        lab.store.patch(pid, status="awaiting_user")
        if any(step["state"] == "WAITING_INPUT" for step in current["steps"]):
            latest_user = max((item["seq"] for item in lab.store.messages(pid) if item["role"] == "user"), default=0)
            lab.store.patch(pid, runtime_input_after_seq=latest_user)
        if any(step["state"] == "RECONCILING" for step in current["steps"]):
            if not any(
                (item.get("data") or {}).get("runtime_run_id") == run_id
                and (item.get("data") or {}).get("runtime_state") == "WAITING"
                for item in lab.store.messages(pid)
            ):
                lab._post(
                    pid,
                    role="event",
                    kind="text",
                    content="上一项请求的结果尚未确认，已暂停，核对后才能继续。",
                    data={"runtime_run_id": run_id, "runtime_state": "WAITING"},
                )
    elif current["state"] == "FAILED":
        if current["steps"] and current["steps"][-1]["kind"] == "model" and current["steps"][-1]["state"] == "FAILED":
            from ..llm.base import LLMError

            raise LLMError(current.get("error") or "模型请求在发出前失败")
        lab._post(
            pid,
            role="event",
            kind="error",
            content="已达到本轮工具调用上限，请开始新的对话回合。"
            if current.get("error") == "max_tool_calls exceeded"
            else (current.get("error") or "本轮执行失败"),
            data={"runtime_run_id": run_id},
        )
    elif current["state"] == "COMPLETED" and mode == "plan":
        from .strategy_lab import PLAN_EDIT, PLAN_GO

        if not any((item.get("data") or {}).get("runtime_plan_run_id") == run_id for item in lab.store.messages(pid)):
            lab._ask(pid, "按这个计划执行吗？也可以写下要改的地方。", [PLAN_GO, PLAN_EDIT], decision=True)
            lab.store.update(
                pid, lab.store.messages(pid)[-1]["seq"], data={"plan": True, "runtime_plan_run_id": run_id}
            )
    if current["state"] in {"COMPLETED", "CANCELED"}:
        for item in lab.store.messages(pid):
            data = item.get("data") or {}
            if data.get("runtime_run_id") == run_id and (
                data.get("runtime_state") == "WAITING"
                or item["content"] == "上一项请求的结果尚未确认，已暂停，核对后才能继续。"
            ):
                lab.store.update(
                    pid,
                    item["seq"],
                    kind="text",
                    content="上一项请求已核对。",
                    data={"runtime_state": current["state"]},
                )
