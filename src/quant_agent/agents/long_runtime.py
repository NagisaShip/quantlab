"""Durable business-key execution and conservative notification delivery."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from ..notify.channels import DISCLAIMER, Notification
from ..platform import context
from ..platform.runtime_adapters import QuantLabAgentModelPort, QuantLabToolPort, RuntimeToolBinding
from .runtime_agent import prepare_agent

DELIVERY = "deliver_notification"
_JOB = ContextVar("long_running_delivery_job", default=None)


@contextmanager
def scheduled_context(ctl):
    token = _JOB.set(ctl)
    try:
        yield
    finally:
        _JOB.reset(token)


def run_once(owner, task, run_key, title, fallback, *, dry_run=False):
    from agent_runtime import AgentRuntime, ModelResult, ModelUnavailable, ToolCall, ToolSpec
    from agent_runtime.journal import JournalIntegrityError

    from .long_running import AgentRunRecord

    journal = owner.agent.runtime_journal or getattr(owner.router, "runtime_journal", None)
    identity = {
        "tenant": context.current_workspace(),
        "user": context.current().user_id or "system",
        "agent": owner.agent_id,
        "key": run_key,
        "channel": owner.channel,
        "preview": dry_run,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    run_id = "long-" + digest
    saved = journal.run(run_id)
    if saved is None:
        prepared = prepare_agent(owner.agent, task)
        metadata = {
            **prepared.spec.metadata,
            "host": "long_running",
            "business_identity": identity,
            "input_digest": task,
            "notification_title": title,
            "fallback": fallback,
        }
        metadata["max_model_calls"] += 2  # bounded regeneration after explicit usage reconciliation
        metadata["finish_after_tools"] = [DELIVERY]
        spec = replace(
            prepared.spec,
            tools=(*prepared.spec.tools, ToolSpec(DELIVERY, effect="external_effect")),
            max_steps=prepared.spec.max_steps + 3,
            max_tool_calls=prepared.spec.max_tool_calls + 1,
            metadata=metadata,
        )
        try:
            AgentRuntime(journal, None, None).start(
                task, spec, run_id=run_id, tenant_id=identity["tenant"], initial_messages=prepared.initial_messages
            )
        except Exception:
            # A competing process may have created the same business operation.
            if journal.run(run_id) is None:
                raise
        saved = journal.run(run_id)
    metadata = saved["spec"]["metadata"]
    if saved["tenant_id"] != identity["tenant"] or metadata.get("business_identity") != identity:
        raise ValueError("scheduled Runtime identity mismatch")
    delegate = QuantLabAgentModelPort(
        owner.router,
        workspace_id=identity["tenant"],
        user_id=identity["user"],
        context_role=context.current().role or "member",
        role=metadata["role"],
        model=metadata["model"],
        temperature=metadata["temperature"],
        effort=metadata.get("effort"),
        tools=list(owner.agent.tools.values()),
    )

    class Model:
        def generate(self, messages, *, request_id):
            if messages[-1]["role"] == "tool" and messages[-1].get("name") == DELIVERY:
                return ModelResult(text=messages[-1]["content"], usage_micro=0)
            try:
                result = delegate.generate(messages, request_id=request_id)
                source = "llm"
            except ModelUnavailable:
                result = ModelResult(text=metadata["fallback"], usage_micro=0)
                source = "template"
            if result.tool_calls or result.tool_call:
                return result
            body = result.text.strip()
            if DISCLAIMER not in body:
                body += "\n\n" + DISCLAIMER
            return replace(result, tool_calls=[], tool_call=ToolCall(DELIVERY, {"body": body, "source": source}))

    def deliver(arguments, effect_id):
        payload = {"body": arguments["body"], "source": arguments["source"], "effect_id": effect_id, "preview": dry_run}
        if dry_run:
            return {**payload, "sent": False}
        notifier = owner.hub.notifiers.get(identity["channel"])
        if notifier is None:
            raise RuntimeError("通知通道未配置，未发送；配置后须确认本次效果不存在再继续")
        # Do not enter NotificationHub's blind retry queue. A missing acknowledgement
        # may mean the external service already accepted this stable logical effect.
        if not notifier.send(
            Notification(title=metadata["notification_title"], body=arguments["body"], channel=identity["channel"])
        ):
            raise OSError("通知发送结果未获确认，请核对后继续，禁止自动重发")
        return {**payload, "sent": True}

    bindings = {DELIVERY: RuntimeToolBinding(deliver)}
    for name, tool in owner.agent.tools.items():

        def execute(arguments, effect_id, bound=tool):
            if bound.handler is None:
                raise ValueError("unbound scheduled capability")
            return bound.handler(**arguments)

        bindings[name] = RuntimeToolBinding(execute)
    expected = {t["name"] for t in saved["spec"]["tools"]}
    if expected != set(bindings):
        raise ValueError("scheduled capabilities changed; restore original bindings before resuming")
    ctl = _JOB.get()
    if ctl is not None:
        ctl.checkpoint({**(ctl.checkpoint_data or {}), "runtime_dependency": run_id})
    if owner.record_store is not None and saved["state"] not in {"COMPLETED", "FAILED", "CANCELED"}:
        owner.record_store(
            AgentRunRecord(
                run_key,
                owner.agent_id,
                saved["created_at"],
                metadata["input_digest"],
                "",
                status="waiting",
                id="long-record-" + digest,
                runtime_run_id=run_id,
                runtime_state=saved["state"],
                delivery_state="waiting_generation",
                runtime_event_seq=saved["seq"],
            ).to_dict()
        )
    engine = AgentRuntime(journal, Model(), QuantLabToolPort(bindings))
    try:
        state = engine.resume(run_id)
    except JournalIntegrityError:
        raise
    except Exception:
        # A commit can fail after external success. Keep the durable dependency
        # visible and wait for its lease/reconciliation instead of resending.
        state = engine.get(run_id)
        if state is None or not any(step["state"] in {"IN_FLIGHT", "RECONCILING"} for step in state["steps"]):
            raise
    return project_record(state)


def project_record(state):
    from .long_running import AgentRunRecord

    metadata = state["spec"]["metadata"]
    identity = metadata["business_identity"]
    run_id = state["id"]
    digest = run_id.removeprefix("long-")
    delivery = next((s for s in state["steps"] if s["kind"] == "tool" and s["payload"]["name"] == DELIVERY), None)
    confirmed = delivery["result"] if delivery and delivery["state"] == "SUCCEEDED" else {}
    pending = state["state"] not in {"COMPLETED", "FAILED", "CANCELED"}
    return AgentRunRecord(
        identity["key"],
        identity["agent"],
        state["created_at"],
        metadata["input_digest"],
        confirmed.get("body", ""),
        pushed_to=[identity["channel"]] if confirmed.get("sent") else [],
        status="waiting" if pending else "ok" if state["state"] == "COMPLETED" else state["state"].lower(),
        id="long-record-" + digest,
        runtime_run_id=run_id,
        runtime_state=state["state"],
        delivery_effect_id=delivery["effect_id"] if delivery else "",
        delivery_state="sent"
        if confirmed.get("sent")
        else "preview"
        if confirmed.get("preview")
        else delivery["state"].lower()
        if delivery
        else "waiting_generation",
        generation_source=confirmed.get("source", ""),
        runtime_event_seq=state["seq"],
    )
