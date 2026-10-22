"""Complete research modules expose a small interface and expandable internals."""

from __future__ import annotations

import pprint

from .catalog import SPECS, array, field, text


def node(key, operation, params=None, x=0, y=0):
    return {
        "id": key,
        "kind": "research." + operation,
        "version": "1",
        "params": params or {},
        "position": {"x": x, "y": y},
    }


def edge(a, out, b, inp):
    return {"id": a + "_" + out + "_" + b + "_" + inp, "source": a, "source_port": out, "target": b, "target_port": inp}


def endpoint(n, p):
    return {"node": n, "port": p}


def parameter(n, p):
    return {"node": n, "parameter": p}


def module(kind, title, description, nodes, edges, inputs, outputs, parameters):
    by_kind = {s["kind"]: s for s in SPECS}
    by_id = {n["id"]: n for n in nodes}
    inports = {k: by_kind[by_id[e["node"]]["kind"]]["inputs"][e["port"]] for k, e in inputs.items()}
    inports = {k: dict(v) for k, v in inports.items()}
    for key in ("train", "validation"):
        if key in inports:
            inports[key]["role"] = key
    outports = {k: dict(by_kind[by_id[e["node"]]["kind"]]["outputs"][e["port"]]) for k, e in outputs.items()}
    # A composite's role inheritance refers to public input names, not internal ports.
    for out in outports.values():
        if out.get("role_from"):
            out["role_from"] = "data" if "data" in inports else "train"
    schema = {k: p[0] for k, p in parameters.items()}
    spec = {
        "kind": "module." + kind,
        "version": "1",
        "title": title,
        "description": description,
        "category": "完整研究模块",
        "level": "module",
        "inputs": inports,
        "outputs": outports,
        "parameters": {
            "type": "object",
            "properties": schema,
            "required": [k for k, v in schema.items() if "default" not in v],
            "additionalProperties": False,
        },
        "requirements": [],
        "subflow": {
            "workflow": {"format": "quantlab.workflow/1", "title": title, "nodes": nodes, "edges": edges},
            "inputs": inputs,
            "outputs": outputs,
            "parameters": {k: p[1] for k, p in parameters.items()},
        },
        "preview": {"kind": "summary", "description": "展开查看内部步骤，参数只映射到明确声明的内部函数"},
    }
    return (
        "NODE = "
        + pprint.pformat(spec, sort_dicts=False, width=110)
        + "\n\ndef execute(ctx, inputs, params):\n    return ctx.run_subflow(inputs, params)\n"
    )


