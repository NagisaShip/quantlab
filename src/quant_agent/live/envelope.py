"""TargetPositionEnvelope：为后续“个人执行器”定义的目标仓位信封（plan v2.0 第 181 行）。

平台本期不执行真实订单；这里只定义协议、生成信封、并提供执行器侧必须做的校验：
过期、重复、乱序的信封一律拒绝；执行器按个人资金与已有持仓自行计算差额，并有独立风险上限。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
#: 标的格式（与 ENVELOPE_SCHEMA 里的 pattern 必须一致，见 check()）
INSTRUMENT_RE = re.compile(r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")
ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TargetPositionEnvelope",
    "type": "object",
    "required": [
        "schema_version",
        "signal_id",
        "source_strategy_version",
        "instrument_id",
        "target_weight",
        "created_at",
        "expires_at",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "signal_id": {"type": "string", "minLength": 8, "maxLength": 64},
        "source_strategy_version": {"type": "string"},
        "instrument_id": {"type": "string", "pattern": INSTRUMENT_RE.pattern},
        "target_weight": {"type": "number", "minimum": 0, "maximum": 1},
        "created_at": {"type": "integer", "description": "毫秒，UTC；信号决策时间"},
        "expires_at": {"type": "integer", "description": "毫秒，UTC；过期后执行器必须拒绝"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}


def from_signal(sig: dict[str, Any], *, equity: float | None = None) -> dict[str, Any] | None:
    """把平台信号转换成信封。只有“权重型”目标能直接转换；数量型目标需按当时权益折算。"""
    t = sig.get("target") or {}
    if t.get("kind") == "weight":
        w = float(t["value"])
    elif t.get("kind") == "quantity" and equity:
        return None  # 数量目标依赖价格与资金，执行器不应盲目照搬；由平台侧换算后再发
    else:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "signal_id": sig["signal_id"],
        "source_strategy_version": sig.get("strategy_version_id") or "",
        "instrument_id": sig["instrument_id"],
        "target_weight": max(0.0, min(1.0, w)),
        "created_at": int(sig["decision_ts"]),
        "expires_at": int(sig["expires_at"]),
        "reason": sig.get("reason", ""),
    }


@dataclass
class EnvelopeValidator:
    """执行器侧的校验状态机：同一 (策略版本, 标的) 只接受时间严格递增、未过期、未见过的信封。"""

    max_weight: float = 1.0
    seen: set[str] = field(default_factory=set)
    last_created: dict[tuple[str, str], int] = field(default_factory=dict)

    def check(self, env: dict[str, Any], *, now: int) -> tuple[bool, str]:
        missing = [k for k in ENVELOPE_SCHEMA["required"] if k not in env]
        if missing:
            return False, f"缺少字段：{', '.join(missing)}"
        extra = set(env) - set(ENVELOPE_SCHEMA["properties"])
        if extra:
            return False, f"未知字段：{', '.join(sorted(extra))}"
        if env["schema_version"] != SCHEMA_VERSION:
            return False, f"不支持的 schema_version：{env['schema_version']}"
        if not isinstance(env["instrument_id"], str) or not INSTRUMENT_RE.match(env["instrument_id"]):
            return False, f"instrument_id 格式不合法（应为 BASE/QUOTE，如 BTC/USDT）：{env['instrument_id']!r}"
        w = env["target_weight"]
        if not isinstance(w, (int, float)) or isinstance(w, bool) or not 0 <= w <= 1:
            return False, "target_weight 必须在 [0, 1]"
        if w > self.max_weight:
            return False, f"目标权重 {w} 超过跟随账户的风险上限 {self.max_weight}"
        if env["expires_at"] <= env["created_at"]:
            return False, "expires_at 必须晚于 created_at"
        if now >= env["expires_at"]:
            return False, "信封已过期，拒绝执行"
        if env["signal_id"] in self.seen:
            return False, "重复信封（signal_id 已处理），拒绝执行"
        key = (env["source_strategy_version"], env["instrument_id"])
        if env["created_at"] <= self.last_created.get(key, -1):
            return False, "乱序信封（早于已处理的同标的信封），拒绝执行"
        self.seen.add(env["signal_id"])
        self.last_created[key] = env["created_at"]
        return True, "通过"
