"""Extract reusable subgraphs with explicit boundary ports, preserving semantics."""

from __future__ import annotations

import copy
import pprint

from .contracts import FORMAT, WorkflowError, compile_workflow, fingerprint, identifier, params_for, subflow_instance


def bounded(value):
    return identifier(value if len(value) <= 80 else value[:64] + "__" + fingerprint(value)[:14])


def encapsulate(workflow, selected, kind, title, registry, exposed=None):
    plan = compile_workflow(workflow, registry, allow_incomplete=True)
    identifier(kind)
    chosen = set(selected)
    by_id = {n["id"]: n for n in workflow["nodes"]}
    if not chosen or not chosen <= set(by_id):
        raise WorkflowError("请选择有效卡片")
    nodes = [copy.deepcopy(n) for n in workflow["nodes"] if n["id"] in chosen]
    x = sum(n.get("position", {}).get("x", 0) for n in nodes) / len(nodes)
    y = sum(n.get("position", {}).get("y", 0) for n in nodes) / len(nodes)
    for n in nodes:
        n["position"] = {"x": n.get("position", {}).get("x", 0) - x, "y": n.get("position", {}).get("y", 0) - y}
    internal = []
    incoming = []
    outgoing = []
    outside = []
    for e in workflow.get("edges", []):
        a = e["source"] in chosen
        b = e["target"] in chosen
        (internal if a and b else incoming if b else outgoing if a else outside).append(copy.deepcopy(e))
    for n in nodes:
        for name in plan["definitions"][n["id"]]["inputs"]:
            if not any(e["target"] == n["id"] and e["target_port"] == name for e in [*incoming, *internal]):
                incoming.append({"source": None, "source_port": None, "target": n["id"], "target_port": name})
    if not outgoing:
        for n in nodes:
            if not any(e["source"] == n["id"] for e in internal):
                for port in registry.get(n["kind"], str(n["version"]))["outputs"]:
                    outgoing.append(
                        {
                            "source": n["id"],
                            "source_port": port,
                            "target": None,
                            "target_port": None,
                        }
                    )
    inputs = {}
    outputs = {}
    imap = {}
    omap = {}
    for e in incoming:
        name = bounded(e["target"] + "__" + e["target_port"])
        inputs[name] = copy.deepcopy(plan["definitions"][e["target"]]["inputs"][e["target_port"]])
        imap[name] = {"node": e["target"], "port": e["target_port"]}
    for e in outgoing:
        name = bounded(e["source"] + "__" + e["source_port"])
        outputs[name] = copy.deepcopy(plan["definitions"][e["source"]]["outputs"][e["source_port"]])
        outputs[name].pop("role_from", None)
        omap[name] = {"node": e["source"], "port": e["source_port"]}
    boundary = {(endpoint["node"], endpoint["port"]): name for name, endpoint in imap.items()}

    def origins(node_id, port):
        inherited = plan["definitions"][node_id]["outputs"][port].get("role_from")
        result = []
        for name in inherited if isinstance(inherited, list) else [inherited] if inherited else []:
            wire = next((e for e in internal if e["target"] == node_id and e["target_port"] == name), None)
            if wire:
                result.extend(origins(wire["source"], wire["source_port"]))
            elif (node_id, name) in boundary:
                result.append(boundary[node_id, name])
        return list(dict.fromkeys(result))

    for name, endpoint in omap.items():
        inherited = origins(endpoint["node"], endpoint["port"])
        if inherited:
            outputs[name].pop("role", None)
            outputs[name]["role_from"] = inherited[0] if len(inherited) == 1 else inherited
    subflow = {
        "workflow": {
            "format": FORMAT,
            "title": title,
            "nodes": nodes,
            "edges": internal,
            "settings": workflow.get("settings", {}),
        },
        "inputs": imap,
        "outputs": omap,
    }
    properties = {}
    mappings = {}
    public_entries = copy.deepcopy(exposed or [])
    addressed = {(e.get("node"), e.get("parameter")) for e in public_entries}
    for n in nodes:
        d = plan["definitions"][n["id"]]
        for key in d["parameters"].get("required", []):
            if key not in params_for(n, d) and (n["id"], key) not in addressed:
                public_entries.append({"node": n["id"], "parameter": key})
    for entry in public_entries:
        node_id, parameter = entry.get("node"), entry.get("parameter")
        name = identifier(entry["name"]) if entry.get("name") else bounded(f"{node_id}__{parameter}")
        if node_id not in chosen or name in properties:
            raise WorkflowError("公开参数必须引用选中卡片，且名称唯一")
        definition = plan["definitions"][node_id]
        schema = definition["parameters"].get("properties", {}).get(parameter)
        if schema is None or parameter in by_id[node_id].get("bindings", {}):
            raise WorkflowError("公开参数不存在或已绑定到运行输入")
        properties[name] = copy.deepcopy(schema)
        properties[name]["title"] = (
            (by_id[node_id].get("title") or definition["title"]) + " · " + (schema.get("title") or parameter)
        )
        value = params_for(by_id[node_id], definition).get(parameter)
        if value is not None:
            properties[name]["default"] = value
        mappings[name] = {"node": node_id, "parameter": parameter}
    subflow["parameters"] = mappings
    spec = {
        "kind": kind,
        "version": "1",
        "title": title,
        "category": "我的研究模板",
        "level": "module",
        "description": "可复用子流程；内部函数、参数和具名边界保留。",
        "inputs": inputs,
        "outputs": outputs,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": [k for k, v in properties.items() if "default" not in v],
            "additionalProperties": False,
        },
        "requirements": [],
        "subflow": subflow,
    }
    code = (
        "NODE = "
        + pprint.pformat(spec, sort_dicts=False, width=110)
        + "\n\ndef execute(ctx, inputs, params):\n    return ctx.run_subflow(inputs, params)\n"
    )
    nid = bounded(kind.replace(".", "-") + "-instance")
    if nid in by_id:
        raise WorkflowError("子流程实例ID冲突，请使用新的kind")
    outer = copy.deepcopy(workflow)
    outer["nodes"] = [n for n in outer["nodes"] if n["id"] not in chosen] + [
        {
            "id": nid,
            "kind": kind,
            "version": "1",
            "params": {},
            "position": {"x": x, "y": y},
        }
    ]
    for e in incoming:
        e["target_port"] = bounded(e["target"] + "__" + e["target_port"])
        e["target"] = nid
    for e in outgoing:
        e["source_port"] = bounded(e["source"] + "__" + e["source_port"])
        e["source"] = nid
    outer["edges"] = (
        outside + [e for e in incoming if e["source"] is not None] + [e for e in outgoing if e["target"] is not None]
    )
    return code, outer


