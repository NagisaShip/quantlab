"""Bounded, workspace-scoped projection of the latest usable research result."""

from __future__ import annotations

from ..platform.context import current_workspace

ACTIVE_STATES = {"QUEUED", "PENDING", "RUNNING", "PAUSED", "RECOVERING"}
RUN_SCAN_LIMIT = 200
FORMAT = "quantlab.recent-result/1"


def _attempt(run, workflows):
    manifest = run.get("manifest") or {}
    workflow_id = manifest.get("workflow_id")
    record = workflows.get(workflow_id) or {}
    return {
        "run_id": run["id"],
        "state": run.get("state"),
        "workflow_id": workflow_id,
        "workflow_title": record.get("title") or run.get("title") or workflow_id,
        "revision": manifest.get("revision"),
        "origin": manifest.get("origin", "gui"),
        "created_at": run.get("created_at"),
    }


def _artifact_count(service, report):
    """Count output refs whose immutable JSON artifact still exists."""
    nodes = report.get("nodes")
    if not isinstance(nodes, dict):
        return 0
    count = 0
    artifact_root = service.root / "artifacts"
    for node in nodes.values():
        outputs = node.get("outputs") if isinstance(node, dict) else None
        if not isinstance(outputs, dict):
            continue
        for ref in outputs.values():
            digest = ref.get("hash") if isinstance(ref, dict) else None
            if not isinstance(digest, str) or len(digest) != 64:
                continue
            if not all(char in "0123456789abcdef" for char in digest):
                continue
            if (artifact_root / f"{digest}.json").is_file():
                count += 1
    return count


def _reason(state, latest):
    workflow = latest.get("workflow_title") if latest else ""
    if state == "running":
        return f"「{workflow}」最近一次运行正在进行，尚无已完成结果"
    if state == "failed":
        return f"「{workflow}」最近一次运行未成功，尚无已完成结果"
    if state == "unavailable":
        return "已有运行缺少可读取的报告或工件，最近结果暂不可用"
    return "本项目还没有已完成的研究结果"


def latest_result(service):
    """Return the newest completed research run with real artifact evidence.

    The scan is one workspace-scoped query plus report reads until the first valid
    candidate. Failed, pending and debug runs never masquerade as results.
    """
    runs = service.services.runs.list(current_workspace(), kind="workflow", limit=RUN_SCAN_LIMIT)
    workflows = {record["id"]: record for record in service.workflows()}
    latest_attempt = _attempt(runs[0], workflows) if runs else None
    broken = False

    for run in runs:
        if run.get("state") != "COMPLETED":
            continue
        manifest = run.get("manifest") or {}
        workflow_id = manifest.get("workflow_id")
        if not workflow_id or workflow_id not in workflows:
            broken = True
            continue
        if manifest.get("mode") != "research":
            continue
        report = service.run_report(run["id"])
        if not report or report.get("status") != "completed" or report.get("mode") != "research":
            broken = True
            continue
        artifact_count = _artifact_count(service, report)
        if not artifact_count:
            broken = True
            continue
        return {
            "format": FORMAT,
            "state": "ready",
            "result": {
                "run_id": run["id"],
                "workflow_id": workflow_id,
                "workflow_title": workflows[workflow_id].get("title", workflow_id),
                "revision": manifest.get("revision"),
                "origin": manifest.get("origin", "gui"),
                "created_at": run.get("created_at"),
                "artifact_count": artifact_count,
            },
            "latest_attempt": latest_attempt,
            "reason": None,
        }

    state = "empty"
    if broken:
        state = "unavailable"
    elif latest_attempt:
        if latest_attempt["state"] in ACTIVE_STATES:
            state = "running"
        elif latest_attempt["state"] in ("FAILED", "CANCELED"):
            state = "failed"
    return {
        "format": FORMAT,
        "state": state,
        "result": None,
        "latest_attempt": latest_attempt,
        "reason": _reason(state, latest_attempt),
    }
