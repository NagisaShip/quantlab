"""A validated component DAG is the source for shapes, visualization and execution."""

from __future__ import annotations

import math
from copy import deepcopy

NODE_FIELDS = {
    "Input": ["id", "type", "shape"],
    "Linear": ["id", "type", "inputs", "out_features", "bias"],
    "Dropout": ["id", "type", "inputs", "p"],
    "GRU": ["id", "type", "inputs", "hidden_size", "return_sequences"],
    **{
        name: ["id", "type", "inputs"]
        for name in ("ReLU", "GELU", "Sigmoid", "Tanh", "Identity", "LayerNorm", "Flatten", "Add", "Concat")
    },
}


def normalize(graph):
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        keys = sorted(graph) if isinstance(graph, dict) else []
        raise ValueError(
            f"网络须声明nodes数组和output；收到字段{keys}。示例：{{'nodes':[{{'id':'in','type':'Input','shape':[8]}},{{'id':'out','type':'Linear','inputs':['in'],'out_features':1}}],'output':'out'}}。workflow_help(topic='network')可查完整规范。"
        )
    result = deepcopy(graph)
    for n in result["nodes"]:
        if isinstance(n, dict) and isinstance(n.get("type"), str):
            n["type"] = next(
                (k for k in NODE_FIELDS if k.lower() == n["type"].lower()),
                {"dense": "Linear"}.get(n["type"].lower(), n["type"]),
            )
            if n["type"] not in NODE_FIELDS:
                raise ValueError(
                    f"组件{n.get('id')}的type={n['type']!r}不受支持；允许{list(NODE_FIELDS)}；Input使用shape:[特征数]。收到字段{sorted(n)}"
                )
    if "edges" in result:
        edges = result.pop("edges")
        if not isinstance(edges, list):
            raise ValueError("网络edges须为[{source:上游ID,target:下游ID}]数组")
        links = {}
        ids = {n.get("id") for n in result["nodes"] if isinstance(n, dict) and isinstance(n.get("id"), str)}
        for edge in edges:
            if not isinstance(edge, dict) or edge.get("source") not in ids or edge.get("target") not in ids:
                raise ValueError("每条网络edge须有有效source和target组件ID；Input节点声明shape:[8]")
            links.setdefault(edge["target"], []).append(edge["source"])
        for n in result["nodes"]:
            if not isinstance(n, dict):
                continue
            parents = links.get(n.get("id"), [])
            if "inputs" in n and n["inputs"] != parents:
                raise ValueError(f"{n.get('id')}的inputs与edges冲突；只使用一种连线定义或保持顺序完全一致")
            n["inputs"] = parents
    return result


DEFAULT = {
    "nodes": [
        {"id": "input", "type": "Input", "shape": [8]},
        {"id": "hidden", "type": "Linear", "inputs": ["input"], "out_features": 32},
        {"id": "activation", "type": "ReLU", "inputs": ["hidden"]},
        {"id": "output", "type": "Linear", "inputs": ["activation"], "out_features": 1},
    ],
    "output": "output",
}