def expand(workflow, node_id, registry):
    outer = copy.deepcopy(workflow)
    node = next((n for n in outer["nodes"] if n["id"] == node_id), None)
    if not node:
        raise WorkflowError("子流程卡片不存在")
    definition = registry.get(node["kind"], str(node["version"]))
    sub = definition.get("subflow")
    if not sub:
        raise WorkflowError("这不是子流程")
    if node.get("bindings"):
        raise WorkflowError("公共参数含运行时输入绑定；请先解除绑定再展开，以免改变参数来源。保留封装时可正常执行。")
    instance = subflow_instance(definition, params_for(node, definition))
    nodes = copy.deepcopy(instance["nodes"])
    renamed = {n["id"]: bounded(node_id + "__" + n["id"]) for n in nodes}
    for n in nodes:
        n["id"] = renamed[n["id"]]
        n["position"] = {
            "x": n.get("position", {}).get("x", 0) + node.get("position", {}).get("x", 0),
            "y": n.get("position", {}).get("y", 0) + node.get("position", {}).get("y", 0),
        }
    edges = []
    for e in outer.get("edges", []):
        if e["target"] == node_id:
            endpoint = sub["inputs"][e["target_port"]]
            e["target"] = renamed[endpoint["node"]]
            e["target_port"] = endpoint["port"]
        if e["source"] == node_id:
            endpoint = sub["outputs"][e["source_port"]]
            e["source"] = renamed[endpoint["node"]]
            e["source_port"] = endpoint["port"]
        edges.append(e)
    for e in copy.deepcopy(sub["workflow"]["edges"]):
        e["id"] = "e-" + fingerprint([node_id, e])[:20]
        e["source"] = renamed[e["source"]]
        e["target"] = renamed[e["target"]]
        edges.append(e)
    outer["nodes"] = [n for n in outer["nodes"] if n["id"] != node_id] + nodes
    outer["edges"] = edges
    compile_workflow(outer, registry, allow_incomplete=True)
    return outer
