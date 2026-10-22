"""Small, explicit experiment plans shared by GUI and agents."""

from __future__ import annotations

import copy
import itertools

from .contracts import WorkflowError, compile_workflow, params_for, validate_value


def variants(workflow, registry, plan):
    if not isinstance(plan, dict):
        raise WorkflowError(
            'plan须为对象，例如{"mode":"grid","axes":[{"node":"source","parameter":"max_rows","values":[10,20]}]}；用workflow_help(topic="experiments")查看完整规范'
        )
    mode = plan.get("mode", "grid")
    if mode == "grid":
        axes = plan.get("axes", [])
        if (
            not isinstance(axes, list)
            or not 1 <= len(axes) <= 4
            or any(
                not isinstance(a, dict)
                or not isinstance(a.get("node"), str)
                or not isinstance(a.get("parameter"), str)
                or not isinstance(a.get("values"), list)
                or not a["values"]
                for a in axes
            )
        ):
            raise WorkflowError(
                'plan.axes需要1—4个对象；每项必须有node、parameter、values。示例：{"mode":"grid","axes":[{"node":"source","parameter":"max_rows","values":[10,20]}]}。grid/params/parameters不是axes的别名'
            )
        size = 1
        for axis in axes:
            size *= len(axis["values"])
        if size > 8:
            raise WorkflowError("一次最多8个实验组合，请缩小参数网格")
        keys = [(axis.get("node"), axis.get("parameter")) for axis in axes]
        if len(set(keys)) != len(keys):
            raise WorkflowError("网格轴不能重复")
        candidates = [
            {
                "name": " / ".join(f"{a['node']}.{a['parameter']}={v}" for a, v in zip(axes, values, strict=True)),
                "updates": [
                    {"node": a["node"], "parameter": a["parameter"], "value": v}
                    for a, v in zip(axes, values, strict=True)
                ],
            }
            for values in itertools.product(*(a["values"] for a in axes))
        ]
    elif mode == "rolling":
        candidates = plan.get("windows", [])
        if not 1 <= len(candidates) <= 8:
            raise WorkflowError("滚动实验需要1—8个明确时间窗口")
        for candidate in candidates:
            if not candidate.get("updates"):
                raise WorkflowError("每个时间窗口须明确更新训练和验证边界参数")
    else:
        raise WorkflowError("实验模式仅支持grid或rolling")
    results = []
    for i, candidate in enumerate(candidates):
        flow = copy.deepcopy(workflow)
        seen = set()
        for update in candidate["updates"]:
            key = (update["node"], update["parameter"])
            if key in seen:
                raise WorkflowError("一个实验不能重复更新同一参数")
            seen.add(key)
            node = next((n for n in flow["nodes"] if n["id"] == update["node"]), None)
            if node is None:
                raise WorkflowError("实验参数引用不存在的卡片")
            definition = registry.get(node["kind"], str(node["version"]))
            schema = definition["parameters"].get("properties", {}).get(update["parameter"])
            if schema is None or update["parameter"] in node.get("bindings", {}):
                raise WorkflowError("实验参数不存在或已绑定到输入")
            validate_value(update["value"], schema)
            node.setdefault("params", {})[update["parameter"]] = update["value"]
        if mode == "rolling":
            splits = [n for n in flow["nodes"] if n["kind"] in ("research.temporal_split", "research.purged_split")]
            if not splits:
                raise WorkflowError("滚动实验需要明确的时间切分卡片")
            for n in splits:
                p = params_for(n, registry.get(n["kind"], str(n["version"])))
                import math
                from datetime import datetime, timezone

                try:
                    train_end, validation_end = float(p["train_end"]), float(p["validation_end"])
                    if not all(math.isfinite(x) for x in (train_end, validation_end)):
                        raise ValueError("non-finite boundary")
                except (ValueError, TypeError):
                    try:

                        def iso(value):
                            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)

                        train_end, validation_end = iso(p["train_end"]), iso(p["validation_end"])
                    except (ValueError, TypeError):
                        raise WorkflowError("窗口截止时间须同时为有限数值或ISO8601日期时间") from None
                if train_end >= validation_end:
                    raise WorkflowError("每个窗口训练截止必须早于验证截止")
            if not any((n["id"], "train_end") in seen and (n["id"], "validation_end") in seen for n in splits):
                raise WorkflowError("滚动窗口须同时明确更新至少一个切分卡片的训练和验证截止")
        compile_workflow(flow, registry)
        results.append(
            {
                "name": str(candidate.get("name") or f"实验{i + 1}")[:160],
                "workflow": flow,
                "updates": candidate["updates"],
            }
        )
    return results


def submit(service, wid, plan, revision=None, origin="gui"):
    from .plugins import require
    require(service, "experiments", agent=origin == "agent")
    service.writable()
    record = service.get(wid, revision)
    rows = variants(record["workflow"], service.registry(), plan)
    for row in rows:
        checks = service.validate(row["workflow"])
        if not checks["ok"]:
            raise WorkflowError("实验预检失败：" + row["name"] + " " + str(checks["checks"]))
    runs = []
    errors = []
    for row in rows:
        try:
            run = service.queue(wid, revision=record["revision"], origin=origin, variant=row)
            runs.append({"name": row["name"], "run": run})
        except Exception as exc:
            errors.append({"name": row["name"], "error": str(exc)})
            break  # Preserve already enqueued IDs rather than claiming atomic dispatch.
    return {"runs": runs, "errors": errors, "planned": len(rows), "submitted": len(runs)}


def comparison(service, run_ids):
    from .plugins import require
    require(service, "experiments")
    if not 1 <= len(run_ids) <= 8:
        raise WorkflowError("一次比较1—8次实验")
    from ..platform.context import current_workspace

    rows = []
    for run_id in run_ids:
        run = service.services.runs.require(run_id, current_workspace())
        report = service.run_report(run_id)
        metrics = {}
        if report:
            from .runtime import Runner

            runner = Runner(service.root, service.registry())

            def flatten(value, path="", depth=0, metrics=metrics):
                if isinstance(value, dict) and depth < 4:
                    for key, child in value.items():
                        flatten(child, path + "." + key, depth + 1)
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    metrics[path.lstrip(".")] = value

            for nid, node in report["nodes"].items():
                for name, ref in node.get("outputs", {}).items():
                    if ref.get("type") == "report":
                        flatten(runner.read(ref), nid + "." + name)
        rows.append(
            {
                "id": run_id,
                "name": run.get("manifest", {}).get("variant", {}).get("name", run_id),
                "state": run["state"],
                "status": report.get("status") if report else None,
                "workflow_hash": report.get("workflow_hash") if report else None,
                "metrics": metrics,
                "error": run.get("error_message"),
            }
        )
    return {"experiments": rows}
