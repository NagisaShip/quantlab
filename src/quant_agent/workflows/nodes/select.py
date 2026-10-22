NODE = {
    "kind": "table.select",
    "version": "1",
    "title": "字段选择与筛选",
    "category": "处理",
    "description": "明确选择字段，可按一个字段值筛选；不隐式修改上游表。",
    "inputs": {"data": {"type": "table"}},
    "outputs": {"data": {"type": "table"}},
    "parameters": {
        "type": "object",
        "properties": {
            "columns": {"type": "array", "items": {"type": "string"}, "default": []},
            "filter_column": {"type": "string", "default": ""},
            "equals": {"type": "string", "default": ""},
        },
        "additionalProperties": False,
    },
    "requirements": [],
}


def execute(ctx, inputs, params):
    data = inputs["data"]
    cols = params["columns"]
    available = set(data[0]) if data else set()
    if set(cols) - available:
        raise ValueError("选择的字段不存在")
    if params["filter_column"] and params["filter_column"] not in available:
        raise ValueError("筛选字段不存在")
    rows = [r for r in data if not params["filter_column"] or str(r.get(params["filter_column"])) == params["equals"]]
    return {"data": [{k: v for k, v in row.items() if not cols or k in cols} for row in rows]}
