"""One verified artifact presentation contract for GUI and Agent conversations."""

from __future__ import annotations

import time
from urllib.parse import urlencode

from .contracts import WorkflowError, identifier
from .runtime import Runner

#: 工作区级固定成果集合；记录本身不随缓存清理删除，只能显式取消固定。
PINS = "artifact_pins"
_HEX = frozenset("0123456789abcdef")


def preview(service, digest, offset=0, limit=20):
    if (
        isinstance(offset, bool)
        or isinstance(limit, bool)
        or not isinstance(offset, int)
        or not isinstance(limit, int)
        or offset < 0
        or not 1 <= limit <= 100
    ):
        raise WorkflowError("offset须非负，limit须为1—100")
    value = Runner(service.root, service.registry()).read({"hash": digest})
    total = len(value) if isinstance(value, list) else None
    if total is not None and offset > total:
        raise WorkflowError(f"offset超出范围；total={total}")
    files = []

    def scan(item, label="研究文件", depth=0):
        if depth > 12 or len(files) >= 100:
            return
        if isinstance(item, dict):
            if isinstance(item.get("file"), str) and isinstance(item.get("sha256"), str):
                path = (service.files / item["file"]).resolve()
                if path.is_relative_to(service.files.resolve()) and path.is_file():
                    friendly = path.name
                    if len(path.stem) == 64 and all(c in "0123456789abcdef" for c in path.stem):
                        friendly = (
                            (label.upper() + " 图表")
                            if path.suffix.lower() in (".png", ".svg")
                            else label + path.suffix
                        )
                    files.append(
                        {
                            "file": item["file"],
                            "name": item.get("name") or friendly,
                            "sha256": item["sha256"],
                            "bytes": path.stat().st_size,
                            "image": path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"),
                            "url": "/api/workflows/files/download?"
                            + urlencode(
                                {
                                    "name": item["file"],
                                    "filename": (item.get("name") or friendly)
                                    + ("" if (item.get("name") or friendly).endswith(path.suffix) else path.suffix),
                                }
                            ),
                        }
                    )
            else:
                for key, child in item.items():
                    scan(child, str(key), depth + 1)
        elif isinstance(item, list):
            for child in item:
                scan(child, label, depth + 1)

    # Scan only the displayed page for tables; file bundles use the full bounded tree.
    shown = value[offset : offset + limit] if total is not None else value
    scan(shown)
    return {
        "format": "quantlab.artifact-view/1",
        "artifact_hash": digest,
        "value": shown,
        "total": total,
        "offset": offset,
        "truncated": total is not None and offset + limit < total,
        "next_offset": offset + limit if total is not None and offset + limit < total else None,
        "files": files,
        "download": f"/api/workflows/artifacts/{digest}/download",
    }


def listing(service, workflow_id="", run_id="", query="", offset=0, limit=40):
    from ..platform.context import current_workspace

    if offset < 0 or not 1 <= limit <= 100:
        raise WorkflowError("分页范围不合法")
    records = service.workflows()
    titles = {r["id"]: r["title"] for r in records}
    definitions = service.registry().definitions
    runs = (
        [service.services.runs.require(run_id, current_workspace())]
        if run_id
        else service.services.runs.list(current_workspace(), limit=500)
    )
    rows = []
    for run in runs:
        manifest = run.get("manifest", {})
        wid = manifest.get("workflow_id")
        if not wid or (workflow_id and wid != workflow_id):
            continue
        report = service.run_report(run["id"])
        if not report:
            continue
        names = {
            n["id"]: n.get("title") or definitions.get((n["kind"], str(n["version"])), {}).get("title", n["kind"])
            for n in report.get("workflow", {}).get("nodes", [])
        }
        for nid, node in report.get("nodes", {}).items():
            for port, ref in node.get("outputs", {}).items():
                title = names.get(nid, nid) + " · " + port
                if query.lower() not in (title + " " + titles.get(wid, "") + " " + ref["type"]).lower():
                    continue
                rows.append(
                    {
                        **ref,
                        "title": title,
                        "node_id": nid,
                        "port": port,
                        "workflow_id": wid,
                        "workflow_title": titles.get(wid, wid),
                        "revision": manifest.get("revision"),
                        "run_id": run["id"],
                        "mode": report.get("mode"),
                        "status": node.get("status"),
                        "created_at": run.get("created_at"),
                        "origin": manifest.get("origin", "gui"),
                    }
                )
    if not workflow_id and not run_id:
        for row in service.store.list("workflow_preview_artifacts"):
            if query.lower() in (row["title"] + " " + row["type"]).lower():
                rows.append(row)
    rows.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return {
        "artifacts": rows[offset : offset + limit],
        "total": len(rows),
        "next_offset": offset + limit if offset + limit < len(rows) else None,
    }


