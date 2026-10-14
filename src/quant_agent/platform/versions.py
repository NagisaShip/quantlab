"""策略版本（不可改写）。

每一份被回测、优化或实时模拟使用过的策略内容（代码 / DSL + 参数），都对应 strategy_versions 里的
一行；行内容写入后**永不更新**，只有状态会沿 DRAFT → VALIDATED → FROZEN → ARCHIVED 单向推进：

- DRAFT      已保存但未通过校验（DSL 校验失败 / Python 加载失败）——不能运行
- VALIDATED  校验通过，可以回测、优化
- FROZEN     冻结：人工批准后，内容被锁定，才能进入实时模拟 / 只读实盘对照
- ARCHIVED   归档：不再允许新的运行引用它（历史运行仍可追溯）

改代码或改参数 → code_hash 变化 → 新的版本号；同一内容重复保存不会产生新版本。
运行（runs.strategy_version_id）引用的是这里的 id，复现时按 id 取回当时的确切内容。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from .db import Database, IntegrityError, jdump, jload, now_ms
from .errors import AppError, ErrorCode

DRAFT, VALIDATED, FROZEN, ARCHIVED = "DRAFT", "VALIDATED", "FROZEN", "ARCHIVED"
RUNNABLE = (VALIDATED, FROZEN)


def content_hash(lang: str, code: str, params: dict[str, Any] | None) -> str:
    body = json.dumps({"lang": lang, "code": code, "params": params or {}}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class VersionService:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _out(row: dict[str, Any] | None, *, code: bool = False) -> dict[str, Any] | None:
        if row is None:
            return None
        out = dict(row)
        out["params"] = jload(row.get("params"), {})
        out["securities"] = jload(row.get("securities"), [])
        out["validation"] = jload(row.get("validation"), {})
        if not code:
            out.pop("code", None)
        return out

    def ensure(
        self,
        *,
        workspace_id: str,
        slug: str,
        lang: str,
        name: str,
        code: str,
        params: dict[str, Any] | None,
        securities: list[str],
        timeframe: str,
        user_id: str,
        issues: list[str],
    ) -> dict[str, Any]:
        """取得（或新建）与这份内容对应的版本。内容相同 → 返回已有版本，不重复登记。"""
        h = content_hash(lang, code, params)
        row = self.db.fetchone(
            "SELECT * FROM strategy_versions WHERE workspace_id=? AND strategy_slug=? AND code_hash=?",
            (workspace_id, slug, h),
        )
        if row:
            return self._out(row)
        for _ in range(3):
            no = (
                int(
                    self.db.scalar(
                        "SELECT COALESCE(MAX(version_no), 0) FROM strategy_versions WHERE workspace_id=? AND strategy_"
                        "slug=?",
                        (workspace_id, slug),
                    )
                    or 0
                )
                + 1
            )
            vid = f"sv-{uuid.uuid4().hex[:16]}"
            status = DRAFT if issues else VALIDATED
            try:
                self.db.execute(
                    "INSERT INTO strategy_versions (id, workspace_id, strategy_slug, version_no, kind, name, code, cod"
                    "e_hash, "
                    "params, securities, timeframe, schema_version, status, validation, created_by, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        vid,
                        workspace_id,
                        slug,
                        no,
                        lang,
                        name,
                        code,
                        h,
                        jdump(params or {}),
                        jdump(securities),
                        timeframe,
                        2 if lang == "dsl" else 1,
                        status,
                        jdump({"issues": issues}),
                        user_id,
                        now_ms(),
                    ),
                )
                return self._out(self.db.fetchone("SELECT * FROM strategy_versions WHERE id=?", (vid,)))
            except IntegrityError:
                row = self.db.fetchone(
                    "SELECT * FROM strategy_versions WHERE workspace_id=? AND strategy_slug=? AND code_hash=?",
                    (workspace_id, slug, h),
                )
                if row:
                    return self._out(row)
        raise AppError(ErrorCode.CONFLICT, "并发保存冲突，请重试")

    def get(self, workspace_id: str, version_id: str, *, code: bool = False) -> dict[str, Any] | None:
        return self._out(
            self.db.fetchone(
                "SELECT * FROM strategy_versions WHERE id=? AND workspace_id=?", (version_id, workspace_id)
            ),
            code=code,
        )

    def list(self, workspace_id: str, slug: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM strategy_versions WHERE workspace_id=? AND strategy_slug=? ORDER BY version_no DESC",
            (workspace_id, slug),
        )
        return [self._out(r) for r in rows]

    def _transition(self, workspace_id: str, version_id: str, to: str, allowed_from: tuple[str, ...]) -> dict[str, Any]:
        v = self.get(workspace_id, version_id)
        if v is None:
            raise AppError(ErrorCode.NOT_FOUND, "策略版本不存在")
        if v["status"] == to:
            return v
        if v["status"] not in allowed_from:
            raise AppError(
                ErrorCode.CONFLICT,
                f"版本状态为 {v['status']}，不能变为 {to}",
                details={"status": v["status"], "to": to},
            )
        extra = ", archived_at=?" if to == ARCHIVED else ""
        params: list[Any] = [to] + ([now_ms()] if to == ARCHIVED else []) + [version_id, workspace_id, v["status"]]
        self.db.execute(
            f"UPDATE strategy_versions SET status=?{extra} WHERE id=? AND workspace_id=? AND status=?", params
        )
        return self.get(workspace_id, version_id)

    def freeze(self, workspace_id: str, version_id: str) -> dict[str, Any]:
        v = self.get(workspace_id, version_id)
        if v and v["status"] == DRAFT:
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                "草稿版本未通过校验，不能冻结：" + "；".join(v["validation"].get("issues", [])[:3]),
            )
        return self._transition(workspace_id, version_id, FROZEN, (VALIDATED,))

    def archive(self, workspace_id: str, version_id: str) -> dict[str, Any]:
        return self._transition(workspace_id, version_id, ARCHIVED, (DRAFT, VALIDATED, FROZEN))

    def require_runnable(self, version: dict[str, Any], *, frozen: bool = False) -> None:
        if version["status"] == ARCHIVED:
            raise AppError(ErrorCode.CONFLICT, "该版本已归档，不能再发起新的运行")
        if version["status"] == DRAFT:
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                "草稿版本未通过校验：" + "；".join(version["validation"].get("issues", [])[:3]),
            )
        if frozen and version["status"] != FROZEN:
            raise AppError(
                ErrorCode.PERMISSION_DENIED,
                "只有经人工批准而冻结的版本才能进入实时模拟或实盘对照",
                details={"status": version["status"]},
            )
