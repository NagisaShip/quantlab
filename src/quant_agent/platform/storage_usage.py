"""按成员统计上传文件占用的服务器存储空间。

平台里能可靠归属到个人的服务器存储主要是用户上传的原图 / 附件及其缩略图。
策略代码、回测结果等由工作区共享，无法在不引入额外归属表的情况下准确拆分到个人，
因此成员存储配额只针对 ``uploads`` 表，UI 中也明确标注统计口径。

存储是存量指标：配额比较的是“当前总占用 + 本次上传”，不做时间窗口累计。
"""

from __future__ import annotations

from typing import Any

from .db import Database


def _row_stats(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    original = int(row.get("original_bytes") or 0)
    thumbnails = int(row.get("thumbnail_bytes") or 0)
    return {
        "used_bytes": original + thumbnails,
        "original_bytes": original,
        "thumbnail_bytes": thumbnails,
        "file_count": int(row.get("file_count") or 0),
        "latest_at": int(row.get("latest_at") or 0) or None,
    }


def upload_storage(db: Database, workspace_id: str, user_id: str | None = None) -> dict[str, Any]:
    """统计工作区（或其中某成员）的上传存储，包含服务端生成的缩略图。"""

    sql = (
        "SELECT COALESCE(SUM(size), 0) AS original_bytes, "
        "COALESCE(SUM(COALESCE(thumb_size, 0)), 0) AS thumbnail_bytes, "
        "COUNT(*) AS file_count, MAX(created_at) AS latest_at "
        "FROM uploads WHERE workspace_id=?"
    )
    args: list[Any] = [workspace_id]
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    return _row_stats(db.fetchone(sql, tuple(args)))


def upload_storage_by_user(db: Database, workspace_id: str) -> dict[str, dict[str, Any]]:
    """一次查询返回工作区内每个成员的上传存储统计。"""

    rows = db.fetchall(
        "SELECT user_id, COALESCE(SUM(size), 0) AS original_bytes, "
        "COALESCE(SUM(COALESCE(thumb_size, 0)), 0) AS thumbnail_bytes, "
        "COUNT(*) AS file_count, MAX(created_at) AS latest_at "
        "FROM uploads WHERE workspace_id=? GROUP BY user_id",
        (workspace_id,),
    )
    return {str(row["user_id"]): _row_stats(row) for row in rows}
