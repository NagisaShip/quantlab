"""Literal card definitions are generated from one human/Agent-readable catalog."""

from __future__ import annotations

import inspect
import pprint

from .network import DEFAULT
from .presentation import engine_schema


def field(kind="string", default=None, *, title="", description="", **extra):
    return {
        "type": kind,
        **({"default": default} if default is not None else {}),
        "title": title,
        "description": description,
        **extra,
    }


def text(default="", **kw):
    return field(default=default, **kw)


def array(default=None, **kw):
    return field("array", default, items={"type": "string"}, minItems=0 if default == [] else 1, **kw)


def port(kind="table", role=None, **kw):
    return {"type": kind, **({"role": role} if role else {}), **kw}


SPECS = []


def add(operation, title, category, description, parameters, inputs=None, outputs=None, requirements=None, **extra):
    properties = {name: {"title": name, **schema} for name, schema in parameters.items()}
    SPECS.append(
        {
            "kind": "research." + operation,
            "version": "1",
            "title": title,
            "category": category,
            "level": "function",
            "description": description,
            "operation": operation,
            "inputs": inputs if inputs is not None else {"data": port()},
            "outputs": outputs or {"data": port()},
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [k for k, v in properties.items() if "default" not in v],
                "additionalProperties": False,
            },
            "requirements": requirements if requirements is not None else ["pandas>=2.2,<4", "numpy>=1.26,<3"],
            "bundled_modules": ["quantlab_research"],
            "resources": {"scope": "local", "rows": "explicit input; no silent sampling"},
            "examples": [
                {
                    "description": description,
                    "parameters": {k: v["default"] for k, v in properties.items() if "default" in v},
                }
            ],
            **extra,
        }
    )


