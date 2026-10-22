NODE = {
    "kind": "table.summary",
    "version": "1",
    "title": "数据概况",
    "category": "评价",
    "description": "统计样本量、字段和缺失数，供人工核验。",
    "inputs": {"data": {"type": "table"}},
    "outputs": {"report": {"type": "report"}},
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    "requirements": [],
}


def execute(ctx, inputs, params):
    rows = inputs["data"]
    columns = sorted({k for r in rows for k in r})
    return {
        "report": {
            "rows": len(rows),
            "columns": columns,
            "missing": {k: sum(r.get(k) in (None, "") for r in rows) for k in columns},
            "sample": rows[:5],
        }
    }
