NODE = {
    "kind": "table.year_split",
    "version": "1",
    "title": "按年份固定切分",
    "category": "处理",
    "description": "按标签所属年划分训练、验证、测试，输出端口携带研究用途。",
    "inputs": {"data": {"type": "table"}},
    "outputs": {
        "train": {"type": "table", "role": "train"},
        "validation": {"type": "table", "role": "validation"},
        "test": {"type": "table", "role": "test"},
    },
    "parameters": {
        "type": "object",
        "properties": {
            "year_column": {"type": "string", "default": "year"},
            "train_start": {"type": "integer", "default": 2012},
            "train_end": {"type": "integer", "default": 2021},
            "validation_end": {"type": "integer", "default": 2023},
            "test_end": {"type": "integer", "default": 2024},
        },
        "additionalProperties": False,
    },
    "requirements": [],
}


def execute(ctx, inputs, params):
    a, b, c = [params[k] for k in ("train_end", "validation_end", "test_end")]
    if not params["train_start"] <= a < b < c:
        raise ValueError("训练、验证和测试年份边界必须递增")
    out = {"train": [], "validation": [], "test": []}
    for row in inputs["data"]:
        year = int(str(row[params["year_column"]])[:4])
        if year > c or year < params["train_start"]:
            continue
        out["train" if year <= a else "validation" if year <= b else "test"].append(row)
    if any(not v for v in out.values()):
        raise ValueError("至少一个年度分组为空，不能开展同口径实验")
    return out