add(
    "rename",
    "字段映射",
    "数据整理",
    "按映射重命名字段，拒绝重名覆盖。",
    {"mapping": field("object", {}, title="原名与新名映射")},
)
add(
    "deduplicate",
    "按键去重",
    "数据整理",
    "按明确的业务键去重，保留第一或最后记录。",
    {"keys": array([], title="唯一键字段"), "keep": text("first", enum=["first", "last"])},
)
add(
    "cast",
    "字段类型转换",
    "数据整理",
    "显式转换number、date或string，转换错误不静默填空。",
    {"mapping": field("object", {}, title="字段类型映射")},
)
add(
    "fill_constant",
    "常量填补",
    "数据整理",
    "用明确常量填缺失，不学习其他年份的数据分布。",
    {"fields": array([], title="待填字段"), "value": field("number", 0, title="填充值")},
)
add(
    "sort",
    "稳定排序",
    "数据整理",
    "按字段稳定排序。",
    {"keys": array(title="排序字段"), "ascending": field("boolean", True)},
)
add(
    "filter_rows",
    "条件筛选",
    "数据整理",
    "只保留满足明确条件的记录。",
    {
        "field": text(title="筛选字段"),
        "operator": text("eq", enum=["eq", "ne", "gt", "ge", "lt", "le", "not_null", "in"]),
        "value": field(default="", title="比较值"),
    },
)
add(
    "join",
    "表格连接",
    "数据整理",
    "按键连接并验证基数，防止重复键放大样本；拒绝空连接键。",
    {
        "keys": array(title="连接键"),
        "how": text("left", enum=["left", "inner", "outer", "right"]),
        "cardinality": text("many_to_one", enum=["one_to_one", "one_to_many", "many_to_one", "many_to_many"]),
    },
    inputs={"left": port(), "right": port()},
)
add(
    "aggregate",
    "分组聚合",
    "数据整理",
    "明确分组字段、数值字段和统计方法。",
    {
        "keys": array(title="分组字段"),
        "values": array(title="数值字段"),
        "method": text("mean", enum=["mean", "sum", "median", "min", "max", "count", "std"]),
    },
)
add(
    "reshape",
    "宽表转长表",
    "数据整理",
    "保留标识列，将其余列转换为变量与数值。",
    {"keys": array([]), "variable": text("variable"), "value": text("value")},
)
add(
    "quality",
    "数据质量检查",
    "数据质量",
    "报告缺失、不同值数量和重复键，不改写数据。",
    {"keys": array([])},
    outputs={"report": port("report")},
)
add(
    "fit_transformer",
    "拟合变换器",
    "数据变换",
    "仅在显式训练输入估计中位数、尺度或截尾阈值。",
    {
        "fields": array(title="拟合字段"),
        "method": text("standardize", enum=["standardize", "impute", "winsorize"]),
        "lower": field("number", 0.01, minimum=0, maximum=1),
        "upper": field("number", 0.99, minimum=0, maximum=1),
    },
    inputs={"data": port(role="train")},
    outputs={"transformer": port("transformer")},
)
add(
    "apply_transformer",
    "应用冻结变换器",
    "数据变换",
    "使用已有变换器处理任意样本，不重新估计分布。",
    {},
    inputs={"data": port(), "transformer": port("transformer")},
)
add(
    "lag",
    "历史滞后特征",
    "时间与样本",
    "按实体时间顺序生成正向滞后，拒绝重复实体时间键。",
    {
        "fields": array(),
        "time": text("date"),
        "entity": text("symbol"),
        "periods": field("integer", 1, minimum=1, maximum=10000),
    },
)
add(
    "temporal_split",
    "时间切分",
    "时间与样本",
    "按有序数值或ISO日期边界切分，三组用途明确。",
    {"time": text("year"), "train_end": text(), "validation_end": text()},
    outputs={k: port(role=k) for k in ["train", "validation", "test"]},
)
add("describe", "描述统计", "统计计量", "计算选定数值字段的描述统计。", {"fields": array()}, outputs={"report": port()})
add(
    "correlation",
    "相关矩阵",
    "统计计量",
    "计算Pearson、Spearman或Kendall相关，不宣称因果。",
    {"fields": array(), "method": text("spearman", enum=["spearman", "pearson", "kendall"])},
    outputs={"report": port()},
)
add(
    "bootstrap_mean",
    "均值Bootstrap区间",
    "统计计量",
    "IID重采样均值区间，时序与聚类数据需另设分组方案。",
    {"field": text(), "draws": field("integer", 500, minimum=100, maximum=10000), "seed": field("integer", 7)},
    outputs={"report": port("report")},
)
add(
    "ols",
    "OLS与固定效应回归",
    "统计计量",
    "可选实体去均值固定效应和HC1标准误，不提供因果识别保证。",
    {"features": array(), "target": text(), "robust": field("boolean", True), "entity": text("")},
    outputs={"report": port("report")},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "scipy>=1.13,<2"],
)
common = {"entity": text("symbol"), "time": text("date")}
add(
    "returns",
    "历史收益率",
    "因子研究",
    "计算过去窗口价格变化，不生成未来标签。",
    {"price": text("close"), **common, "periods": field("integer", 1, minimum=1, maximum=10000)},
)
add(
    "rolling",
    "滚动统计因子",
    "因子研究",
    "使用当前与过去观测计算滚动统计，窗口不足保持缺失。",
    {
        "field": text("return"),
        **common,
        "window": field("integer", 20, minimum=2, maximum=10000),
        "method": text("std", enum=["mean", "std", "sum", "min", "max", "median"]),
    },
)
add(
    "forward_label",
    "未来收益标签",
    "时间与样本",
    "生成未来收益及标签结束时间，用于后续清除跨界样本。",
    {"price": text("close"), **common, "horizon": field("integer", 5, minimum=1, maximum=10000)},
    outputs={"data": port(role="labels")},
)
add(
    "purged_split",
    "清除标签重叠后切分",
    "时间与样本",
    "丢弃标签跨训练或验证边界的样本，不使用测试标签拟合。",
    {"time": text("date"), "label_end": text("label_end"), "train_end": text(), "validation_end": text()},
    outputs={**{k: port(role=k) for k in ["train", "validation", "test"]}, "audit": port("report")},
)
add(
    "cross_section_rank",
    "截面分位排名",
    "因子研究",
    "每个时点独立计算因子排名。",
    {"field": text(), "time": text("date")},
)
add(
    "neutralize",
    "行业去均值",
    "因子研究",
    "每个时点在行业内去均值，不等同多因素回归中性化。",
    {"field": text(), "time": text("date"), "group": text("industry")},
)
add(
    "ic",
    "IC与Rank IC",
    "因子研究",
    "按时点计算因子和未来标签相关，至少三个有效观测。",
    {
        "factor": text(),
        "label": text("label"),
        "time": text("date"),
        "method": text("spearman", enum=["spearman", "pearson"]),
    },
)
add(
    "quantile_returns",
    "因子分组收益",
    "因子研究",
    "按截面因子分组，报告样本数与平均未来收益。",
    {
        "factor": text(),
        "label": text("label"),
        "time": text("date"),
        "groups": field("integer", 5, minimum=2, maximum=20),
    },
)
add(
    "threshold_signal",
    "阈值策略信号",
    "策略回测",
    "将研究因子转换为仓位信号，保持信号与执行分离。",
    {"field": text(), "threshold": field("number", 0), "short": field("boolean", False)},
)
add(
    "backtest",
    "延迟执行基础回测",
    "策略回测",
    "信号延迟一个观测执行，明确换手成本与等权组合假设，不提交真实订单。",
    {
        **common,
        "returns_field": text("return"),
        "signal": text("signal"),
        "fee_bps": field("number", 10, minimum=0),
        "annual_periods": field("integer", 252, minimum=1),
    },
    outputs={"series": port(), "trades": port(), "metrics": port("report")},
)
add(
    "inverse_volatility",
    "波动率倒数配置",
    "组合风险",
    "在明确时点的资产截面计算正权重，权重之和为1。",
    {"asset": text("symbol"), "volatility": text("volatility")},
)
add("drawdown", "净值回撤", "组合风险", "按输入顺序计算高水位回撤，调用前须按时间排序。", {"value": text("equity")})
add(
    "chart",
    "通用图表",
    "图表展示",
    "统计口径与样式独立保存；只改此卡片不会重训上游。",
    {
        "chart_type": text(
            "line", enum=["line", "bar", "area", "scatter", "histogram", "box", "heatmap", "network", "candlestick"]
        ),
        "x": text(""),
        "y": text(""),
        "group": text(""),
        "aggregate": text("none", enum=["none", "mean", "sum", "median", "min", "max", "count"]),
        "title": text("研究图表"),
        "color": text("#176c68"),
        "x_label": text(""),
        "y_label": text(""),
        "log_y": field("boolean", False),
        "grid": field("boolean", True),
        "bins": field("integer", 20, minimum=2, maximum=200),
        "width": field("number", 9, minimum=3, maximum=16),
        "height": field("number", 5, minimum=2, maximum=12),
    },
    outputs={"chart": port("chart")},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "matplotlib>=3.8,<4", "networkx>=3,<4"],
    view="chart",
)
add(
    "network",
    "神经网络组件",
    "神经网络",
    "组件DAG驱动形状、参数量与实际执行，不猜测任意Python网络。",
    {"graph": field("object", DEFAULT)},
    inputs={},
    outputs={"network": port("network")},
    requirements=[],
    view="network",
)
learn_inputs = {"train": port(role="train"), "validation": port(role="validation")}
training = {"features": array(), "target": text("label")}
add(
    "fit",
    "模型拟合",
    "模型研究",
    "显式训练与验证输入，不接收测试用途数据；输出冻结模型。",
    {
        **training,
        "algorithm": text("logistic", enum=["logistic", "gbdt", "random_forest", "ridge"]),
        "C": field("number", 1.0, minimum=0.00001),
        "iterations": field("integer", 100, minimum=1, maximum=1000),
        "leaves": field("integer", 15, minimum=2, maximum=256),
        "depth": field("integer", 8, minimum=1, maximum=100),
        "alpha": field("number", 1.0, minimum=0.00001),
    },
    inputs=learn_inputs,
    outputs={"model": port("model"), "validation": port("report")},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "scikit-learn>=1.5,<2", "joblib>=1,<2"],
)
add(
    "predict",
    "冻结模型预测",
    "模型研究",
    "读取已拟合模型，不在预测数据上重训。",
    {"threshold": field("number", 0.5, minimum=0, maximum=1)},
    inputs={"data": port(), "model": port("model")},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "scikit-learn>=1.5,<2", "joblib>=1,<2"],
)
add(
    "evaluate",
    "模型评价",
    "模型研究",
    "以固定0.5阈值计算基础二分类指标或回归误差，单类AUC不可定义。",
    {
        "target": text("label"),
        "score": text("score"),
        "task": text("classification", enum=["classification", "regression"]),
    },
    outputs={"report": port("report")},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "scikit-learn>=1.5,<2"],
)
add(
    "train_network",
    "组件网络训练",
    "神经网络",
    "训练图中定义的单输入表格二分类网络，按验证损失早停。",
    {
        **training,
        "epochs": field("integer", 50, minimum=1, maximum=500),
        "patience": field("integer", 8, minimum=1, maximum=100),
        "learning_rate": field("number", 0.001, minimum=0.000001, maximum=1),
        "weight_decay": field("number", 0.0001, minimum=0),
    },
    inputs={**learn_inputs, "network": port("network")},
    outputs={"model": port("model"), "validation": port("report"), "history": port()},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "torch>=2.8,<3", "scikit-learn>=1.5,<2"],
    resources={"scope": "local", "device": "CPU baseline", "max_parameters": 10000000},
)


