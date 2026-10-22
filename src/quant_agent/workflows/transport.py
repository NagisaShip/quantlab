"""Keep workflow tool messages valid JSON under the conversation text budget."""

import json


def tool_text(name, value, limit=6000):
    if name.removeprefix("platform_") == "local_preview_document" and isinstance(value, dict):
        # Keep page paths and pagination valid even when extracted page text is long.
        value = {**value, "pages": [{k: v for k, v in page.items() if k != "text"} for page in value.get("pages", [])]}
    view = value.get("workflow_view") if isinstance(value, dict) else None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if not name.removeprefix("platform_").startswith("workflow_"):
        return text[:limit]
    if isinstance(value, dict):
        # The structured chat payload travels separately from the LLM transcript.
        value = {k: v for k, v in value.items() if k not in ("workflow_view", "artifact_view", "task_view")}
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    brief = {
        k: value[k]
        for k in (
            "id",
            "kind",
            "version",
            "artifact_hash",
            "revision",
            "created",
            "state",
            "status",
            "error",
            "ok",
            "checks",
            "total",
            "next_cursor",
            "next_offset",
            "code_hash",
            "sha256",
            "submitted",
            "planned",
            "errors",
        )
        if isinstance(value, dict) and k in value
    }
    if isinstance(value, dict):
        if isinstance(value.get("runs"), list):
            brief["runs"] = [
                {"name": row.get("name"), "run": {k: row.get("run", {}).get(k) for k in ("id", "state")}}
                for row in value["runs"]
            ]
        if isinstance(value.get("variants"), list):
            brief["variants"] = [{k: row[k] for k in ("name", "updates") if k in row} for row in value["variants"]]
        if isinstance(value.get("workflow"), dict):
            brief["workflow"] = {k: value["workflow"][k] for k in ("id", "revision", "title") if k in value["workflow"]}
        if isinstance(value.get("run"), dict):
            report = value["run"]
            brief["run"] = {
                "id": report.get("id"),
                "status": report.get("status"),
                "node_ids": list(report.get("nodes", {})),
            }
        if isinstance(value.get("node"), dict):
            brief["node"] = {
                k: value["node"][k] for k in ("status", "error", "outputs", "child_run_id") if k in value["node"]
            }
            brief["node_id"] = value.get("node_id")
        if view:
            brief["workflow_id"] = view["workflow_id"]
            brief["run_id"] = view.get("run_id")
    brief.update(
        truncated=True,
        recovery="结果超过对话文本预算，未返回残缺JSON。用目录next_cursor、源码next_offset、workflow_inspect(node_id=...)或workflow_artifact(offset,limit)逐段读取；完整结构在工作流页面。",
    )
    output = json.dumps(brief, ensure_ascii=False, default=str)
    if len(output) > limit:
        output = json.dumps(
            {"truncated": True, "preview": text[: limit - 400], "recovery": brief["recovery"]}, ensure_ascii=False
        )
        # JSON escaping can expand a preview, so shrink until it fits.
        while len(output) > limit:
            item = json.loads(output)
            item["preview"] = item["preview"][: len(item["preview"]) // 2]
            output = json.dumps(item, ensure_ascii=False)
    return output
