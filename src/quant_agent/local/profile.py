"""One selected folder owns one independent local research instance."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path

from ..workflows.contracts import WorkflowError
from ..workflows.environments import write_json
from ..workflows.files import digest, references

DISABLED_PREFIXES = (
    "/api/community",
    "/api/collab",
    "/api/groups",
    "/api/issues",
    "/api/inbox",
    "/api/public",
    "/api/social",
    "/api/invites",
    "/api/members",
    "/api/admin/users",
    "/api/admin/invites",
    "/api/bot/hook",
)
DEFAULT_PREFERENCES = {"close_action": "ask"}


def read_preferences(home: Path) -> dict:
    """Read desktop preferences without discarding fields written by newer versions."""
    path = home / "desktop-preferences.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {**DEFAULT_PREFERENCES, **value}


DISABLED_PAGES = [
    "square",
    "post",
    "compose",
    "groups",
    "inbox",
    "issues",
    "profile",
    "favorites",
    "settings/account",
    "settings/members",
    "settings/workspace",
    "settings/feedback",
]


def enabled():
    return os.environ.get("QUANTLAB_MODE") == "local"


class LocalProject:
    def __init__(self, services):
        self.services = services
        self.home = services.home.resolve()
        self.root = Path(os.environ.get("QUANTLAB_PROJECT") or self.home.parent).resolve()
        if self.home != self.root / ".quantlab":
            raise WorkflowError("本地模式数据目录须为所选项目/.quantlab，不能复用账号版数据目录")
        self.permissions = self.home / "permissions.json"
        self.manifest_path = self.home / "project.json"
        # 首次打开空项目：manifest 还不存在。桌面壳据此决定落在本地项目设置页还是研究工作区，
        # 必须由后端在初始化前记录，而不是由壳层猜文件是否存在（后端启动时会创建 project.json）。
        self.first_run = not self.manifest_path.exists()
        if self.manifest_path.exists():
            self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        else:
            if services.db.scalar("SELECT COUNT(*) FROM users"):
                raise WorkflowError("所选目录已有账号版数据库，请选择新的本地项目目录")
            identity = services.auth.register(username="local", name="本地研究者", password=secrets.token_urlsafe(40))
            self.manifest = {
                "format": "quantlab.local-project/1",
                "id": uuid.uuid4().hex,
                "name": self.root.name,
                "user_id": identity["user_id"],
                "workspace_id": identity["workspace_id"],
                "created_at": time.time(),
            }
            write_json(self.manifest_path, self.manifest)
        if not self.permissions.exists():
            write_json(self.permissions, [{"path": str(self.root), "mode": "write", "source": "selected project"}])
        services.db.execute("UPDATE workspaces SET name=? WHERE id=?", (self.root.name, self.manifest["workspace_id"]))

    def preferences(self):
        return read_preferences(self.home)

    def info(self):
        from ..platform import buildinfo

        return {
            "mode": "local",
            "first_run": self.first_run,
            "project": self.manifest,
            "root": str(self.root),
            "home": str(self.home),
            "permissions": self.grants(),
            "disabled_pages": DISABLED_PAGES,
            "account_features": False,
            "upload_byte_limit": None,
            "preferences": self.preferences(),
            "interpretation": "文件大小无统一上限；预览和计算使用明确的行数与内存约束",
            **buildinfo.info(),
        }

    def grants(self):
        return json.loads(self.permissions.read_text(encoding="utf-8"))

    def resolve_path(self, path) -> Path:
        """把用户/Agent 给的路径解析成绝对路径。

        相对路径必须相对本地项目根解析：后端 cwd 是 QuantLab 仓库，按 cwd 解析会把
        `验收输入/关键证据摘要.md` 拼成 `<仓库>/验收输入/...`，随后被授权检查误判为越界。
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve()

    def authorize(self, path, *, write=False):
        target = self.resolve_path(path)
        for grant in self.grants():
            if (not write or grant["mode"] == "write") and target.is_relative_to(Path(grant["path"]).resolve()):
                return target
        raise WorkflowError(
            "目录未授权："
            + str(target)
            + "。请改用绝对路径，或先在「本地项目 → 文件与目录权限」把该目录添加为"
            + ("可写输出目录" if write else "只读数据目录")
            + "；读取 .md/.txt/.json/.py 等文本文件可用 local_read_text"
            + ("（写入文本用 local_write_text）" if write else "，表格预览支持 CSV/TSV/XLSX/Parquet/JSONL")
        )

    def grant(self, path, mode):
        target = self.resolve_path(path)
        if mode not in ("read", "write") or not (target.is_dir() or (mode == "read" and target.is_file())):
            raise WorkflowError("只读可授权文件或目录，可写必须选择文件夹；mode为read或write")
        grants = [g for g in self.grants() if g["path"] != str(target)]
        grants.append({"path": str(target), "mode": mode, "source": "explicit user grant"})
        write_json(self.permissions, grants)
        return self.info()

    def revoke(self, path):
        target = self.resolve_path(path)
        if target == self.root:
            raise WorkflowError("项目根目录不能撤销；关闭项目即可停止使用")
        write_json(self.permissions, [g for g in self.grants() if Path(g["path"]).resolve() != target])
        return self.info()

    def browse(self, path="", limit=100):
        folder = self.authorize(path or self.root)
        if not folder.is_dir():
            raise WorkflowError("请选择文件夹")
        entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        entries = [p for p in entries if p.name not in (".quantlab", ".git", ".venv", "node_modules")]
        return {
            "path": str(folder),
            "total": len(entries),
            "truncated": len(entries) > limit,
            "entries": [
                {
                    "path": str(p),
                    "name": p.name,
                    "directory": p.is_dir(),
                    "bytes": p.stat().st_size if p.is_file() else None,
                }
                for p in entries[:limit]
            ],
        }

    def import_file(self, service, path, mode="reference", name=""):
        source = self.authorize(path)
        if not source.is_file():
            raise WorkflowError("请选择文件；目录请先浏览后选择需要的数据文件")
        if mode not in ("reference", "copy"):
            raise WorkflowError("导入方式为reference或copy")
        stored = service.file_path(name or ("local-" + uuid.uuid4().hex[:10] + "-" + source.name)).name
        refs = references(service.files)
        if stored in refs or (service.files / stored).exists():
            raise WorkflowError("同名资源已存在，导入不会覆盖")
        sha = digest(source)
        if mode == "copy":
            shutil.copyfile(source, service.files / stored)
        else:
            refs[stored] = {
                "path": str(source),
                "sha256": sha,
                "bytes": source.stat().st_size,
                "grants_file": str(self.permissions),
                "imported_at": time.time(),
                "mode": "reference",
            }
            write_json(service.files / ".references.json", refs)
        return {"name": stored, "sha256": sha, "bytes": source.stat().st_size, "mode": mode, "source": str(source)}

    def relocate(self, service, name, path):
        refs = references(service.files)
        if name not in refs:
            raise WorkflowError("只有外部引用支持重新定位")
        target = self.authorize(path)
        if not target.is_file():
            raise WorkflowError("新的文件不存在")
        old = refs[name]
        sha = digest(target)
        history = [*old.get("history", []), {"path": old["path"], "sha256": old["sha256"], "replaced_at": time.time()}]
        refs[name] = {**old, "path": str(target), "sha256": sha, "bytes": target.stat().st_size, "history": history}
        write_json(service.files / ".references.json", refs)
        return {"name": name, "changed": sha != old["sha256"], "sha256": sha, "history": history}

    def active(self, *, include_management=True):
        from ..agents.lab_state import COLLECTION

        chats = sum(
            1
            for r in self.services.store.for_workspace(self.manifest["workspace_id"]).list(COLLECTION)
            if r.get("status") == "running"
        )
        management = (
            sum(
                1
                for p in (self.home / "store/workflows/management-jobs").glob("*.json")
                if json.loads(p.read_text(encoding="utf-8")).get("state") in ("running", "queued")
            )
            if include_management
            else 0
        )
        return (
            chats
            + management
            + int(
                self.services.db.scalar(
                    "SELECT COUNT(*) FROM runs WHERE workspace_id=? AND state IN ('QUEUED','RUNNING','PAUSED','RECOVERING')",
                    (self.manifest["workspace_id"],),
                )
                or 0
            )
        )

    def _pinned_hashes(self):
        """当前项目固定成果引用的工件 hash（缓存清理必须保留对应条目）。"""
        try:
            rows = self.services.store.for_workspace(self.manifest["workspace_id"]).list("artifact_pins")
        except (OSError, ValueError):
            return set()
        return {row["hash"] for row in rows if isinstance(row.get("hash"), str) and row["hash"]}

    @staticmethod
    def _references_pinned(path, pinned):
        """节点缓存文件的内容是 {端口: 工件引用}；任何一层引用到被固定 hash 就保留。"""

        def scan(value):
            if isinstance(value, dict):
                if value.get("hash") in pinned:
                    return True
                return any(scan(child) for child in value.values())
            if isinstance(value, list):
                return any(scan(child) for child in value)
            return False

        try:
            return scan(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return False

    def cache(self, service, clear=False):
        directory = (service.root / "cache").resolve()
        if not directory.is_relative_to(service.root.resolve()):
            raise WorkflowError("非法缓存路径")
        files = list(directory.glob("*.json")) if directory.exists() else []
        pinned = self._pinned_hashes()
        protected = [p for p in files if pinned and self._references_pinned(p, pinned)] if pinned else []
        result = {
            "entries": len(files),
            "bytes": sum(p.stat().st_size for p in files),
            "pinned_entries": len(protected),
            "active_runs": self.active(),
            "effect": "仅清理可重建的节点计算缓存；运行证据、输入、图表文件和被固定成果引用的缓存条目保留，后续运行可能重新计算",
        }
        if clear:
            if result["active_runs"]:
                raise WorkflowError("项目有运行或排队任务，不能清理缓存")
            for path in files:
                if path in protected:
                    continue
                path.unlink()
            result["cleared"] = True
            result["skipped_pinned"] = len(protected)
            result["protected_files"] = [p.name for p in protected]
        return result