def source(spec):
    literal = {k: v for k, v in spec.items() if k != "operation"}
    return (
        "NODE = "
        + pprint.pformat(literal, width=110, sort_dicts=False)
        + "\n\ndef execute(ctx, inputs, params):\n    from quantlab_research.catalog import invoke\n    return invoke(ctx, inputs, params, "
        + repr(spec["operation"])
        + ")\n"
    )


def card_sources():
    return [source(s) for s in SPECS]


def invoke(ctx, inputs, params, operation):
    from . import charts, finance, learning, network, tables

    if operation == "chart":
        return charts.execute(ctx, inputs, params)
    if operation == "network":
        return network.execute(ctx, inputs, params)
    if operation in ("fit", "predict", "predict_network", "train_network", "evaluate"):
        return getattr(learning, "predict" if operation == "predict_network" else operation)(ctx, inputs, params)
    if operation in ("native_factor", "native_portfolio", "native_execution", "select_table"):
        from . import native

        fn = getattr(native, operation.replace("native_", ""))
        value = fn(**inputs, **params)
        spec = next(s for s in SPECS if s["operation"] == operation)
        return {next(iter(spec["outputs"])): value}
    fn = getattr(tables, operation, None) or getattr(finance, operation, None)
    if fn is None:
        raise ValueError("未知研究操作")
    kwargs = {**inputs, **params}
    if "seed" in inspect.signature(fn).parameters and "seed" not in kwargs:
        kwargs["seed"] = ctx.seed
    result = fn(**kwargs)
    spec = next(s for s in SPECS if s["operation"] == operation)
    if len(spec["outputs"]) == 1:
        return {next(iter(spec["outputs"])): result}
    return result


