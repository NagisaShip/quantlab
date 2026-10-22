"""Templates are editable definitions, not a second execution path."""

from .contracts import FORMAT


def csv_research():
    nodes = [
        {
            "id": "source",
            "kind": "source.csv",
            "version": "1",
            "params": {"file": "input.csv"},
            "position": {"x": 40, "y": 80},
        },
        {
            "id": "check",
            "kind": "table.check",
            "version": "1",
            "params": {},
            "position": {"x": 350, "y": 80},
        },
        {
            "id": "summary",
            "kind": "table.summary",
            "version": "1",
            "params": {},
            "position": {"x": 660, "y": 80},
        },
        {
            "id": "report",
            "kind": "report.collect",
            "version": "1",
            "params": {},
            "position": {"x": 970, "y": 80},
        },
    ]
    edges = [
        {"id": f"e{i}", "source": a, "source_port": ap, "target": b, "target_port": bp}
        for i, (a, ap, b, bp) in enumerate(
            [
                ("source", "data", "check", "data"),
                ("check", "data", "summary", "data"),
                ("summary", "report", "report", "report"),
            ]
        )
    ]
    return {
        "format": FORMAT,
        "title": "CSV数据研究",
        "nodes": nodes,
        "edges": edges,
        "settings": {"seed": 7},
    }


def classification():
    flow = csv_research()
    flow["title"] = "年度分类实验"
    flow["nodes"] = [
        {
            "id": "source",
            "kind": "source.csv",
            "version": "1",
            "params": {"file": "input.csv"},
            "position": {"x": 0, "y": 100},
        },
        {
            "id": "split",
            "kind": "table.year_split",
            "version": "1",
            "params": {},
            "position": {"x": 300, "y": 100},
        },
        {
            "id": "train",
            "kind": "model.logistic",
            "version": "1",
            "params": {"features": ["feature"]},
            "position": {"x": 620, "y": 0},
        },
        {
            "id": "predict",
            "kind": "model.predict",
            "version": "1",
            "params": {},
            "position": {"x": 930, "y": 100},
        },
        {
            "id": "evaluate",
            "kind": "classification.evaluate",
            "version": "1",
            "params": {},
            "position": {"x": 1240, "y": 100},
        },
        {
            "id": "report",
            "kind": "report.collect",
            "version": "1",
            "params": {},
            "position": {"x": 1550, "y": 100},
        },
    ]
    flow["edges"] = [
        {"id": f"e{i}", "source": a, "source_port": ap, "target": b, "target_port": bp}
        for i, (a, ap, b, bp) in enumerate(
            [
                ("source", "data", "split", "data"),
                ("split", "train", "train", "train"),
                ("split", "validation", "train", "validation"),
                ("split", "test", "predict", "data"),
                ("train", "model", "predict", "model"),
                ("predict", "predictions", "evaluate", "predictions"),
                ("evaluate", "report", "report", "report"),
            ]
        )
    ]
    return flow
