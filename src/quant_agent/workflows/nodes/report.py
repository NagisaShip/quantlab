NODE = {
    "kind": "report.collect",
    "version": "1",
    "title": "固定研究证据",
    "category": "输出",
    "description": "将评价结果固定为可导出的JSON与Markdown，供报告和汇报共用。",
    "inputs": {"report": {"type": "report"}},
    "outputs": {"evidence": {"type": "evidence"}},
    "parameters": {
        "type": "object",
        "properties": {"title": {"type": "string", "default": "研究结果"}},
        "additionalProperties": False,
    },
    "requirements": [],
}


def execute(ctx, inputs, params):
    import json

    report = inputs["report"]
    text = "# " + params["title"] + "\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n"
    return {"evidence": {"title": params["title"], "report": report, "markdown": text}}
