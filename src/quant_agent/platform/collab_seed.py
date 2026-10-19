"""工作区内置协作示例：一个公开群组和一条任务。

新手引导需要打开“平台内置内容”的详情页；群组与任务页不像因子 / 模型那样有
文件型默认资产，所以在这里补一个幂等的协作示例。它在工作区首次初始化时写入，
所有成员都能看到；成员打开示例详情时会自动加入公开群组。
"""

from __future__ import annotations

from typing import Any

BUILTIN_GROUP_NAME = "平台内置 · 研究协作示例"
BUILTIN_TASK_TITLE = "复核内置双均线策略的回测与审查"


def seed_builtin_collab(services: Any, workspace_id: str, owner_id: str) -> str | None:
    """写入内置群组与任务；已存在时只补齐缺失部分。"""
    collab = getattr(services, "collab", None)
    db = getattr(services, "db", None)
    if collab is None or db is None or not workspace_id or not owner_id or owner_id == "system":
        return None

    row = db.fetchone(
        "SELECT id FROM collab_groups WHERE workspace_id=? AND name=? AND archived_at IS NULL",
        (workspace_id, BUILTIN_GROUP_NAME),
    )
    if row is not None:
        gid = str(row["id"])
    else:
        try:
            gid = collab.create_group(
                workspace_id,
                owner_id,
                name=BUILTIN_GROUP_NAME,
                description="平台内置的协作示例：讨论、引用研究与把下一步摊派成任务。",
                visibility="open",
            )
        except Exception:
            # 另一个进程可能刚创建了同名群组；重新读取，确实没有才向上抛。
            row = db.fetchone(
                "SELECT id FROM collab_groups WHERE workspace_id=? AND name=? AND archived_at IS NULL",
                (workspace_id, BUILTIN_GROUP_NAME),
            )
            if row is None:
                raise
            gid = str(row["id"])

    if collab.group_role(gid, owner_id) is None:
        collab.add_members(gid, [owner_id], by=owner_id, by_name="QuantLab 平台")

    task = db.fetchone(
        "SELECT id FROM collab_tasks WHERE group_id=? AND title=? AND status<>'cancelled'",
        (gid, BUILTIN_TASK_TITLE),
    )
    if task is None:
        collab.create_task(
            gid,
            by=owner_id,
            by_name="QuantLab 平台",
            title=BUILTIN_TASK_TITLE,
            description=(
                "这是平台内置的协作示例。打开任务看板，看看研究结论如何被摊派成任务、"
                "跟踪状态，并把回测与上线验证作为引用附在任务里。"
            ),
            assignee_id=owner_id,
            priority="normal",
            refs=[
                {"type": "link", "id": "backtests", "title": "回测列表"},
                {"type": "link", "id": "validation", "title": "上线验证"},
            ],
        )
    return gid


__all__ = ["BUILTIN_GROUP_NAME", "BUILTIN_TASK_TITLE", "seed_builtin_collab"]
