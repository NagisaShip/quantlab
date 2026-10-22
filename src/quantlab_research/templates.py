"""Human-readable starter graphs keep full research modules visible by default."""

from .modules import edge, node


def base(title):
    return {
        "format": "quantlab.workflow/1",
        "title": title,
        "nodes": [
            {
                "id": "source",
                "kind": "source.csv",
                "version": "1",
                "params": {"file": "input.csv"},
                "position": {"x": 20, "y": 120},
            }
        ],
        "edges": [],
        "settings": {"seed": 7},
    }


def templates():
    quality = base("数据整理与质量研究")
    quality["nodes"] += [
        {"id": "research", "kind": "module.clean", "version": "1", "params": {}, "position": {"x": 380, "y": 120}}
    ]
    quality["edges"] = [edge("source", "data", "research", "data")]
    strategy = base("策略研究 · 收益与净值图")
    strategy["nodes"] += [
        {"id": "research", "kind": "module.strategy", "version": "1", "params": {}, "position": {"x": 380, "y": 120}},
        node("chart", "chart", {"x": "date", "y": "equity", "title": "策略净值"}, 760, 120),
    ]
    strategy["edges"] = [edge("source", "data", "research", "data"), edge("research", "series", "chart", "data")]
    model = base("模型研究 · 时间切分与冻结测试")
    model["nodes"] += [
        node("split", "temporal_split", {"time": "year", "train_end": "2021", "validation_end": "2023"}, 340, 120),
        {
            "id": "research",
            "kind": "module.model",
            "version": "1",
            "params": {"features": ["feature"], "target": "label"},
            "position": {"x": 680, "y": 40},
        },
        node("test_transform", "apply_transformer", x=1020, y=220),
        node("predict", "predict", x=1360, y=120),
        node("evaluate", "evaluate", x=1700, y=120),
    ]
    model["edges"] = [
        edge("source", "data", "split", "data"),
        edge("split", "train", "research", "train"),
        edge("split", "validation", "research", "validation"),
        edge("split", "test", "test_transform", "data"),
        edge("research", "transformer", "test_transform", "transformer"),
        edge("test_transform", "data", "predict", "data"),
        edge("research", "model", "predict", "model"),
        edge("predict", "data", "evaluate", "data"),
    ]
    neural = base("组件神经网络 · 训练与验证")
    neural["nodes"] += [
        node("split", "temporal_split", {"time": "year", "train_end": "2021", "validation_end": "2023"}, 340, 120),
        {
            "id": "research",
            "kind": "module.network",
            "version": "1",
            "params": {"features": ["x" + str(i) for i in range(1, 9)]},
            "position": {"x": 680, "y": 120},
        },
    ]
    neural["edges"] = [
        edge("source", "data", "split", "data"),
        edge("split", "train", "research", "train"),
        edge("split", "validation", "research", "validation"),
    ]
    return [quality, strategy, model, neural]
