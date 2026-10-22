NODE = {
    "kind": "table.check",
    "version": "1",
    "title": "可复用数据规则",
    "category": "检查",
    "description": "检查空表、必需字段、非空列和主键唯一性，可阻断下游。",
    "inputs": {"data": {"type": "table"}},
    "outputs": {"data": {"type": "table"}, "checks": {"type": "report"}},
    "parameters": {
        "type": "object",
        "properties": {
            "required_columns": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
            "non_null": {"type": "array", "items": {"type": "string"}, "default": []},
            "unique_key": {"type": "array", "items": {"type": "string"}, "default": []},
            "block_on_failure": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
    "requirements": [],
}


def execute(ctx, inputs, params):
    rows = inputs["data"]
    checks = [{"name": "非空数据集", "passed": bool(rows)}]
    columns = set(rows[0]) if rows else set()
    for col in params["required_columns"]:
        checks.append({"name": f"字段存在：{col}", "passed": col in columns})
    for col in params["non_null"]:
        checks.append(
            {
                "name": f"非空：{col}",
                "passed": bool(rows) and all(r.get(col) not in (None, "") for r in rows),
            }
        )
    keys = params["unique_key"]
    if keys:
        values = [tuple(str(r.get(k)) for k in keys) for r in rows]
        checks.append(
            {
                "name": "主键存在且唯一",
                "passed": set(keys) <= columns and len(values) == len(set(values)),
            }
        )
    if params["block_on_failure"] and not all(x["passed"] for x in checks):
        raise ValueError("数据规则未通过：" + "；".join(x["name"] for x in checks if not x["passed"]))
    return {
        "data": rows,
        "checks": {"checks": checks, "passed": all(x["passed"] for x in checks)},
    }
