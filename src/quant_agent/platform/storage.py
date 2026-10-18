"""工作区存储分类、清理与自动保留策略。

只处理平台已知的数据目录与集合；不跟随符号链接，不触碰其他工作区。
自动清理只删除超过保留期的数据，运行中的对话 / 任务不会被删除。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ..store import Collections
from .db import now_ms

CATEGORIES: dict[str, dict[str, Any]] = {
    "conversations": {
        "label": "对话与 Agent 输出",
        "collections": ("lab_projects",),
        "blobs": ("lab_threads", "lab_images"),
    },
    "feedback": {
        "label": "Agent 评价与意见",
        "collections": ("lab_message_feedback",),
    },
    "backtests": {
        "label": "回测、优化与结果文件",
        "collections": ("backtests", "optimizations", "compare_batches"),
        "blobs": ("backtests", "optimizations", "run_results"),
    },
    "runs": {
        "label": "运行记录与实验",
        "collections": ("runs", "paper_sessions", "reconciliations"),
        "blobs": ("paper_sessions",),
    },
    "uploads": {"label": "上传文件与对话媒体", "dirs": ("uploads", "store/media")},
    "cache": {"label": "行情与接口缓存", "dirs": ("cache", "market/bars")},
    "audit": {"label": "审计日志", "db": "audit"},
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _size(path: Path) -> tuple[int, int, int | None, int | None]:
    """返回 (bytes, files, oldest_mtime_ms, newest_mtime_ms)，不跟随符号链接。"""
    if not path.exists():
        return 0, 0, None, None
    total = files = 0
    oldest: float | None = None
    newest: float | None = None
    if path.is_file():
        try:
            st = path.lstat()
        except OSError:
            return 0, 0, None, None
        return st.st_size, 1, int(st.st_mtime * 1000), int(st.st_mtime * 1000)
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for name in names:
            f = Path(root, name)
            try:
                st = f.lstat()
            except OSError:
                continue
            total += st.st_size
            files += 1
            oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    return total, files, int(oldest * 1000) if oldest else None, int(newest * 1000) if newest else None


def _collect(path: Path, out: dict[str, int]) -> None:
    b, n, old, new = _size(path)
    out["bytes"] = out.get("bytes", 0) + b
    out["files"] = out.get("files", 0) + n
    if old is not None:
        cur_old = out.get("oldest")
        out["oldest"] = old if cur_old is None else min(cur_old, old)
    if new is not None:
        cur_new = out.get("newest")
        out["newest"] = new if cur_new is None else max(cur_new, new)


def _workspace_root(services: Any, ws: str) -> Path:
    return Path(services.home) / "workspaces" / ws


def scan(services: Any, ws: str) -> dict[str, Any]:
    home = Path(services.home)
    root = _workspace_root(services, ws) / "store"
    rows: list[dict[str, Any]] = []
    for key, spec in CATEGORIES.items():
        out: dict[str, Any] = {
            "key": key,
            "label": spec["label"],
            "bytes": 0,
            "files": 0,
            "oldest": None,
            "newest": None,
        }
        for coll in spec.get("collections") or ():
            _collect(root / f"{coll}.json", out)
        for kind in spec.get("blobs") or ():
            _collect(root / "blobs" / kind, out)
        for rel in spec.get("dirs") or ():
            _collect(home / rel if not rel.startswith("store/") else _workspace_root(services, ws) / rel, out)
        if spec.get("db") == "audit":
            try:
                row = (
                    services.db.fetchone(
                        "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(action)+LENGTH(details)+LENGTH(COALESCE(ip,''))),0) AS b, "
                        "MIN(ts) AS old, MAX(ts) AS new FROM audit_log WHERE workspace_id=?",
                        (ws,),
                    )
                    or {}
                )
                out["files"] = int(row.get("n") or 0)
                out["bytes"] = int(row.get("b") or 0)
                out["oldest"] = int(row["old"]) if row.get("old") else None
                out["newest"] = int(row["new"]) if row.get("new") else None
            except Exception:
                pass
        rows.append(out)
    total = sum(int(r["bytes"]) for r in rows)
    disk = shutil.disk_usage(home)
    return {
        "workspace_id": ws,
        "categories": rows,
        "total_bytes": total,
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "used_percent": round(disk.used * 100 / disk.total, 1) if disk.total else 0,
        },
    }


def _cutoff_ms(keep_days: int) -> int:
    return now_ms() - max(0, int(keep_days)) * 86_400_000


def _is_old(value: Any, cutoff: int) -> bool:
    try:
        n = int(float(value or 0))
    except (TypeError, ValueError):
        return False
    if n < 1_000_000_000_000:  # 秒级时间戳
        n *= 1000
    return 0 < n < cutoff


def _delete_old_files(path: Path, cutoff: int) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    files = size = 0
    for root, dirs, names in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not Path(root, d).is_symlink()]
        for name in names:
            f = Path(root, name)
            try:
                st = f.lstat()
                if st.st_mtime * 1000 >= cutoff:
                    continue
                n = st.st_size
                f.unlink()
                files += 1
                size += n
            except OSError:
                continue
    return files, size


def cleanup(services: Any, ws: str, category: str, *, keep_days: int, dry_run: bool = False) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise ValueError(f"未知存储分类：{category}")
    root = _workspace_root(services, ws) / "store"
    cutoff = _cutoff_ms(keep_days)
    before = scan(services, ws)
    deleted = {"files": 0, "bytes": 0, "records": 0}
    store = services.store.for_workspace(ws)
    if category == "conversations":
        from ..agents.strategy_lab import LabStore

        lab = LabStore(store)
        for p in lab.list():
            if p.get("status") == "running" or not _is_old(p.get("updated_at") or p.get("created_at"), cutoff):
                continue
            if dry_run:
                deleted["records"] += 1
                continue
            b = _size(root / "blobs" / "lab_threads" / f"{p['id']}.json")[0]
            lab.delete(p["id"])
            deleted["records"] += 1
            deleted["bytes"] += b
    elif category == "feedback":
        for rec in store.list(Collections.MESSAGE_FEEDBACK):
            if not _is_old(rec.get("updated_at") or rec.get("created_at"), cutoff):
                continue
            if dry_run:
                deleted["records"] += 1
                continue
            store.delete(Collections.MESSAGE_FEEDBACK, rec["id"])
            deleted["records"] += 1
    elif category == "backtests":
        for rec in store.list(Collections.BACKTESTS):
            if not _is_old(rec.get("created_at"), cutoff):
                continue
            if dry_run:
                deleted["records"] += 1
                continue
            bid = rec["id"]
            b, n, _, _ = _size(root / "blobs" / "backtests" / f"{bid}.json")
            store.delete_blob("backtests", bid)
            store.delete(Collections.BACKTESTS, bid)
            results = Path(services.home) / "results" / ws
            if results.exists():
                for f in results.glob(f"{bid}.*"):
                    try:
                        b += f.stat().st_size
                        n += 1
                        f.unlink()
                    except OSError:
                        pass
            deleted["records"] += 1
            deleted["files"] += n
            deleted["bytes"] += b
    elif category == "uploads":
        for rec in services.db.fetchall("SELECT id, size, created_at FROM uploads WHERE workspace_id=?", (ws,)):
            if not _is_old(rec.get("created_at"), cutoff):
                continue
            if dry_run:
                deleted["records"] += 1
                continue
            f = Path(services.collab.upload_dir()) / rec["id"]
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                continue
            services.db.execute("DELETE FROM uploads WHERE id=?", (rec["id"],))
            deleted["records"] += 1
            deleted["files"] += 1
            deleted["bytes"] += int(rec.get("size") or 0)
    elif category == "cache":
        for rel in CATEGORIES["cache"]["dirs"]:
            f, b = _delete_old_files(Path(services.home) / rel, cutoff) if not dry_run else (0, 0)
            deleted["files"] += f
            deleted["bytes"] += b
        if dry_run:
            for rel in CATEGORIES["cache"]["dirs"]:
                p = Path(services.home) / rel
                if p.exists():
                    for root2, _, names in os.walk(p, followlinks=False):
                        for name in names:
                            try:
                                if Path(root2, name).lstat().st_mtime * 1000 < cutoff:
                                    deleted["files"] += 1
                            except OSError:
                                pass
    elif category == "audit":
        days = max(1, int(keep_days))
        if dry_run:
            row = (
                services.db.fetchone("SELECT COUNT(*) AS n FROM audit_log WHERE workspace_id=? AND ts<?", (ws, cutoff))
                or {}
            )
            deleted["records"] = int(row.get("n") or 0)
        else:
            try:
                res = services.audit.archive_expired(ws, retention_days=days)
                deleted["records"] = int(res.get("archived") or 0)
            except Exception:
                pass
    elif category == "runs":
        for rec in store.list(Collections.RUNS):
            if not _is_old(rec.get("created_at"), cutoff):
                continue
            if dry_run:
                deleted["records"] += 1
                continue
            services.db.execute("DELETE FROM runs WHERE id=? AND workspace_id=?", (rec["id"], ws))
            store.delete(Collections.RUNS, rec["id"])
            deleted["records"] += 1
    if not dry_run:
        try:
            services.audit.record(
                "storage.cleanup",
                object_type="workspace",
                object_id=ws,
                details={"category": category, "keep_days": keep_days, **deleted},
            )
        except Exception:
            pass
    after = scan(services, ws)
    return {"ok": True, "category": category, "dry_run": dry_run, "deleted": deleted, "before": before, "after": after}


def auto_cleanup(services: Any) -> dict[str, Any]:
    """执行隐私保留与磁盘阈值清理。

    - 隐私开关关闭时，不依赖磁盘阈值，立即按维护周期清理已结束对话 / 旧附件；
    - ``retention_days > 0`` 时，按工作区默认保留期清理各类旧数据；
    - 磁盘越过阈值时，再按“缓存 → 上传 → 回测 → 对话 → 评价”的顺序清理到目标占用。
    """
    home = Path(services.home)
    disk = shutil.disk_usage(home)
    used_pct = disk.used * 100 / disk.total if disk.total else 0
    out: dict[str, Any] = {"used_percent": round(used_pct, 1), "workspaces": [], "deleted": []}
    try:
        workspaces = [r["id"] for r in services.db.fetchall("SELECT id FROM workspaces")]
    except Exception:
        return out
    for ws in workspaces:
        try:
            st = services.auth.workspace(ws).get("settings") or {}
            policy = {
                **({"auto_cleanup": False, "threshold_percent": 90, "target_percent": 75, "keep_days": 30}),
                **(st.get("storage") or {}),
            }
            privacy = {
                **(
                    {
                        "retain_agent_history": True,
                        "retain_uploads": True,
                        "allow_improvement_feedback": True,
                        "retention_days": 365,
                    }
                ),
                **(st.get("privacy") or {}),
            }
            keep = max(1, int(policy.get("keep_days") or 30))
            retention = max(0, int(privacy.get("retention_days") or 0))
            drop_history = privacy.get("retain_agent_history") is False
            drop_uploads = privacy.get("retain_uploads") is False
            threshold = float(policy.get("threshold_percent") or 90)
            target = float(policy.get("target_percent") or 75)
            disk_cleanup = bool(policy.get("auto_cleanup")) and used_pct >= threshold

            # 隐私关闭项优先于磁盘策略执行；保留 0 天表示清理所有已结束数据，等价于最多保留一个维护周期。
            scheduled: dict[str, int] = {}
            if drop_history:
                scheduled["conversations"] = 0
            elif retention > 0:
                scheduled["conversations"] = retention
            if drop_uploads:
                scheduled["uploads"] = 0
            elif retention > 0:
                scheduled["uploads"] = retention
            if retention > 0:
                for category in ("feedback", "backtests", "runs"):
                    scheduled.setdefault(category, retention)

            for category in ("uploads", "conversations", "feedback", "backtests", "runs"):
                if category not in scheduled:
                    continue
                try:
                    res = cleanup(services, ws, category, keep_days=scheduled[category], dry_run=False)
                    deleted = res["deleted"]
                    if any(int(deleted.get(k) or 0) for k in ("records", "files", "bytes")):
                        out["deleted"].append(
                            {
                                "workspace_id": ws,
                                "category": category,
                                "reason": "privacy",
                                **deleted,
                            }
                        )
                except Exception as exc:
                    out["deleted"].append(
                        {"workspace_id": ws, "category": category, "reason": "privacy", "error": str(exc)[:160]}
                    )

            if disk_cleanup:
                for category in ("cache", "uploads", "backtests", "conversations", "feedback"):
                    if used_pct <= target:
                        break
                    days = (
                        0
                        if (category == "uploads" and drop_uploads) or (category == "conversations" and drop_history)
                        else keep
                    )
                    try:
                        res = cleanup(services, ws, category, keep_days=days, dry_run=False)
                        deleted = res["deleted"]
                        if any(int(deleted.get(k) or 0) for k in ("records", "files", "bytes")):
                            out["deleted"].append(
                                {
                                    "workspace_id": ws,
                                    "category": category,
                                    "reason": "disk",
                                    **deleted,
                                }
                            )
                    except Exception as exc:
                        out["deleted"].append(
                            {"workspace_id": ws, "category": category, "reason": "disk", "error": str(exc)[:160]}
                        )
                    disk = shutil.disk_usage(home)
                    used_pct = disk.used * 100 / disk.total if disk.total else used_pct

            if scheduled or disk_cleanup:
                out["workspaces"].append({"workspace_id": ws, "used_percent": round(used_pct, 1)})
        except Exception:
            continue
    out["used_percent"] = round(used_pct, 1)
    return out