add("identity", "模块输入", "内部连接", "保留表格和用途，将外部输入连接到内部多个步骤。", {}, level="internal")
for spec in SPECS:
    for meta in spec["outputs"].values():
        if (
            meta["type"] == "table"
            and not meta.get("role")
            and "data" in spec["inputs"]
            and spec["operation"] not in ("correlation", "describe", "ic", "quantile_returns")
        ):
            meta["role_from"] = "data"
    if spec["operation"] == "fit":
        spec["outputs"]["model"]["semantics"] = {"backend": "sklearn"}
    if spec["operation"] == "train_network":
        spec["outputs"]["model"]["semantics"] = {"backend": "torch"}
    if spec["operation"] == "predict":
        spec["inputs"]["model"]["semantics"] = {"backend": "sklearn"}
add(
    "predict_network",
    "组件网络预测",
    "神经网络",
    "加载冻结的组件网络，仅推断不重训。",
    {"threshold": field("number", 0.5, minimum=0, maximum=1)},
    inputs={"data": port(), "model": port("model", semantics={"backend": "torch"})},
    requirements=["pandas>=2.2,<4", "numpy>=1.26,<3", "torch>=2.8,<3"],
)
add(
    "native_factor",
    "QuantLab因子表达式",
    "原有研究能力",
    "与原因子专业页共用同一表达式引擎，支持选择已有表达式或自定义。",
    {"expression": text("returns(20)", title="因子表达式"), **common, "output": text("factor", title="输出字段")},
    level="module",
)
add(
    "native_portfolio",
    "QuantLab组合研究",
    "原有研究能力",
    "与原组合页共用优化与风险引擎；按历史窗口重新估计，不提交订单。",
    {
        "config": field("object", {}, title="组合配置"),
        **common,
        "timeframe": text("1d", title="K线周期"),
        "cost_bps": field("number", 10, minimum=0),
    },
    outputs={"report": port("report")},
    level="module",
)
add(
    "native_execution",
    "QuantLab执行仿真",
    "原有研究能力",
    "与原执行页共用拆单与TCA引擎；此入口由K线估算盘口，报告保留来源限制。",
    {
        "config": field("object", {}, title="执行配置"),
        **common,
        "step_ms": field("integer", 60000, title="K线间隔毫秒", minimum=1),
    },
    outputs={"report": port("report")},
    level="module",
)
add(
    "select_table",
    "从报告提取表格",
    "数据整理",
    "使用明确字段路径提取报告中的表格，不改变统计含义。",
    {"path": text("nav", title="报告字段路径")},
    inputs={"report": port("report")},
)
for spec in SPECS:
    if spec["operation"] in ("native_factor", "predict_network"):
        spec["outputs"]["data"]["role_from"] = "data"
    for name, meta in spec["parameters"].get("properties", {}).items():
        if not meta.get("title") or meta.get("title") == name:
            meta["title"] = {
                "entity": "实体标识字段",
                "time": "时间字段",
                "features": "特征字段",
                "target": "目标字段",
                "fields": "处理字段",
                "method": "方法",
                "algorithm": "算法",
                "train_end": "训练截止时间",
                "validation_end": "验证截止时间",
                "keys": "业务唯一键",
                "field": "字段",
                "value": "值",
                "operator": "条件",
                "seed": "随机种子",
                "horizon": "未来期数",
                "periods": "过去期数",
                "window": "窗口长度",
                "fee_bps": "单边换手成本bp",
                "annual_periods": "每年观测数",
            }.get(name, name)
    if spec["operation"] == "fit":
        spec["parameters"]["properties"]["max_iter"] = field(
            "integer",
            1000,
            title="逻辑回归最大迭代",
            minimum=1,
            maximum=10000,
            visible_when={"algorithm": ["logistic"]},
        )
        for key, choices in {
            "C": ["logistic"],
            "iterations": ["gbdt", "random_forest"],
            "leaves": ["gbdt"],
            "depth": ["random_forest"],
            "alpha": ["ridge"],
        }.items():
            spec["parameters"]["properties"][key]["visible_when"] = {"algorithm": choices}
    if spec["operation"] == "join":
        spec["outputs"]["data"]["role_from"] = ["left", "right"]
    if spec["operation"] == "filter_rows":
        spec["parameters"]["properties"]["value"] = {
            "default": None,
            "widget": "json",
            "title": "比较值（JSON）",
            "description": "数字直接填写；文本加双引号；in 条件填写数组。",
        }


for spec in SPECS:
    if spec["operation"].startswith("native_"):
        spec["level"] = "function"


for spec in SPECS:
    if spec["operation"] in ("native_portfolio", "native_execution"):
        spec["parameters"]["properties"]["config"]["properties"] = engine_schema(
            spec["operation"].replace("native_", "")
        )
        spec["parameters"]["properties"]["config"]["additionalProperties"] = True

for spec in SPECS:
    if spec["operation"] in ("native_portfolio", "native_execution"):
        spec["parameters"]["properties"]["config"]["primary"] = (
            ["optimizer", "risk_model", "lookback", "rebalance"]
            if spec["operation"] == "native_portfolio"
            else ["algo", "horizon", "slices", "style", "episodes"]
        )
