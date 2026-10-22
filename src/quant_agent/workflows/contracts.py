"""Single-source contracts shared by CLI, GUI, Agent tools and exports."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
from typing import Any

FORMAT = "quantlab.workflow/1"
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
MAX_NODES = 160


class WorkflowError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise WorkflowError(f"非法标识：{value!r}")
    return value


def normalize_workflow(workflow):
    """Accept the documented endpoint form while keeping one execution dialect."""
    if not isinstance(workflow, dict):
        raise WorkflowError("工作流须为对象；使用workflow_help查看示例")
    result = copy.deepcopy(workflow)
    edges = result.get("edges", [])
    if not isinstance(edges, list):
        raise WorkflowError("edges须为连接数组")
    for edge in edges:
        if not isinstance(edge, dict):
            raise WorkflowError("每条连接须声明source/source_port/target/target_port")
        for old, node_key, port_key in (("from", "source", "source_port"), ("to", "target", "target_port")):
            endpoint = edge.pop(old, None)
            if endpoint is not None:
                if not isinstance(endpoint, dict) or not {"node", "port"} <= endpoint.keys():
                    raise WorkflowError(f"连接{old}须声明node和port")
                for key, value in ((node_key, endpoint["node"]), (port_key, endpoint["port"])):
                    if key in edge and edge[key] != value:
                        raise WorkflowError("连接的两种写法相互冲突")
                    edge[key] = value
    result["edges"] = edges
    return result


def installed_requirement(requirement: str) -> str:
    """Check the declared version range, not just distribution presence."""
    import importlib.metadata

    try:
        from packaging.requirements import Requirement
    except ImportError as exc:
        raise WorkflowError("依赖版本检查需要packaging>=24，请在运行环境安装") from exc
    parsed = Requirement(requirement)
    try:
        version = importlib.metadata.version(parsed.name)
    except importlib.metadata.PackageNotFoundError as exc:
        raise WorkflowError(f"缺少依赖{requirement}；请先在执行环境安装") from exc
    if parsed.specifier and not parsed.specifier.contains(version, prereleases=True):
        raise WorkflowError(f"依赖{parsed.name}已安装{version}，不满足{parsed.specifier}")
    return version


def validate_value(value: Any, schema: dict, path: str = "参数") -> None:
    """Deliberately bounded JSON Schema subset, documented in custom-node spec."""
    types = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "object": dict,
        "array": list,
        "null": type(None),
    }
    kind = schema.get("type")
    if kind and kind not in types:
        raise WorkflowError(f"{path}使用了不支持的schema类型 {kind}")
    if kind and (not isinstance(value, types[kind]) or (kind in ("number", "integer") and isinstance(value, bool))):
        raise WorkflowError(f"{path}必须为{kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise WorkflowError(f"{path}不在允许范围")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise WorkflowError(f"{path}缺少 {key}")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise WorkflowError(f"{path}含未知字段：{', '.join(sorted(set(value) - set(props)))}")
        for key, item in value.items():
            if key in props:
                validate_value(item, props[key], f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise WorkflowError(f"{path}项目不足")
        if len(value) > schema.get("maxItems", 1_000_000):
            raise WorkflowError(f"{path}项目过多")
        for item in value:
            validate_value(item, schema.get("items", {}), f"{path}[]")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise WorkflowError(f"{path}超出范围")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            raise WorkflowError(f"{path}长度不合适")


def parse_definition(code: str) -> dict:
    if len(code.encode()) > 150_000:
        raise WorkflowError("卡片代码超过150KB")
    try:
        tree = ast.parse(code)
        assignments = [
            n
            for n in tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "NODE" for t in n.targets)
        ]
        if len(assignments) != 1:
            raise WorkflowError("代码必须有且只有一个字面量 NODE 定义")
        spec = ast.literal_eval(assignments[0].value)
    except (SyntaxError, ValueError, TypeError) as exc:
        raise WorkflowError(f"代码或NODE定义无效：{exc}") from exc
    if not isinstance(spec, dict):
        raise WorkflowError("NODE必须是字典")
    for key in ("kind", "version", "title", "inputs", "outputs", "parameters"):
        if key not in spec:
            raise WorkflowError(f"NODE缺少{key}")
    identifier(spec["kind"])
    identifier("v" + str(spec["version"]))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute"]
    if len(funcs) != 1 or [a.arg for a in funcs[0].args.args] != [
        "ctx",
        "inputs",
        "params",
    ]:
        raise WorkflowError("需要定义 execute(ctx, inputs, params)")
    if not isinstance(spec["inputs"], dict) or not isinstance(spec["outputs"], dict) or not spec["outputs"]:
        raise WorkflowError("inputs和outputs必须为具名端口字典，至少一个输出")
    for name, port in {**spec["inputs"], **spec["outputs"]}.items():
        identifier(name)
        if not isinstance(port, dict) or "type" not in port:
            raise WorkflowError(f"端口{name}需要type")
    if not isinstance(spec["parameters"], dict) or spec["parameters"].get("type") != "object":
        raise WorkflowError("parameters必须是object schema")
    if spec.get("subflow"):
        expected = ast.parse("def execute(ctx, inputs, params):\n    return ctx.run_subflow(inputs, params)\n").body[0]
        if ast.dump(funcs[0], include_attributes=False) != ast.dump(expected, include_attributes=False):
            raise WorkflowError("复合卡片必须保留标准分派函数；修改算法请展开子流程或编辑NODE.subflow")
    deps = spec.get("requirements", [])
    if not isinstance(deps, list) or any(
        not isinstance(d, str)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~]{1,2}[A-Za-z0-9.*+!_-]+(?:,[<>=!~]{1,2}[A-Za-z0-9.*+!_-]+)*)?",
            d,
        )
        for d in deps
    ):
        raise WorkflowError("requirements仅支持PyPI包名和版本约束，不接受URL、路径或pip参数")
    spec = {
        **spec,
        "requirements": deps,
        "code": code,
        "code_hash": hashlib.sha256(code.encode()).hexdigest(),
    }
    return spec


def compatible(source: dict, target: dict, *, check_roles=True) -> bool:
    if target["type"] != "any" and source["type"] != target["type"]:
        return False
    required_role = target.get("role")
    if check_roles and required_role and source.get("role") != required_role:
        return False
    if check_roles and target.get("forbid_roles") and source.get("role") in target["forbid_roles"]:
        return False
    for key, expected in target.get("semantics", {}).items():
        if expected is not None and source.get("semantics", {}).get(key) != expected:
            return False
    return True


def subflow_instance(definition, params):
    """Apply only explicitly exposed public parameters to a copied child graph."""
    child = copy.deepcopy(definition["subflow"]["workflow"])
    nodes = {n["id"]: n for n in child["nodes"]}
    for public, targets in definition["subflow"].get("parameters", {}).items():
        if public not in definition["parameters"].get("properties", {}):
            raise WorkflowError("子流程暴露了未声明的公共参数：" + public)
        if public not in params:
            continue
        for endpoint in targets if isinstance(targets, list) else [targets]:
            if endpoint.get("node") not in nodes:
                raise WorkflowError("公共参数指向不存在的内部节点")
            nodes[endpoint["node"]].setdefault("params", {})[endpoint["parameter"]] = copy.deepcopy(params[public])
    return child


def params_for(node: dict, definition: dict) -> dict:
    props = definition["parameters"].get("properties", {})
    return {
        **{k: v["default"] for k, v in props.items() if "default" in v},
        **node.get("params", {}),
    }


def compile_workflow(
    workflow: dict, registry, *, target: str | None = None, external=None, depth=0, allow_incomplete=False
) -> dict:
    workflow = normalize_workflow(workflow)
    if depth > 8:
        raise WorkflowError("子流程嵌套不能超过8层")
    if workflow.get("format") != FORMAT:
        raise WorkflowError(f"工作流格式必须为{FORMAT}")
    nodes = workflow.get("nodes", [])
    if not isinstance(nodes, list) or not 0 < len(nodes) <= MAX_NODES:
        raise WorkflowError("工作流需要1—160张卡片")
    by_id = {}
    definitions = {}
    for n in nodes:
        nid = identifier(n["id"])
        if nid in by_id:
            raise WorkflowError(f"重复卡片ID {nid}")
        by_id[nid] = n
        definitions[nid] = copy.deepcopy(registry.get(n["kind"], str(n["version"])))
    incoming = {nid: {} for nid in by_id}
    dependencies = {nid: set() for nid in by_id}
    for edge in workflow.get("edges", []):
        a, b = edge.get("source"), edge.get("target")
        if a not in by_id or b not in by_id:
            raise WorkflowError("连接引用了不存在的卡片")
        outp = definitions[a]["outputs"].get(edge.get("source_port"))
        inp = definitions[b]["inputs"].get(edge.get("target_port"))
        if not outp or not inp:
            raise WorkflowError(f"{a} → {b}连接的端口不存在")
        if not compatible(outp, inp, check_roles=not outp.get("role_from")):
            raise WorkflowError(f"{a} → {b}输入类型或研究用途不兼容")
        name = edge["target_port"]
        if name in incoming[b]:
            raise WorkflowError(f"{b}.{name}只能连接一个来源")
        incoming[b][name] = edge
        dependencies[b].add(a)
    selected = set(by_id)
    if target:
        if target not in by_id:
            raise WorkflowError("目标卡片不存在")
        selected = set()

        def visit(nid):
            if nid in selected:
                return
            selected.add(nid)
            for parent in dependencies[nid]:
                visit(parent)

        visit(target)
    order = []
    remaining = set(selected)
    while remaining:
        ready = sorted(n for n in remaining if not dependencies[n] & remaining)
        if not ready:
            raise WorkflowError("工作流存在循环依赖")
        order.extend(ready)
        remaining.difference_update(ready)
    for nid in order:
        definition = definitions[nid]
        # Composite boundary placeholders carry the effective role rather than
        # erasing train/validation/test when the public port is generic.
        if definition.get("subflow"):
            for name, meta in definition["inputs"].items():
                edge = incoming[nid].get(name)
                origin = (
                    definitions[edge["source"]]["outputs"][edge["source_port"]]
                    if edge
                    else (external or {}).get(nid, {}).get(name, {})
                )
                if not meta.get("role") and origin.get("role"):
                    meta["role"] = origin["role"]
        for meta in definition["outputs"].values():
            inherited = meta.get("role_from")
            if inherited:
                names = inherited if isinstance(inherited, list) else [inherited]
                roles = []
                for name in names:
                    if not isinstance(name, str) or name not in definition["inputs"]:
                        raise WorkflowError("用途继承指向不存在的输入")
                    edge = incoming[nid].get(name)
                    origin = (
                        definitions[edge["source"]]["outputs"][edge["source_port"]]
                        if edge
                        else (external or {}).get(nid, {}).get(name, {})
                    )
                    if origin.get("role"):
                        roles.append(origin["role"])
                # A join with validation/test data must never relabel the result train.
                meta["role"] = next(
                    (r for r in ("test", "validation", "train") if r in roles), roles[0] if roles else None
                )
        for name, edge in incoming[nid].items():
            if not compatible(definitions[edge["source"]]["outputs"][edge["source_port"]], definition["inputs"][name]):
                raise WorkflowError(f"{nid}.{name}的字段语义或训练/验证/测试用途不兼容")
        for port, meta in definition["inputs"].items():
            reference = (external or {}).get(nid, {}).get(port)
            if reference and reference.get("type") and not compatible(reference, meta):
                raise WorkflowError(f"{nid}.{port}外部输入的类型、语义或用途不兼容")
            if (
                not allow_incomplete
                and meta.get("required", True)
                and port not in incoming[nid]
                and port not in (external or {}).get(nid, {})
            ):
                raise WorkflowError(f"{nid}缺少输入 {port}")
        if definition.get("subflow"):
            sub = definition["subflow"]
            ext = {}
            for name, endpoint in sub["inputs"].items():
                if name not in definition["inputs"]:
                    raise WorkflowError("子流程输入映射不存在")
                ext.setdefault(endpoint["node"], {})[endpoint["port"]] = {
                    "placeholder": True,
                    **definition["inputs"][name],
                }
            child = compile_workflow(
                subflow_instance(definition, params_for(by_id[nid], definition)),
                registry,
                external=ext,
                depth=depth + 1,
                allow_incomplete=allow_incomplete or bool(by_id[nid].get("bindings")),
            )
            for name, endpoint in sub["inputs"].items():
                child_port = child["definitions"].get(endpoint["node"], {}).get("inputs", {}).get(endpoint["port"])
                if not child_port or not compatible(definition["inputs"][name], child_port):
                    raise WorkflowError("子流程输入映射不兼容")
            for name, endpoint in sub["outputs"].items():
                child_port = child["definitions"].get(endpoint["node"], {}).get("outputs", {}).get(endpoint["port"])
                if (
                    not child_port
                    or name not in definition["outputs"]
                    or not compatible(child_port, definition["outputs"][name])
                ):
                    raise WorkflowError("子流程输出映射不兼容")
        params = params_for(by_id[nid], definition)
        bound = by_id[nid].get("bindings", {})
        if set(bound) - set(definition["parameters"].get("properties", {})):
            raise WorkflowError(f"{nid}绑定了不存在的参数")
        for name, binding in bound.items():
            if (
                not allow_incomplete
                and binding.get("input") not in incoming[nid]
                and binding.get("input") not in (external or {}).get(nid, {})
            ):
                raise WorkflowError(f"{nid}.{name}绑定来源未连接")
        # Bound values are validated again after resolving the actual input.
        schema = {
            **definition["parameters"],
            "required": []
            if allow_incomplete
            else [k for k in definition["parameters"].get("required", []) if k not in bound],
        }
        validate_value({k: v for k, v in params.items() if k not in bound}, schema, nid)
    semantic = {
        "format": FORMAT,
        "nodes": [
            {k: v for k, v in by_id[n].items() if k not in ("position", "title", "flipped", "ui")} for n in order
        ],
        "edges": [e for e in workflow.get("edges", []) if e["source"] in selected and e["target"] in selected],
        "definitions": {n: definitions[n]["code_hash"] for n in order},
        "settings": workflow.get("settings", {}),
        "external": external or {},
    }
    return {
        "order": order,
        "incoming": incoming,
        "definitions": definitions,
        "nodes": by_id,
        "semantic_hash": fingerprint(semantic),
        "target": target,
    }