# ---- P3 固定成果与版本历史 ----


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        raise WorkflowError("非法工件引用")
    return value


def _version_key(workflow_id, node_id, port):
    """版本键 = workflow_id:node_id:port；三段都复用工作流标识规则。"""
    return ":".join(identifier(part) for part in (workflow_id, node_id, port))


def _parse_version_key(value):
    parts = str(value or "").split(":")
    if len(parts) != 3:
        raise WorkflowError("version_key须为workflow_id:node_id:port")
    return tuple(identifier(part) for part in parts)


def _pins(service):
    return service.store.list(PINS)


def _evidence(service, run_id, workflow_id, node_id, port):
    """复用列表口径校验证据：运行属于当前工作区、workflow 一致、报告里有该输出。"""
    from ..platform.context import current_workspace

    run = service.services.runs.require(run_id, current_workspace())
    manifest = run.get("manifest") or {}
    if manifest.get("workflow_id") != workflow_id:
        raise WorkflowError("运行与工作流不一致：workflow_id须等于该运行manifest里的workflow_id")
    report = service.run_report(run["id"])
    if not report:
        raise WorkflowError("该运行没有运行报告，不能固定产物")
    node = (report.get("nodes") or {}).get(node_id)
    ref = ((node or {}).get("outputs") or {}).get(port)
    if not isinstance(ref, dict) or not ref.get("hash"):
        raise WorkflowError("该运行报告里没有此节点端口的产物")
    return run, ref


def pin_create(service, body):
    """固定一个真实出现在运行报告里的产物；同一(工作区, hash, version_key)幂等。"""
    from ..platform.context import current
    from ..store import _FileLock

    service.writable()
    digest = _digest(body.get("hash"))
    run_id = str(body.get("run_id") or "")
    workflow_id = str(body.get("workflow_id") or "")
    node_id = str(body.get("node_id") or "")
    port = str(body.get("port") or "")
    key = _version_key(workflow_id, node_id, port)
    _run, ref = _evidence(service, run_id, workflow_id, node_id, port)
    if ref.get("hash") != digest:
        raise WorkflowError("hash与运行报告中的产物不一致，可能伪造或已过期")
    actual_type = ref.get("type") or ""
    declared_type = str(body.get("type") or "")
    if declared_type and declared_type != actual_type:
        raise WorkflowError("type与运行报告中的产物类型不一致")
    if not (service.root / "artifacts" / (digest + ".json")).is_file():
        raise WorkflowError("该工件文件不存在或已被清理，不能固定")
    with _FileLock(service.root / "artifact_pins.lock"):
        for row in _pins(service):
            if row.get("hash") == digest and row.get("version_key") == key:
                return row
        title = str(body.get("title") or "")[:200].strip() or (node_id + " · " + port)
        pin = service.store.upsert(
            PINS,
            {
                "hash": digest,
                "run_id": run_id,
                "workflow_id": workflow_id,
                "node_id": node_id,
                "port": port,
                "title": title,
                "type": actual_type,
                "note": str(body.get("note") or "")[:2000],
                "version_key": key,
                "pinned_at": time.time(),
                "pinned_by": current().user_id or current().user_name or "system",
            },
        )
    return pin