def module_sources():
    clean = module(
        "clean",
        "数据整理与质量研究",
        "按键去重并检查缺失和重复，默认不学习全样本分布。",
        [node("deduplicate", "deduplicate"), node("quality", "quality", x=340)],
        [edge("deduplicate", "data", "quality", "data")],
        {"data": endpoint("deduplicate", "data")},
        {"data": endpoint("deduplicate", "data"), "audit": endpoint("quality", "report")},
        {
            "keys": (array([], title="业务唯一键"), [parameter("deduplicate", "keys"), parameter("quality", "keys")]),
            "keep": (text("first", enum=["first", "last"], title="保留记录"), parameter("deduplicate", "keep")),
        },
    )
    strategy = module(
        "strategy",
        "策略信号与基础回测",
        "历史收益→有效样本→阈值信号→下一观测执行；可展开替换为自定义策略。",
        [
            node("returns", "returns"),
            node("valid", "filter_rows", {"field": "return", "operator": "not_null", "value": ""}, 340),
            node("signal", "threshold_signal", {"field": "return"}, 680),
            node("backtest", "backtest", x=1020),
        ],
        [
            edge("returns", "data", "valid", "data"),
            edge("valid", "data", "signal", "data"),
            edge("signal", "data", "backtest", "data"),
        ],
        {"data": endpoint("returns", "data")},
        {k: endpoint("backtest", k) for k in ("series", "trades", "metrics")},
        {
            "price": (text("close", title="价格字段"), parameter("returns", "price")),
            "entity": (
                text("symbol", title="资产标识"),
                [parameter("returns", "entity"), parameter("backtest", "entity")],
            ),
            "time": (text("date", title="时间字段"), [parameter("returns", "time"), parameter("backtest", "time")]),
            "threshold": (field("number", 0, title="信号阈值"), parameter("signal", "threshold")),
            "fee_bps": (field("number", 10, title="单边换手成本bp", minimum=0), parameter("backtest", "fee_bps")),
        },
    )
    factor = module(
        "factor",
        "因子评价研究",
        "同一因子同时生成IC与分组收益，标签的生成和时间切分由上游明确完成。",
        [node("input", "identity"), node("ic", "ic", x=340), node("groups", "quantile_returns", x=340, y=260)],
        [edge("input", "data", "ic", "data"), edge("input", "data", "groups", "data")],
        {"data": endpoint("input", "data")},
        {"ic": endpoint("ic", "data"), "groups": endpoint("groups", "data")},
        {
            "factor": (text(title="因子字段"), [parameter("ic", "factor"), parameter("groups", "factor")]),
            "label": (text("label", title="未来收益标签"), [parameter("ic", "label"), parameter("groups", "label")]),
            "time": (text("date", title="截面时间"), [parameter("ic", "time"), parameter("groups", "time")]),
            "groups": (field("integer", 5, title="分组数量", minimum=2, maximum=20), parameter("groups", "groups")),
        },
    )
    learning = module(
        "model",
        "模型训练与验证",
        "训练期拟合标准化器，分别应用于训练和验证，再拟合所选模型；测试集另行连接预测。",
        [
            node("train_input", "identity"),
            node("validation_input", "identity", y=260),
            node("scale", "fit_transformer", x=200),
            node("train_data", "apply_transformer", x=340),
            node("validation_data", "apply_transformer", x=340, y=260),
            node("fit", "fit", x=680),
        ],
        [
            edge("train_input", "data", "scale", "data"),
            edge("train_input", "data", "train_data", "data"),
            edge("validation_input", "data", "validation_data", "data"),
            edge("scale", "transformer", "train_data", "transformer"),
            edge("scale", "transformer", "validation_data", "transformer"),
            edge("train_data", "data", "fit", "train"),
            edge("validation_data", "data", "fit", "validation"),
        ],
        {"train": endpoint("train_input", "data"), "validation": endpoint("validation_input", "data")},
        {
            "model": endpoint("fit", "model"),
            "validation": endpoint("fit", "validation"),
            "transformer": endpoint("scale", "transformer"),
        },
        {
            "features": (array(title="特征字段"), [parameter("scale", "fields"), parameter("fit", "features")]),
            "target": (text("label", title="目标字段"), parameter("fit", "target")),
            "algorithm": (
                text("logistic", title="算法", enum=["logistic", "gbdt", "random_forest", "ridge"]),
                parameter("fit", "algorithm"),
            ),
        },
    )
    from .network import DEFAULT

    neural = module(
        "network",
        "神经网络训练研究",
        "组件网络定义→训练与验证；网络图直接驱动实际执行，测试预测单独连接。",
        [node("network", "network"), node("fit", "train_network", x=340)],
        [edge("network", "network", "fit", "network")],
        {"train": endpoint("fit", "train"), "validation": endpoint("fit", "validation")},
        {
            "model": endpoint("fit", "model"),
            "validation": endpoint("fit", "validation"),
            "history": endpoint("fit", "history"),
        },
        {
            "graph": (field("object", DEFAULT, title="网络组件图"), parameter("network", "graph")),
            "features": (array(title="特征字段"), parameter("fit", "features")),
            "target": (text("label", title="目标字段"), parameter("fit", "target")),
            "epochs": (field("integer", 50, title="最大训练轮数", minimum=1, maximum=500), parameter("fit", "epochs")),
        },
    )
    # The public graph parameter is edited with the same visual component editor.
    import ast

    spec = ast.literal_eval(ast.parse(neural).body[0].value)
    spec["view"] = "network"
    neural = (
        "NODE = "
        + pprint.pformat(spec, sort_dicts=False, width=110)
        + "\n\ndef execute(ctx, inputs, params):\n    return ctx.run_subflow(inputs, params)\n"
    )
    native_modules = []
    for operation, title, path in [
        ("native_factor", "QuantLab因子表达式研究", None),
        ("native_portfolio", "QuantLab组合与风险研究", "nav"),
        ("native_execution", "QuantLab执行与成本研究", "episodes"),
    ]:
        base = next(s for s in SPECS if s["operation"] == operation)
        children = [node("evaluate", operation)]
        wires = []
        outputs = {name: endpoint("evaluate", name) for name in base["outputs"]}
        if path:
            children.append(node("table", "select_table", {"path": path}, x=340))
            wires.append(edge("evaluate", "report", "table", "report"))
            outputs["data"] = endpoint("table", "data")
        public = {
            name: (schema, parameter("evaluate", name)) for name, schema in base["parameters"]["properties"].items()
        }
        native_modules.append(
            module(
                operation,
                title,
                base["description"] + " 展开可更改执行函数和报告表格提取。",
                children,
                wires,
                {"data": endpoint("evaluate", "data")},
                outputs,
                public,
            )
        )
    return [clean, strategy, factor, learning, neural, *native_modules]
