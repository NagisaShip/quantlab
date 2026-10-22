"""Versioned, bounded chat display payload made only from saved state/evidence."""

from .contracts import WorkflowError, canonical


def workflow_view(service, record, report=None, run=None, validation=None):
    from ..platform.context import current_workspace

    flow = record["workflow"]
    registry = service.registry()
    nodes = []
    for node in flow["nodes"]:
        try:
            definition = registry.get(node["kind"], str(node["version"]))
        except WorkflowError:
            definition = {"title": node["kind"], "parameters": {}, "inputs": {}, "outputs": {}}
        actual = (report or {}).get("nodes", {}).get(node["id"], {})
        params = {
            k: v.get("default") for k, v in definition["parameters"].get("properties", {}).items() if "default" in v
        }
        params.update(node.get("params", {}))
        params = actual.get("params", params)
        nodes.append(
            {
                "id": node["id"],
                "kind": node["kind"],
                "version": str(node["version"]),
                "title": node.get("title") or definition["title"],
                "parameters": {
                    k: v if len(canonical(v)) <= 600 else {"summary": canonical(v)[:600], "truncated": True}
                    for k, v in params.items()
                },
                "status": actual.get("status", "not_run"),
                "error": actual.get("error", ""),
                "inputs": list(definition["inputs"]),
                "outputs": actual.get("outputs", {}),
                "child_run_id": actual.get("child_run_id"),
                "has_subflow": bool(definition.get("subflow")),
                "declared_outputs": list(definition["outputs"]),
            }
        )
    return {
        "format": "quantlab.workflow-view/1",
        "workspace_id": current_workspace(),
        "workflow_id": record["id"],
        "revision": record["revision"],
        "title": record["title"],
        "nodes": nodes,
        "edges": flow.get("edges", []),
        "run_id": (run or {}).get("id"),
        "state": (run or {}).get("state", "saved"),
        "report_status": (report or {}).get("status"),
        "validation": validation,
        "evidence": "run" if report else "saved",
        "read_only": True,
    }