def inspect(graph):
    graph = normalize(graph)
    if not isinstance(graph, dict):
        raise ValueError("网络定义须为对象")
    nodes = graph.get("nodes", [])
    if not isinstance(nodes, list) or not nodes or len(nodes) > 80:
        raise ValueError("网络需要1—80个组件")
    if any(
        not isinstance(n, dict)
        or not isinstance(n.get("id"), str)
        or not isinstance(n.get("type"), str)
        or not isinstance(n.get("inputs", []), list)
        for n in nodes
    ):
        raise ValueError("每个组件需声明字符串id、type与输入数组")
    if any(any(not isinstance(p, str) for p in n.get("inputs", [])) for n in nodes):
        raise ValueError("组件输入必须使用组件ID字符串")
    by_id = {n["id"]: n for n in nodes}
    if len(by_id) != len(nodes) or any(not k or not k.replace("_", "").isalnum() for k in by_id):
        raise ValueError("组件ID必须唯一，使用字母数字下划线")
    result = {}
    order = []
    pending = list(by_id)
    for _ in range(len(nodes) + 1):
        progressed = False
        for key in pending[:]:
            n = by_id[key]
            parents = n.get("inputs", [])
            kind = n["type"]
            if any(p not in by_id for p in parents):
                raise ValueError(f"{key}引用不存在的组件")
            if any(p not in result for p in parents):
                continue
            count = 0
            if kind == "Input":
                shape = n.get("shape", [])
                if (
                    parents
                    or not isinstance(shape, list)
                    or not shape
                    or any(not isinstance(v, int) or isinstance(v, bool) or not 0 < v <= 100000 for v in shape)
                ):
                    raise ValueError(
                        f"Input组件{key}需要shape:[8]这样的正整数数组（不含batch），且不能有inputs；收到字段{sorted(n)}，允许字段{NODE_FIELDS['Input']}"
                    )
                shape = list(shape)
            else:
                if not parents:
                    raise ValueError(
                        f"组件{key}（{kind}）缺少inputs:['上游ID']；收到字段{sorted(n)}，允许字段{NODE_FIELDS[kind]}。若是输入节点，请用type:'Input',shape:[8]"
                    )
                shape = list(result[parents[0]]["shape"])
                if kind not in ("Add", "Concat") and len(parents) != 1:
                    raise ValueError(f"{kind}仅接受一个输入")
                if kind == "Linear":
                    width = n.get("out_features", 0)
                    if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 16384:
                        raise ValueError("Linear输出维度须为1—16384")
                    count = shape[-1] * width + (width if n.get("bias", True) else 0)
                    shape[-1] = width
                elif kind in ("ReLU", "GELU", "Sigmoid", "Tanh", "Identity"):
                    pass
                elif kind == "Dropout":
                    if not 0 <= n.get("p", 0.1) < 1:
                        raise ValueError("Dropout概率应在[0,1)")
                elif kind == "LayerNorm":
                    count = 2 * shape[-1]
                elif kind == "Flatten":
                    shape = [math.prod(shape)]
                elif kind == "Add":
                    if len(parents) < 2 or any(result[p]["shape"] != shape for p in parents):
                        raise ValueError("残差相加需要至少两个形状完全相同的输入")
                elif kind == "Concat":
                    if len(parents) < 2 or any(result[p]["shape"][:-1] != shape[:-1] for p in parents):
                        raise ValueError("拼接只支持最后维度，其他维度须一致")
                    shape[-1] = sum(result[p]["shape"][-1] for p in parents)
                elif kind == "GRU":
                    width = n.get("hidden_size", 32)
                    if (
                        len(shape) != 2
                        or not isinstance(width, int)
                        or isinstance(width, bool)
                        or not 1 <= width <= 4096
                    ):
                        raise ValueError("GRU需要[时间步,特征]输入和有效hidden_size")
                    count = 3 * width * shape[-1] + 3 * width * width + 6 * width
                    shape = [shape[0], width] if n.get("return_sequences", False) else [width]
                else:
                    raise ValueError(f"未支持组件{kind}；自定义代码不假装可编辑标准图")
            if math.prod(shape) > 10_000_000:
                raise ValueError("单样本张量过大")
            result[key] = {"id": key, "type": kind, "shape": shape, "parameters": count, "inputs": parents}
            pending.remove(key)
            order.append(key)
            progressed = True
        if not pending:
            break
        if not progressed:
            raise ValueError("网络组件存在循环")
    output = graph.get("output")
    if output not in result:
        raise ValueError("必须指定有效的网络输出")
    needed = {output}
    for key in reversed(order):
        if key in needed:
            needed.update(result[key]["inputs"])
    disconnected = set(result) - needed
    if disconnected:
        raise ValueError("存在未连接到输出的组件：" + ",".join(sorted(disconnected)))
    return {
        "format": "quantlab.network/1",
        "graph": graph,
        "components": [result[k] for k in order],
        "output_shape": result[output]["shape"],
        "parameter_count": sum(v["parameters"] for v in result.values()),
        "input_ids": [k for k in order if result[k]["type"] == "Input"],
        "batch_dimension": "omitted in displayed shapes",
    }


def build(graph):
    import torch
    from torch import nn

    spec = inspect(graph)
    graph = spec["graph"]
    if spec["parameter_count"] > 10_000_000:
        raise ValueError("基础组件执行器最多支持一千万参数；可查看图结构，执行请使用专用训练模块")

    class ComponentNetwork(nn.Module):
        def __init__(self):
            super().__init__()
            self.layers = nn.ModuleDict()
            shapes = {c["id"]: c["shape"] for c in spec["components"]}
            for n in graph["nodes"]:
                kind = n["type"]
                key = "component_" + n["id"]
                parent = n.get("inputs", [])
                if kind == "Linear":
                    self.layers[key] = nn.Linear(shapes[parent[0]][-1], n["out_features"], bias=n.get("bias", True))
                elif kind == "LayerNorm":
                    self.layers[key] = nn.LayerNorm(shapes[parent[0]][-1])
                elif kind == "GRU":
                    self.layers[key] = nn.GRU(shapes[parent[0]][-1], n.get("hidden_size", 32), batch_first=True)
                elif kind == "Dropout":
                    self.layers[key] = nn.Dropout(n.get("p", 0.1))
                elif kind in ("ReLU", "GELU", "Sigmoid", "Tanh", "Identity"):
                    self.layers[key] = getattr(nn, kind)()
                elif kind == "Flatten":
                    self.layers[key] = nn.Flatten(start_dim=1)

        def forward(self, inputs):
            values = inputs if isinstance(inputs, dict) else {spec["input_ids"][0]: inputs}
            definitions = {n["id"]: n for n in graph["nodes"]}
            for c in spec["components"]:
                key = c["id"]
                kind = c["type"]
                n = definitions[key]
                if kind == "Input":
                    if key not in values or list(values[key].shape[1:]) != c["shape"]:
                        raise ValueError("实际输入张量形状与组件定义不一致：" + key)
                    continue
                parents = [values[p] for p in c["inputs"]]
                if kind == "Add":
                    values[key] = torch.stack(parents).sum(0)
                elif kind == "Concat":
                    values[key] = torch.cat(parents, dim=-1)
                elif kind == "GRU":
                    seq, hidden = self.layers["component_" + key](parents[0])
                    values[key] = seq if n.get("return_sequences", False) else hidden[-1]
                else:
                    values[key] = self.layers["component_" + key](parents[0])
            return values[graph["output"]]

    model = ComponentNetwork()
    if sum(p.numel() for p in model.parameters()) != spec["parameter_count"]:
        raise ValueError("组件参数量与实际模型不一致")
    return model


def code(graph):
    import pprint

    graph = inspect(graph)["graph"]
    return (
        "from quantlab_research.network import build\n\nGRAPH = "
        + pprint.pformat(graph, sort_dicts=False, width=90)
        + "\n\nmodel = build(GRAPH)\n"
    )


def execute(ctx, inputs, params):
    value = inspect(params["graph"])
    value["code"] = code(params["graph"])
    return {"network": value}
