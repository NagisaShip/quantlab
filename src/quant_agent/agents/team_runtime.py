"""QuantLab parallel Team host for the Runtime's persistent child/join contract."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from ..platform import context
from ..platform.errors import AppError, ErrorCode
from .qmoji import decorate_qmoji
from .roles import Role
from .runtime_agent import prepare_agent
from .team import PARALLEL_BRIEF, TeamRun, TeamRunStep, fallback_synthesis


def _engine(runner: Any):
    from agent_runtime import AgentRuntime

    if runner.runtime_journal is None:
        raise ValueError("Agent Runtime is not enabled")
    return AgentRuntime(runner.runtime_journal, None, None)


def _load(runner: Any, run_id: str, *, write: bool = False):
    engine = _engine(runner)
    root = engine.get("team-root-" + run_id)
    if root is None:
        return None
    if root["tenant_id"] != context.current_workspace():
        raise AppError(ErrorCode.PERMISSION_DENIED, "团队运行不属于当前工作区")
    metadata = root["spec"].get("metadata", {})
    if "team_plan" not in metadata or metadata["team_plan"]["run"]["id"] != run_id:
        raise ValueError("invalid Team Runtime identity")
    if write and metadata["user_id"] != (context.current().user_id or "system"):
        raise AppError(ErrorCode.PERMISSION_DENIED, "仅原发起人可继续或取消团队运行")
    if write and not context.current().can_write:
        raise AppError(ErrorCode.PERMISSION_DENIED, "当前角色无权推进团队运行")
    return root


def _decorate(output: str, role: Role, metrics: dict) -> str:
    return decorate_qmoji(output, role=role.id, tags=role.tags, metrics=metrics)


def start_parallel(
    runner: Any, run: TeamRun, roles: list[Role], synthesizer: Role, shared: str, metrics: dict
) -> TeamRun:
    engine = _engine(runner)
    run.runtime_run_id = "team-root-" + run.id
    parent_agent = runner._make_agent(synthesizer, max_steps=2, use_tools=False)
    task = (
        f"调研主题：{run.topic}\n\n"
        "接下来 child_results 中是各角色已提交的独立意见。按声明顺序对应角色："
        + "、".join(role.name for role in roles)
        + "。失败或取消的角色没有可用结论，不能当成有效证据。"
        "请汇总共识、分歧、你的判断、结论和下一步行动。不要再调用工具。"
    )
    parent = prepare_agent(parent_agent, task)
    plan = {
        "run": run.to_dict(),
        "roles": [role.to_dict() for role in roles],
        "synthesizer": synthesizer.to_dict(),
        "metrics": metrics,
    }
    parent_spec = replace(
        parent.spec,
        metadata={**parent.spec.metadata, "team_plan": plan, "child_output_chars": runner.SYNTH_CHARS_PER_STEP},
    )
    children = [prepare_agent(runner._make_agent(role), shared + PARALLEL_BRIEF) for role in roles]
    engine.start_with_children(
        task,
        parent_spec,
        children,
        run_id=run.runtime_run_id,
        tenant_id=context.current_workspace(),
        initial_messages=parent.initial_messages,
        join_policy="all_settled",
    )
    # The index is a rebuildable projection. The full plan was committed with
    # the graph, so an index write failure cannot orphan an unidentifiable run.
    runner._persist(project(runner, engine.get(run.runtime_run_id)))
    return resume(runner, run.id)


def project(runner: Any, root: dict) -> TeamRun:
    plan = root["spec"]["metadata"]["team_plan"]
    record = plan["run"]
    run = TeamRun(
        id=record["id"],
        team_id=record["team_id"],
        team_name=record["team_name"],
        topic=record["topic"],
        mode=record["mode"],
        context=record.get("context", ""),
        created_at=record["created_at"],
        runtime_run_id=root["id"],
        runtime_event_seq=root["seq"],
    )
    run.can_manage = (
        root["spec"]["metadata"]["user_id"] == (context.current().user_id or "system") and context.current().can_write
    )
    children = _engine(runner).children(root["id"])
    if len(children) != len(plan["roles"]):
        raise ValueError("Team plan and child runs disagree")
    for child_id, frozen in zip(children, plan["roles"], strict=True):
        child = runner.runtime_journal.run(child_id)
        role = Role.from_dict(frozen)
        successful = child["state"] == "COMPLETED"
        status = {"COMPLETED": "ok", "FAILED": "error", "CANCELED": "error"}.get(child["state"], "waiting")
        output = child.get("output") or ""
        steps = runner.runtime_journal.steps(child_id)
        models = [step["result"] for step in steps if step["kind"] == "model" and step.get("result")]
        actual = models[-1] if models else {}
        model = "/".join(str(actual.get(key) or "") for key in ("provider", "model")).strip("/")
        run.steps.append(
            TeamRunStep(
                role_id=role.id,
                role_name=role.name,
                model=model or role.model or "Runtime",
                status=status,
                output=_decorate(output, role, plan["metrics"]) if successful else "",
                error="" if successful else (child.get("error") or f"Runtime 状态：{child['state']}"),
                elapsed_ms=max(0, child["updated_at"] - child["created_at"]),
                runtime_run_id=child_id,
            )
        )
    run.status = {
        "COMPLETED": "done",
        "FAILED": "failed",
        "CANCELED": "canceled",
        "CANCELING": "canceling",
        "WAITING": "waiting",
    }.get(root["state"], "running")
    role = Role.from_dict(plan["synthesizer"])
    if root.get("output"):
        run.synthesis = _decorate(root["output"], role, plan["metrics"])
    elif root["state"] == "FAILED":
        run.synthesis = fallback_synthesis(run.steps, reason=root.get("error") or "汇总失败")
    return run


def resume(runner: Any, run_id: str) -> TeamRun:
    root = _load(runner, run_id, write=True)
    if root is None:
        raise KeyError(run_id)
    plan = root["spec"]["metadata"]["team_plan"]
    engine = _engine(runner)
    if root["state"] not in {"COMPLETED", "FAILED", "CANCELED"}:
        children = engine.children(root["id"])

        def one(child_id: str, frozen: dict):
            child = runner.runtime_journal.run(child_id)
            role = Role.from_dict(frozen)
            names = [tool["name"] for tool in child["spec"]["tools"]]
            agent = runner._make_agent(role, max_steps=child["spec"]["max_steps"], tool_names=names)
            agent.resume_runtime(child_id)

        with ThreadPoolExecutor(max_workers=min(runner.max_workers, len(children))) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, one, child, role)
                for child, role in zip(children, plan["roles"], strict=True)
            ]
            for future in futures:
                future.result()
        # Resolve the join with control-plane ports first. Only a READY parent
        # is handed to its frozen synthesis model by the host below.
        role = Role.from_dict(plan["synthesizer"])
        agent = runner._make_agent(role, max_steps=2, use_tools=False)
        agent.resume_runtime(root["id"])
    result = project(runner, engine.get(root["id"]))
    runner._persist(result)
    return result


def get_run(runner: Any, run_id: str) -> TeamRun | None:
    root = _load(runner, run_id)
    return project(runner, root) if root else None


def cancel(runner: Any, run_id: str, *, signal_id: str) -> TeamRun:
    root = _load(runner, run_id, write=True)
    if root is None:
        raise KeyError(run_id)
    engine = _engine(runner)
    engine.signal(root["id"], "cancel", {}, signal_id=signal_id, actor=context.current().user_id or "system")
    engine.resume(root["id"])
    result = project(runner, engine.get(root["id"]))
    runner._persist(result)
    return result


def recover_indexes(runner: Any, records: list[dict], limit: int) -> list[dict]:
    with runner.runtime_journal.connection() as db:
        roots = db.execute(
            "SELECT id FROM runs WHERE tenant_id=? AND parent_id IS NULL AND id LIKE 'team-root-%' "
            "ORDER BY created_at DESC LIMIT ?",
            (context.current_workspace(), limit),
        ).fetchall()
    merged = {record["id"]: record for record in records}
    for row in roots:
        run_id = row["id"].removeprefix("team-root-")
        run = get_run(runner, run_id)
        if run:
            merged[run_id] = run.to_dict()
    return sorted(merged.values(), key=lambda item: item.get("created_at", 0), reverse=True)[:limit]
