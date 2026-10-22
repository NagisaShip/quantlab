"""Project transfer, immutable reusable modules, resources and safe cache controls."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path

from ..workflows.contracts import WorkflowError, fingerprint
from ..workflows.environments import write_json


def resources(profile):
    info = {
        "project": profile.manifest["id"],
        "active_runs": profile.active(),
        "disk_free": shutil.disk_usage(profile.root).free,
        "cpu_percent": None,
        "memory": None,
        "gpu": [],
    }
    try:
        import psutil

        p = psutil.Process()
        info.update(
            cpu_percent=psutil.cpu_percent(),
            memory={"used": psutil.virtual_memory().used, "total": psutil.virtual_memory().total},
            process={"pid": p.pid, "rss": p.memory_info().rss, "children": len(p.children(recursive=True))},
        )
    except ImportError:
        info["resource_note"] = "安装psutil后显示CPU与内存详情"
    if tool := shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                [tool, "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            info["gpu"] = [line.split(",") for line in result.stdout.strip().splitlines()]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info


def export_project(profile):
    if profile.active(include_management=False):
        raise WorkflowError("项目有活动任务，请完成或取消后再导出一致的项目快照")
    folder = profile.home / "transfers"
    folder.mkdir(exist_ok=True)
    archive = folder / ("project-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
    excluded = []
    included = []
    omit = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".quantlab-keys",
        "environments",
        "cache",
        "transfers",
        "webview",
        "management-jobs",
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
        paths = []
        for folder_path, directories, filenames in os.walk(profile.root, followlinks=False):
            base = Path(folder_path)
            skipped = [
                d for d in directories if d in omit or (base / d).is_symlink() or (base / d) == profile.home / "global"
            ]
            excluded.extend(str((base / d).relative_to(profile.root)) + "/" for d in skipped)
            directories[:] = [d for d in directories if d not in skipped]
            paths.extend(base / name for name in filenames)
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(profile.root):
                excluded.append(str(path.relative_to(profile.root)))
                continue
            relative = path.relative_to(profile.root)
            if not path.is_file():
                continue
            if (
                any(part in omit for part in relative.parts)
                or relative.parts[:2] == (".quantlab", "global")
                or path.name.startswith(".env")
                or "secret" in path.name.lower()
                or path.suffix in (".key", ".pem")
                or path.name
                in ("instance.json", "instance.lock", "environment.json", "permissions.json", "launcher.log")
                or path.name.endswith(("-wal", "-shm"))
            ):
                excluded.append(str(relative))
                continue
            if path.suffix in (".sqlite", ".sqlite3", ".db"):
                temporary = folder / (path.name + ".backup")
                source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
                dest = sqlite3.connect(temporary)
                try:
                    source.backup(dest)
                    tables = {r[0] for r in dest.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for table in ("sessions", "user_totp", "totp_recovery_codes", "login_attempts"):
                        if table in tables:
                            dest.execute("DELETE FROM " + table)
                    dest.commit()
                finally:
                    dest.close()
                    source.close()
                output.write(temporary, relative.as_posix())
                temporary.unlink()
            else:
                output.write(path, relative.as_posix())
            included.append(relative.as_posix())
        output.writestr(
            "QUANTLAB-TRANSFER.json",
            json.dumps(
                {
                    "format": "quantlab.project-transfer/1",
                    "project_id": profile.manifest["id"],
                    "included": included,
                    "excluded": excluded,
                    "note": "不含环境、密钥和模型服务配置；解压后选择文件夹，外部引用需重新授权/定位",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return {
        "file": str(archive),
        "name": archive.name,
        "bytes": archive.stat().st_size,
        "included": len(included),
        "excluded": excluded,
    }


def library_root():
    base = Path(os.environ.get("QUANTLAB_LIBRARY") or Path.home() / ".quantlab" / "module-library").resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base


def publish_library(service, kind, version):
    definition = service.registry().get(kind, str(version))
    pending = [definition]
    definitions = {}
    while pending:
        d = pending.pop()
        key = d["kind"] + "@" + str(d["version"])
        if key in definitions:
            continue
        definitions[key] = {
            "kind": d["kind"],
            "version": str(d["version"]),
            "code": d["code"],
            "code_hash": d["code_hash"],
        }
        for node in d.get("subflow", {}).get("workflow", {}).get("nodes", []):
            pending.append(service.registry().get(node["kind"], str(node["version"])))
    files = service.definition_sources(kind, str(version))["files"]
    payload = {
        "format": "quantlab.module-library/1",
        "kind": kind,
        "version": str(version),
        "title": definition["title"],
        "definitions": list(definitions.values()),
        "sources": files,
    }
    identity = fingerprint(payload)
    path = library_root() / (identity + ".json")
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != payload:
        raise WorkflowError("模块库指纹冲突")
    if not path.exists():
        write_json(path, payload)
    return {"id": identity, "kind": kind, "version": str(version), "title": definition["title"], "immutable": True}


def list_library():
    result = []
    for path in library_root().glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        result.append({"id": path.stem, **{k: item[k] for k in ("kind", "version", "title")}})
    return {"modules": result}


def import_library(service, identity):
    import sys

    if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise WorkflowError("非法模块库指纹")
    payload = json.loads((library_root() / (identity + ".json")).read_text(encoding="utf-8"))
    if fingerprint(payload) != identity:
        raise WorkflowError("模块库内容指纹不符")
    # Reject imports whose bundled platform helper code differs from this release.
    installed = Path(__file__).resolve().parents[2]
    for f in payload["sources"]:
        if f["path"].split("/")[0] in ("quantlab_research", "quantlab_files"):
            current = (installed / f["path"]).resolve()
            if (
                not current.is_relative_to(installed)
                or not current.is_file()
                or current.read_text(encoding="utf-8") != f["code"]
            ):
                raise WorkflowError("模板依赖的内置函数实现版本不同，请使用原发行版或显式升级模板：" + f["path"])
    sources = service.services.home / "modules"
    for f in payload["sources"]:
        if f["path"].startswith("cards/"):
            continue
        # Platform modules are shared by the installed runtime; only user packages are installed here.
        if f["path"].split("/")[0] in ("quant_agent", "quantlab_research", "quantlab_files"):
            continue
        target = (sources / f["path"]).resolve()
        if not target.is_relative_to(sources.resolve()) or target.suffix != ".py":
            raise WorkflowError("非法模块源码路径")
        if target.exists() and target.read_text(encoding="utf-8") != f["code"]:
            raise WorkflowError("同名实现包已存在不同代码，请为包改名后发布")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["code"], encoding="utf-8")
    if str(sources) not in sys.path:
        sys.path.append(str(sources))
    registry = service.registry()
    for item in payload["definitions"]:
        registry.add(item["code"])
    for item in payload["definitions"]:
        if not registry.get(item["kind"], item["version"]).get("builtin"):
            service.publish_card(item["code"])
    return {"kind": payload["kind"], "version": payload["version"], "id": identity, "imported": True}
