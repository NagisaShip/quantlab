"""Shared tool dispatch admission, separate from host model billing authority."""

from __future__ import annotations

from typing import Any

from .journal import Journal, JournalIntegrityError, load


class RuntimeScheduler:
    def __init__(self, journal: Journal):
        self.journal = journal

    def set_limit(
        self,
        resource: str,
        capacity: int,
        *,
        tenant_id: str | None = None,
        tenant_default: bool = False,
        if_absent: bool = False,
    ) -> None:
        if not isinstance(resource, str) or not resource or len(resource) > 256:
            raise ValueError("a bounded resource identity is required")
        if type(capacity) is not int or not 0 <= capacity <= 100000:
            raise ValueError("capacity must be an integer between 0 and 100000")
        if tenant_default and tenant_id is not None:
            raise ValueError("tenant and tenant_default are mutually exclusive")
        if tenant_id is not None and not tenant_id:
            raise ValueError("tenant identity cannot be empty")
        scope = "tenant_default" if tenant_default else "tenant" if tenant_id is not None else "global"
        with self.journal.tx() as db:
            db.execute(
                "INSERT INTO resource_limits(scope,tenant_id,resource,capacity) VALUES(?,?,?,?) "
                + (
                    "ON CONFLICT(scope,tenant_id,resource) DO NOTHING"
                    if if_absent
                    else "ON CONFLICT(scope,tenant_id,resource) DO UPDATE SET capacity=excluded.capacity"
                ),
                (scope, tenant_id or "", resource, capacity),
            )

    @staticmethod
    def tool_resource(spec: dict, payload: dict) -> str:
        metadata = spec.get("metadata") or {}
        mapping = metadata.get("tool_resource_classes") or {}
        resource = mapping.get(payload["name"], "tool:" + payload["name"])
        if not isinstance(resource, str) or not resource or len(resource) > 256:
            raise ValueError("invalid frozen tool resource class")
        return resource

    @classmethod
    def tool_admission(cls, db: Any, run: Any, step: Any, spec: dict, payload: dict) -> dict | None:
        """Called inside the same write transaction that claims the Step lease.

        Unknown effects retain their logical permit until reconciled; lease expiry
        cannot prove an external operation stopped. A safe retry of the same Step
        reuses its own logical permit. Limits bound managed operations, not remote
        processes outside Runtime's control.
        """
        policy = db.execute("SELECT version FROM resource_policy_meta WHERE id=1").fetchone()
        if policy is None or policy["version"] != 1:
            raise JournalIntegrityError("shared tool admission policy version is missing or unsupported")
        resource = cls.tool_resource(spec, payload)
        resources = {"tools", resource}
        limits = db.execute(
            "SELECT scope,tenant_id,resource,capacity FROM resource_limits "
            "WHERE scope IN ('global','tenant_default') OR (scope='tenant' AND tenant_id=?)",
            (run["tenant_id"],),
        ).fetchall()
        defaults = {(row["scope"], row["resource"]) for row in limits}
        if not {("global", "tools"), ("tenant_default", "tools")}.issubset(defaults):
            raise JournalIntegrityError("required shared tool admission policy is missing")
        selected = [row for row in limits if row["resource"] in resources]
        overrides = {row["resource"] for row in selected if row["scope"] == "tenant"}
        selected = [row for row in selected if row["scope"] != "tenant_default" or row["resource"] not in overrides]
        active = db.execute(
            "SELECT s.id,s.payload,r.tenant_id,r.spec FROM steps s JOIN runs r ON r.id=s.run_id "
            "WHERE s.kind='tool' AND s.state IN ('IN_FLIGHT','RECONCILING') AND s.id<>?",
            (step["id"],),
        ).fetchall()
        for limit in selected:
            used = sum(
                1
                for item in active
                if (limit["scope"] == "global" or item["tenant_id"] == run["tenant_id"])
                and (
                    limit["resource"] == "tools"
                    or cls.tool_resource(load(item["spec"]), load(item["payload"])) == limit["resource"]
                )
            )
            if used >= limit["capacity"]:
                return {
                    "resource": limit["resource"],
                    "scope": limit["scope"],
                    "used": used,
                    "capacity": limit["capacity"],
                }
        return None