def pins(service, workflow_id="", version_key="", limit=100):
    """只返回当前工作区的固定成果，按固定时间倒序。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise WorkflowError("limit须为1—200")
    if workflow_id:
        identifier(workflow_id)
    if version_key:
        _parse_version_key(version_key)
    rows = [
        row
        for row in _pins(service)
        if (not workflow_id or row.get("workflow_id") == workflow_id)
        and (not version_key or row.get("version_key") == version_key)
    ]
    rows.sort(key=lambda r: r.get("pinned_at") or r.get("created_at") or 0, reverse=True)
    return {"pins": rows[:limit], "total": len(rows)}


def pin_delete(service, pin_id):
    """取消固定；集合按工作区隔离，其他工作区的 pin 在这里不可见也不可删。"""
    service.writable()
    pin_id = str(pin_id or "")
    if not 1 <= len(pin_id) <= 80:
        raise WorkflowError("固定记录不存在")
    if not service.store.delete(PINS, pin_id):
        raise WorkflowError("本工作区没有该固定记录")
    return {"deleted": True, "id": pin_id}


def _key_for_hash(service, digest):
    """通过本工作区运行报告反查 hash 所属的 version_key。"""
    from ..platform.context import current_workspace

    runs = service.services.runs.list(current_workspace(), kind="workflow", limit=500)
    for run in runs:
        report = service.run_report(run["id"])
        if not report:
            continue
        workflow_id = (run.get("manifest") or {}).get("workflow_id") or ""
        if not workflow_id:
            continue
        for node_id, node in (report.get("nodes") or {}).items():
            for port, ref in ((node or {}).get("outputs") or {}).items():
                if isinstance(ref, dict) and ref.get("hash") == digest:
                    return _version_key(workflow_id, node_id, port)
    return ""


def versions(service, version_key="", digest="", limit=100):
    """同一 version_key（workflow_id:node_id:port）的历史运行产物，按时间倒序。"""
    from ..platform.context import current_workspace

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise WorkflowError("limit须为1—200")
    if digest:
        digest = _digest(digest)
    key = str(version_key or "")
    if key:
        _parse_version_key(key)
    elif not digest:
        raise WorkflowError("需要version_key或工件hash")
    if digest:
        resolved = _key_for_hash(service, digest)
        if not resolved:
            # 运行可能已被清理，但固定记录仍保存了版本键，优先用它。
            for row in _pins(service):
                if row.get("hash") == digest and row.get("version_key"):
                    resolved = row["version_key"]
                    break
        if key and resolved and key != resolved:
            raise WorkflowError("工件hash与给定version_key不一致")
        key = resolved or key
        if not key:
            return {
                "version_key": None,
                "versions": [],
                "total": 0,
                "reason": "本工作区没有引用该工件的运行报告；可直接用version_key查询历史版本",
            }
    workflow_id, node_id, port = _parse_version_key(key)
    pinned = {
        row.get("hash"): row.get("pinned_at")
        for row in _pins(service)
        if row.get("version_key") == key and row.get("hash")
    }
    rows = []
    for run in service.services.runs.list(current_workspace(), kind="workflow", limit=500):
        manifest = run.get("manifest") or {}
        if manifest.get("workflow_id") != workflow_id:
            continue
        report = service.run_report(run["id"])
        if not report:
            continue
        node = (report.get("nodes") or {}).get(node_id) or {}
        ref = (node.get("outputs") or {}).get(port)
        if not isinstance(ref, dict) or not ref.get("hash"):
            continue
        item_hash = ref["hash"]
        rows.append(
            {
                "hash": item_hash,
                "type": ref.get("type"),
                "role": ref.get("role"),
                "bytes": ref.get("bytes"),
                "run_id": run["id"],
                "workflow_id": workflow_id,
                "node_id": node_id,
                "port": port,
                "version_key": key,
                "revision": manifest.get("revision"),
                "created_at": run.get("created_at"),
                "status": node.get("status"),
                "mode": report.get("mode"),
                "origin": manifest.get("origin", "gui"),
                "pinned": item_hash in pinned,
                "pinned_at": pinned.get(item_hash),
                "available": (service.root / "artifacts" / (item_hash + ".json")).is_file(),
            }
        )
    rows.sort(key=lambda r: (r.get("created_at") or 0, r["run_id"]), reverse=True)
    return {
        "version_key": key,
        "workflow_id": workflow_id,
        "node_id": node_id,
        "port": port,
        "versions": rows[:limit],
        "total": len(rows),
    }
