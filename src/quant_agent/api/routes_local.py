"""Local project control surface; disabled in account deployments."""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from ..local import runtime as local_runtime
from ..platform import context
from ..store import _FileLock
from ..workflows.contracts import WorkflowError
from ..workflows.environments import EnvironmentManager, write_json
from ..workflows.service import WorkflowService
from .deps import get_services

router = APIRouter(prefix="/api/local", tags=["本地项目"])
envrouter = APIRouter(prefix="/api/workflows/environment", tags=["研究执行环境"])


def local():
    s = get_services()
    if not s.local:
        raise WorkflowError("此操作仅在显式启用的本地模式可用")
    return s.local


def manager():
    return EnvironmentManager(WorkflowService(get_services()).root)


def admin():
    if not get_services().local and not context.current().is_admin:
        raise WorkflowError("仅部署管理员或本地项目所有者可以更改执行环境")


class PathIn(BaseModel):
    path: str
    mode: str = "read"
    name: str = ""


class EnvironmentIn(BaseModel):
    python: str = ""
    wheelhouse: str = ""
    neural: bool = False


class DataIn(BaseModel):
    name: str
    data: list[dict] | None = Field(default=None, max_length=5000)
    csv_text: str = Field(default="", max_length=2000000)


class ModuleIn(BaseModel):
    kind: str = ""
    version: str = "1"
    id: str = ""


class PreferencesIn(BaseModel):
    close_action: str | None = None
    last_workflow_id: str | None = None


@router.get("/preferences")
def read_preferences():
    return local().preferences()


@router.post("/preferences")
def preferences(payload: PreferencesIn):
    p = local()
    # 整个 read → validate → merge → write 必须在项目本地锁内完成；write_json 的
    # 原子替换只能保证文件不半写，不能防止两个并发字段更新互相覆盖。
    with _FileLock(p.home / "desktop-preferences.lock"):
        current = p.preferences()
        if payload.close_action is not None:
            if payload.close_action not in ("ask", "background", "exit"):
                raise WorkflowError("关窗行为为ask/background/exit")
            current["close_action"] = payload.close_action
        if payload.last_workflow_id is not None:
            from ..workflows.contracts import identifier

            wid = payload.last_workflow_id.strip()
            if wid:
                identifier(wid)
                WorkflowService(get_services()).get(wid)
                current["last_workflow_id"] = wid
            else:
                current.pop("last_workflow_id", None)
        write_json(p.home / "desktop-preferences.json", current)
        return current


@router.post("/shutdown")
def shutdown():
    """请求停止本机项目服务。

    有任务在跑时返回结构化 409（``code=tasks_running`` + tasks），桌面端据此给出
    「隐藏到托盘 / 完成后退出 / 取消」；无任务时仍然返回 200 并延迟关闭。
    """
    p = local()
    tasks = active_tasks(p)
    if tasks:
        return JSONResponse(
            status_code=409,
            content={
                "code": "tasks_running",
                "detail": "有未完成任务，请完成或取消后退出；也可保持后台运行",
                "active_count": len(tasks),
                "tasks": tasks,
            },
        )
    callback = getattr(p, "shutdown", None)
    if not callback:
        raise WorkflowError("此实例由命令行管理，请在原启动终端停止服务")
    threading.Timer(0.5, callback).start()
    return {"stopping": True}


