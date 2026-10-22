NODE = {
    "kind": "source.csv",
    "version": "1",
    "title": "CSV数据源",
    "category": "数据源",
    "description": "读取已导入CSV，保留字符串和代码前导零；异常行明确报错。",
    "inputs": {},
    "outputs": {"data": {"type": "table", "schema": {"type": "array"}}},
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "minLength": 1},
            "encoding": {"type": "string", "default": "utf-8-sig"},
            "max_rows": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000000,
                "default": 100000,
            },
        },
        "required": ["file"],
        "additionalProperties": False,
    },
    "file_params": ["file"],
    "requirements": [],
}


def execute(ctx, inputs, params):
    import csv

    csv.field_size_limit(10000000)
    rows = []
    with ctx.file(params["file"]).open(encoding=params["encoding"], newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV字段为空或重名")
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError(f"CSV第{reader.line_num}行列数不匹配，请先修复源数据")
            rows.append(row)
            if len(rows) > params["max_rows"]:
                raise ValueError("超过配置的最大行数；未静默截断，请调整max_rows或分区读取")
    ctx.log(f"读取{len(rows)}行，{len(reader.fieldnames)}列")
    return {"data": rows}
