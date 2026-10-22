"""Versioned research notes with verified links to frozen run evidence."""

from .contracts import WorkflowError


def read(service, workflow_id):
    service.get(workflow_id)
    return service.store.get("workflow_notes", workflow_id) or {
        "id": workflow_id,
        "version": 0,
        "text": "",
        "run_ids": [],
    }


def save(service, workflow_id, text, run_ids, expected_version):
    service.writable()
    if not isinstance(text, str) or len(text) > 100000 or len(run_ids) > 30:
        raise WorkflowError("研究笔记最多10万字符、30个证据运行")
    for rid in run_ids:
        service.inspected(workflow_id, rid)
    from ..store import _FileLock

    # Same process serialization as workflow revisions; store remains workspace scoped.
    with _FileLock(service.root / (workflow_id + ".notes.lock")):
        old = read(service, workflow_id)
        if old["version"] != expected_version:
            raise WorkflowError("笔记已更新，请刷新后合并内容")
        record = {
            "id": workflow_id,
            "version": old["version"] + 1,
            "text": text,
            "run_ids": list(dict.fromkeys(run_ids)),
        }
        service.store.upsert("workflow_notes", record)
        return record
