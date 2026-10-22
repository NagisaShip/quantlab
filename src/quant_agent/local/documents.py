"""Render real Office pages to PNG, with authorized, content-addressed previews."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

from ..workflows.contracts import WorkflowError


def office_binary():
    candidates = [shutil.which("soffice"), shutil.which("libreoffice")]
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(variable):
            candidates.append(str(Path(os.environ[variable]) / "LibreOffice/program/soffice.exe"))
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs/LibreOffice/program/soffice.exe"))
    return next((str(p) for p in candidates if p and Path(p).is_file()), None)


def render_document(profile, path: str, start: int = 1, limit: int = 4):
    from ..workflows.agent import _authorized_target

    source = _authorized_target(profile, path, write=False)
    if source.suffix.lower() not in (".docx", ".pptx", ".pdf") or not source.is_file():
        raise WorkflowError("请选择已授权目录中的 DOCX、PPTX 或 PDF 文件")
    if source.stat().st_size > 100 * 1024 * 1024:
        raise WorkflowError("预览文件不能超过 100MB")
    if not 1 <= start or not 1 <= limit <= 8:
        raise WorkflowError("start 从 1 开始，limit 为 1—8")
    try:
        import fitz
    except ImportError as exc:
        raise WorkflowError("页面预览需要 PyMuPDF：请安装 .[deliverables] 后重试") from exc
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    # Authorize every destination before creating it, including symlinked parents.
    folder = _authorized_target(profile, str(source.parent / (".preview-" + digest[:20])), write=True)
    folder.mkdir(parents=True, exist_ok=True)
    pdf = source
    if source.suffix.lower() != ".pdf":
        binary = office_binary()
        if not binary:
            raise WorkflowError("已生成文件；真实页面预览需要 LibreOffice。安装后可重新预览，当前尚未检查排版")
        pdf = _authorized_target(profile, str(folder / "document.pdf"), write=True)
        if not pdf.is_file():
            with tempfile.TemporaryDirectory(prefix="quantlab-office-") as tmp:
                temporary = Path(tmp)
                command = [
                    binary,
                    "-env:UserInstallation=" + (temporary / "profile").as_uri(),
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(temporary),
                    str(source),
                ]
                try:
                    result = subprocess.run(command, capture_output=True, timeout=90, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise WorkflowError("页面渲染失败或超时；文件已保留，可在系统应用中打开") from exc
                converted = temporary / (source.stem + ".pdf")
                if result.returncode or not converted.is_file():
                    raise WorkflowError("LibreOffice 未输出 PDF；文件已保留，尚未检查排版")
                from ..workflows.agent import _atomic_write

                _atomic_write(pdf, converted.read_bytes())
    pages = []
    try:
        with fitz.open(pdf) as document:
            total = len(document)
            if start > total:
                raise WorkflowError(f"起始页超出总页数 {total}")
            for index in range(start - 1, min(total, start - 1 + limit)):
                page = document[index]
                output = _authorized_target(profile, str(folder / f"page-{index + 1}.png"), write=True)
                if not output.is_file():
                    scale = min(1.5, 1800 / max(page.rect.width, page.rect.height))
                    from ..workflows.agent import _atomic_write

                    _atomic_write(output, page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes("png"))
                pages.append(
                    {
                        "page": index + 1,
                        "path": str(output),
                        "url": "/api/local/files/image?path=" + quote(str(output)),
                        "text": page.get_text()[:1200],
                    }
                )
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError("无法读取文档页面，文件可能损坏或已加密") from exc
    return {
        "kind": "document",
        "path": str(source),
        "source_hash": digest,
        "pages": pages,
        "total_pages": total,
        "next_page": start + len(pages) if start + len(pages) <= total else None,
        "rendered": True,
        "visual_review": "pending",
        "renderer": "LibreOffice/PyMuPDF",
    }


def preview_images(tool_name, result):
    """Only the explicit preview tool may supply locally authorized images to the model."""
    if (
        tool_name.removeprefix("platform_") != "local_preview_document"
        or not isinstance(result, dict)
        or not result.get("rendered")
    ):
        return []
    import base64

    from ..api.deps import get_services
    from ..workflows.agent import _authorized_target

    profile = get_services().local
    if not profile:
        return []
    images = []
    for page in result.get("pages", [])[:8]:
        target = _authorized_target(profile, page["path"], write=False)
        if target.suffix.lower() == ".png" and target.stat().st_size <= 5 * 1024 * 1024:
            images.append("data:image/png;base64," + base64.b64encode(target.read_bytes()).decode("ascii"))
    return images