def active_tasks(p) -> list[dict]:
    """列出会阻止“直接退出”的任务，供 /tasks 与 shutdown 409 共用。"""
    s = get_services()
    workspace = p.manifest["workspace_id"]
    rows = s.db.fetchall(
        "SELECT id,kind,state,title,created_at FROM runs "
        "WHERE workspace_id=? AND state IN ('QUEUED','RUNNING','PAUSED','RECOVERING') "
        "ORDER BY created_at ASC",
        (workspace,),
    )
    tasks = [
        {
            "kind": "run",
            "id": r["id"],
            "title": r["title"],
            "state": r["state"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    from ..agents.lab_state import COLLECTION

    for r in s.store.for_workspace(workspace).list(COLLECTION):
        if r.get("status") == "running":
            tasks.append(
                {
                    "kind": "conversation",
                    "id": r["id"],
                    "title": r.get("title"),
                    "state": "running",
                    "created_at": r.get("created_at"),
                }
            )
    jobs = p.home / "store/workflows/management-jobs"
    for path in jobs.glob("*.json") if jobs.exists() else []:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if job.get("state") in ("running", "queued"):
            tasks.append(
                {
                    "kind": "job",
                    "id": job.get("id") or path.stem,
                    "title": job.get("kind"),
                    "state": job.get("state"),
                    "created_at": job.get("started_at"),
                }
            )
    return tasks


def start_job(kind, fn):
    root = manager().root / "management-jobs"
    root.mkdir(parents=True, exist_ok=True)
    key = "job-" + uuid.uuid4().hex[:12]
    path = root / (key + ".json")
    state = {"id": key, "kind": kind, "state": "running", "started_at": time.time()}
    write_json(path, state)

    def work():
        try:
            write_json(path, {**state, "state": "completed", "result": fn(), "finished_at": time.time()})
        except Exception as exc:
            write_json(path, {**state, "state": "failed", "error": str(exc), "finished_at": time.time()})

    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(work), name=key, daemon=True).start()
    return state


@envrouter.get("")
def environment():
    return manager().status()


@envrouter.get("/discover")
def environments():
    admin()
    return manager().discover()


@envrouter.post("/select")
def select_environment(payload: EnvironmentIn):
    admin()
    return manager().select(payload.python)


@envrouter.post("/repair")
def repair_environment(payload: EnvironmentIn):
    admin()
    m = manager()
    return start_job(
        "environment-install",
        lambda: m.repair(base_python=payload.python or None, wheelhouse=payload.wheelhouse, neural=payload.neural),
    )


@envrouter.get("/jobs/{job_id}")
def job(job_id: str):
    from ..workflows.contracts import identifier

    path = manager().root / "management-jobs" / (identifier(job_id) + ".json")
    if not path.is_file():
        raise WorkflowError("当前项目没有该管理任务")
    result = json.loads(path.read_text(encoding="utf-8"))
    log = manager().root / "environment-install.log"
    if log.exists():
        result["log_tail"] = log.read_text(encoding="utf-8", errors="replace")[-3000:]
    return result


@router.get("/info")
def info():
    body = local().info()
    # 追加运行时摘要（解释器 + 受管依赖是否可用），保留 info() 原有字段不变
    body["runtime"] = local_runtime.summary()
    return body


@router.get("/runtime", summary="受管 Python 运行时探测（仅本地模式，只读诊断）")
def runtime(refresh: bool = False):
    """当前后端进程真实使用的解释器与受管依赖诊断。

    只读：不安装依赖、不创建虚拟环境；``refresh=1`` 跳过缓存重新探测。
    非本地部署由 app 层统一 404（不暴露该接口是否存在）。
    """
    if get_services().local is None:
        raise HTTPException(404, "接口不存在")
    return local_runtime.probe(refresh=refresh)


@router.get("/files")
def browse(path: str = "", limit: int = 100):
    return local().browse(path, max(1, min(limit, 200)))


@router.post("/permissions")
def grant(payload: PathIn):
    return local().grant(payload.path, payload.mode)


@router.post("/permissions/revoke")
def revoke(payload: PathIn):
    return local().revoke(payload.path)


@router.get("/files/image")
def image(path: str):
    """在对话/报告中内联展示项目内图片（仅本地模式，只读授权目录）。"""
    target = local().authorize(path)
    if not target.is_file():
        raise WorkflowError("图片文件不存在")
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }.get(target.suffix.lower())
    if mime is None:
        raise WorkflowError("只支持 PNG/JPEG/GIF/WebP/SVG 图片")
    if target.stat().st_size > 20 * 1024 * 1024:
        raise WorkflowError("图片超过 20MB，请压缩后再展示")
    return FileResponse(target, media_type=mime, headers={"Cache-Control": "private, max-age=300"})


@router.get("/files/preview")
def preview(path: str, limit: int = 20, offset: int = 0, encoding: str = "utf-8-sig"):
    """本地文件预览：文本按文本读、表格按表格预览，二进制/办公文件给出可行动说明。"""
    from ..workflows.agent import BINARY_SUFFIXES, TABLE_SUFFIXES, TEXT_READ_LIMIT, read_text_target

    target = local().authorize(path)
    suffix = target.suffix.lower()
    if suffix in BINARY_SUFFIXES:
        raise WorkflowError(
            f"这是二进制/办公文件（{suffix or '无扩展名'}），不能用文本预览；请直接用系统应用打开。"
            "文本文件（.md/.txt/.json/.py/.log 等）可直接预览，表格支持 CSV/TSV/XLSX/XLSM/Parquet/JSONL。"
        )
    if suffix not in TABLE_SUFFIXES:
        return {"kind": "text", **read_text_target(target, offset, TEXT_READ_LIMIT)}
    return WorkflowService(get_services()).preview_file(target, limit=limit, offset=offset, encoding=encoding)


@router.post("/files/document-preview")
def document_preview(payload: PathIn, start: int = 1, limit: int = 4):
    from ..local.documents import render_document

    return render_document(local(), payload.path, start, limit)


@router.get("/files/download")
def download_document(path: str):
    from ..workflows.agent import _authorized_target

    target = _authorized_target(local(), path, write=False)
    if target.suffix.lower() not in (".docx", ".pptx", ".pdf") or not target.is_file():
        raise HTTPException(404, "交付文件不存在")
    return FileResponse(target, filename=target.name, headers={"Cache-Control": "no-store"})


@router.post("/files/import")
def import_file(payload: PathIn):
    return local().import_file(WorkflowService(get_services()), payload.path, payload.mode, payload.name)


@router.post("/files/relocate")
def relocate(payload: PathIn):
    return local().relocate(WorkflowService(get_services()), payload.name, payload.path)


@router.get("/artifacts/{hash}/path", summary="解析本地项目中工件文件的真实路径，供桌面端打开文件夹")
def artifact_path(hash: str):
    """Tauri 的 native_reveal_path / native_open_path 只接受真实本机路径。

    账号模式没有“本机项目目录”的概念，直接 404（前端隐藏入口）；工件不存在同样是 404，
    非法 hash 是 400。返回的路径必须同时位于工件目录与所选项目目录内。
    """
    s = get_services()
    if not s.local:
        raise HTTPException(404, "账号模式不提供本机文件路径；请在本地项目中打开文件夹")
    service = WorkflowService(s)
    digest = str(hash or "").strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise WorkflowError("非法工件引用")
    artifacts = (service.root / "artifacts").resolve()
    path = (artifacts / (digest + ".json")).resolve()
    if not path.is_relative_to(artifacts):
        raise WorkflowError("工件路径越界")
    project = s.local.root
    if not path.is_relative_to(project):
        raise WorkflowError("工件不在本地项目目录内，无法在本机打开")
    if not path.is_file():
        raise HTTPException(404, "本工作区没有该工件文件，可能已被清理")
    return {
        "hash": digest,
        "path": str(path),
        "directory": str(path.parent),
        "name": path.name,
        "bytes": path.stat().st_size,
        "project_root": str(project),
        "mode": "local",
    }


@router.post("/project/open")
def open_project(payload: PathIn):
    local()
    from ..local.launcher import open_project_instance

    return open_project_instance(payload.path)


@router.get("/resources")
def resources():
    from ..local.management import resources

    return resources(local())


@router.get("/tasks")
def tasks():
    p = local()
    s = get_services()
    rows = s.db.fetchall(
        "SELECT id,kind,state,title,created_at FROM runs WHERE workspace_id=? ORDER BY created_at DESC LIMIT 50",
        (context.current_workspace(),),
    )
    from ..agents.lab_state import COLLECTION

    active = active_tasks(p)
    return {
        "runs": rows,
        "conversations": [
            {"id": r["id"], "title": r.get("title"), "status": r.get("status"), "job": r.get("job")}
            for r in s.store.list(COLLECTION)
            if r.get("status") == "running"
        ],
        "active_count": len(active),
        "active": active,
    }


@router.get("/cache")
def cache():
    return local().cache(WorkflowService(get_services()))


@router.post("/cache/clear")
def clear_cache():
    return local().cache(WorkflowService(get_services()), True)


@router.post("/project/export")
def export_project():
    from ..local.management import export_project

    profile = local()
    return start_job("project-export", lambda: export_project(profile))


@router.get("/project/download/{name}")
def download_project(name: str):
    p = local()
    target = (p.home / "transfers" / name).resolve()
    if Path(name).name != name or not target.is_relative_to((p.home / "transfers").resolve()) or not target.is_file():
        raise WorkflowError("项目导出文件不存在")
    return FileResponse(target, filename=name)


@router.get("/library")
def library():
    local()
    from ..local.management import list_library

    return list_library()


@router.post("/library/publish")
def publish_module(payload: ModuleIn):
    local()
    from ..local.management import publish_library

    return publish_library(WorkflowService(get_services()), payload.kind, payload.version)


@router.post("/library/import")
def import_module(payload: ModuleIn):
    local()
    from ..local.management import import_library

    return import_library(WorkflowService(get_services()), payload.id)
