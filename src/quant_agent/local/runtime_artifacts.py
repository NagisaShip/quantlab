"""Verified local delivery snapshots; journal references outlive cache retention."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import time
from dataclasses import asdict
from urllib.parse import quote

from ..workflows.contracts import WorkflowError

TOOLS = {
    "local_export_docx",
    "local_export_docx_file",
    "local_export_pptx",
    "local_export_pptx_file",
    "local_preview_document",
}
RETENTION_MS = 30 * 86400000
MAX_BYTES = 64 * 1024 * 1024
KEY = re.compile(r"[0-9a-f]{64}\.(?:docx|pptx|pdf|png)")


def _cache(profile):
    folder = profile.home / "runtime-artifacts"
    if folder.resolve() != profile.home.resolve() / "runtime-artifacts":
        raise WorkflowError("交付快照目录不合法")
    folder.mkdir(exist_ok=True)
    return folder.resolve()


def attach_artifacts(tool_name, result, *, effect_id):
    if tool_name.removeprefix("platform_") not in TOOLS or not isinstance(result, dict) or result.get("error"):
        return result
    from agent_runtime.artifacts import FIELD, ArtifactRef

    from ..api.deps import get_services
    from ..workflows.agent import _atomic_write, _authorized_target

    profile = get_services().local
    if profile is None:
        return result
    paths = [result["path"]] if result.get("path") else []
    paths += [page["path"] for page in result.get("pages", [])[:8] if isinstance(page, dict) and page.get("path")]
    refs = []
    sources = {}
    now = int(time.time() * 1000)
    for path in dict.fromkeys(paths):
        source = _authorized_target(profile, path, write=False)
        if source.suffix.lower() not in {".docx", ".pptx", ".pdf", ".png"} or not source.is_file():
            continue
        if source.stat().st_size > MAX_BYTES:
            raise WorkflowError("交付文件超过 64MB，未建立交付快照")
        with source.open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise WorkflowError("交付文件超过 64MB，未建立交付快照")
        digest = hashlib.sha256(data).hexdigest()
        if str(source) == result.get("path") and result.get("source_hash") and result["source_hash"] != digest:
            raise WorkflowError("预览期间源文件已改变，请重新预览")
        key = digest + source.suffix.lower()
        sources[str(source)] = digest
        if any(ref["key"] == key for ref in refs):
            continue
        target = _cache(profile) / key
        if target.resolve().parent != _cache(profile):
            raise WorkflowError("交付快照路径不合法")
        _atomic_write(target, data)
        refs.append(
            asdict(
                ArtifactRef(
                    key=key,
                    sha256=digest,
                    bytes=len(data),
                    name=source.name,
                    media_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                    source=str(source),
                    expires_at=now + RETENTION_MS,
                )
            )
        )
    enriched = {**result, FIELD: refs}
    # Existing conversation cards can use the immutable snapshot immediately.
    if refs:
        enriched["download_url"] = f"/api/local/runtime-artifacts/{quote(effect_id, safe='')}/{refs[0]['sha256']}"
    if result.get("pages"):
        by_source = sources
        enriched["pages"] = [
            {
                **page,
                "url": f"/api/local/runtime-artifacts/{quote(effect_id, safe='')}/{by_source[page['path']]}",
            }
            if page.get("path") in by_source
            else page
            for page in result["pages"]
        ]
    return enriched


def read_snapshot(profile, ref):
    from ..workflows.agent import _authorized_target

    _authorized_target(profile, ref["source"], write=False)  # revocation still applies
    if not KEY.fullmatch(ref["key"]):
        raise WorkflowError("交付快照标识不合法")
    if ref.get("expires_at") is not None and ref["expires_at"] <= int(time.time() * 1000):
        raise WorkflowError("交付快照已过期；原文件仍保留")
    target = _cache(profile) / ref["key"]
    if target.resolve().parent != _cache(profile) or not target.is_file():
        raise WorkflowError("交付快照不存在；原文件仍保留")
    data = target.read_bytes()
    if len(data) != ref["bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
        raise WorkflowError("交付快照校验失败，请重新生成或预览")
    return data


def prune(profile, *, at=None):
    """Remove only aged content-addressed cache files, never original outputs."""
    at = int(time.time() * 1000) if at is None else at
    folder = _cache(profile)
    removed = 0
    for path in folder.iterdir():
        if KEY.fullmatch(path.name) and path.is_file() and not path.is_symlink() and path.resolve().parent == folder:
            if path.stat().st_mtime_ns // 1000000 + RETENTION_MS <= at:
                path.unlink()
                removed += 1
    return removed
