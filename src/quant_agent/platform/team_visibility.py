"""Conservative host ACL migration for Team runs with no declared origin."""

from __future__ import annotations

from typing import Any

from . import context
from .db import now_ms


class TeamRunVisibility:
    def __init__(self, db: Any, store: Any, source_visible: Any):
        self.db = db
        self.store = store
        self.source_visible = source_visible

    def _sources(self, workspace_id: str, run_id: str) -> list[str]:
        return [
            row["project_id"]
            for row in self.db.fetchall(
                "SELECT project_id FROM team_run_sources WHERE workspace_id=? AND run_id=? ORDER BY project_id",
                (workspace_id, run_id),
            )
        ]

    def _remember(self, workspace_id: str, run_id: str, projects: list[str], evidence: str) -> None:
        if not projects:
            return
        with self.db.tx() as tx:
            for pid in projects:
                tx.execute(
                    "INSERT INTO team_run_sources(workspace_id,run_id,project_id,evidence,discovered_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(workspace_id,run_id,project_id) DO NOTHING",
                    (workspace_id, run_id, pid, evidence, now_ms()),
                )

    def _discover(self, record: dict) -> list[str]:
        """Only server-authored Team cards in a pre-existing conversation count.

        A fork made after the review copies its card, but cannot make itself the
        origin of that review. Orphan thread references are retained as missing
        origins, which source_visible rejects rather than defaulting to public.
        """
        projects = {project["id"]: project for project in self.store.list("lab_projects")}
        found = []
        created_at = record.get("created_at")
        for pid in self.store.blob_ids("lab_threads"):
            project = projects.get(pid)
            if project and (
                not isinstance(created_at, (int, float))
                or not isinstance(project.get("created_at"), (int, float))
                or project["created_at"] > created_at
            ):
                continue
            thread = self.store.get_blob("lab_threads", pid) or {}
            if any(
                message.get("role") == "tool"
                and message.get("kind") == "team"
                and isinstance(message.get("data"), dict)
                and message["data"].get("run_id") == record["id"]
                for message in thread.get("messages", [])
                if isinstance(message, dict)
            ):
                found.append(pid)
        return found

    def __call__(self, record: dict, owner_id: str | None = None) -> bool:
        ctx = context.current()
        workspace_id = context.current_workspace()
        run_id = record["id"]
        source = record.get("source_project_id")
        known = self._sources(workspace_id, run_id)
        if source and source not in known:
            self._remember(workspace_id, run_id, [source], "declared")
            known = sorted(set(known) | {source})
        elif record.get("source_policy_version") != 1 and not known:
            found = self._discover(record)
            self._remember(workspace_id, run_id, found, "legacy_thread")
            known = self._sources(workspace_id, run_id)
        if known:
            return all(self.source_visible(pid) for pid in known)
        if record.get("source_policy_version") == 1 and source == "":
            return True  # Explicitly declared workspace Team, not unknown legacy data.
        return ctx.is_ws_admin or bool(owner_id and ctx.user_id == owner_id)
