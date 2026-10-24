"""Small, deterministic guards at the Agent/tool boundary."""

from __future__ import annotations

import re
from typing import Any

_PROTOCOL_RESIDUE = re.compile(r"</?(?:target|tool|assistant|final)[_-]?placeholder\s*/?>", re.I)


def clean_protocol_residue(text: str) -> str:
    """Remove only known provider protocol placeholders, not arbitrary angle brackets."""
    return _PROTOCOL_RESIDUE.sub("", str(text or ""))


def approval_input_error(tool_name: str, args: dict[str, Any] | None) -> str:
    """Reject empty destructive calls before they become approval cards.

    A confirmation request is a promise that there is a concrete action to approve.
    Returning a recoverable tool error lets the model correct its arguments without
    interrupting the researcher.
    """
    values = args or {}
    if tool_name == "delete_backtests":
        ids = values.get("ids")
        if not isinstance(ids, list) or not any(str(item).strip() for item in ids if item is not None):
            return "没有指定要删除的回测记录；未创建审批"
    elif tool_name == "delete_strategy":
        if not str(values.get("slug") or "").strip():
            return "没有指定要删除的策略；未创建审批"
    elif tool_name == "delete_factor":
        if not str(values.get("fid") or "").strip():
            return "没有指定要删除的因子；未创建审批"
    return ""
