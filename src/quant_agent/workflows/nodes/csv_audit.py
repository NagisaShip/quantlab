NODE = {
    "kind": "source.csv_audit",
    "version": "1",
    "title": "CSV结构自检",
    "category": "检查",
    "description": "流式统计字段、记录、年份与异常片段，不修改原文件。",
    "inputs": {},
    "outputs": {"report": {"type": "report"}},
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "minLength": 1},
            "date_column": {"type": "string", "default": ""},
        },
        "required": ["file"],
        "additionalProperties": False,
    },
    "file_params": ["file"],
    "requirements": [],
}


def execute(ctx, inputs, params):
    import csv
    from collections import Counter

    csv.field_size_limit(10000000)
    count = 0
    bad = []
    years = Counter()
    with ctx.file(params["file"]).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if params["date_column"] and params["date_column"] not in (reader.fieldnames or []):
            raise ValueError("指定日期字段不存在")
        for row in reader:
            count += 1
            if None in row or any(v is None for v in row.values()):
                if len(bad) < 100:
                    bad.append(reader.line_num)
                continue
            if params["date_column"]:
                years[row[params["date_column"]][:4]] += 1
    return {
        "report": {
            "source": params["file"],
            "rows": count,
            "columns": reader.fieldnames,
            "bad_fragment_lines_first_100": bad,
            "years": dict(years),
            "checks": [{"name": "CSV列数一致", "passed": not bad}],
        }
    }
