NODE = {
    "kind": "source.file",
    "version": "1",
    "title": "本地多格式数据源",
    "category": "数据源",
    "description": "CSV/TSV、Excel、Parquet、JSONL；明确分区范围，不静默截断。支持项目授权的外部引用。",
    "inputs": {},
    "outputs": {"data": {"type": "table"}, "scope": {"type": "report"}},
    "parameters": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "minLength": 1},
            "encoding": {"type": "string", "default": "utf-8-sig"},
            "columns": {"type": "array", "items": {"type": "string"}, "default": []},
            "start_row": {"type": "integer", "minimum": 0, "default": 0},
            "max_rows": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 100000},
            "on_limit": {"type": "string", "enum": ["error", "partition"], "default": "error"},
        },
        "required": ["file"],
        "additionalProperties": False,
    },
    "file_params": ["file"],
    "requirements": ["openpyxl>=3.1", "pyarrow>=16"],
    "bundled_modules": ["quantlab_files"],
}


def execute(ctx, inputs, params):
    from quantlab_files import source

    return source(ctx, inputs, params)
