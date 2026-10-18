"""Qmoji 自动表情：把结构化回测指标翻译成一条克制的 Agent 结束语。

设计边界：
- 每条 Agent 发言最多追加一个 Qmoji；
- 只追加在结论末尾，不插入代码块、表格或数字中间；
- 指标不满足时不强行发表情；
- 先警告、后庆祝：风控拦截、过拟合、未来函数、严重亏损优先于 alpha / moon。
"""

from __future__ import annotations

import re
from typing import Any

QMOJI_RE = re.compile(r":qbot-[a-z0-9-]+:")
STRONG_SHARPE = 2.5
STRONG_EXCESS = 0.03
MOON_EXCESS = 0.10
MIN_TRADES = 20
SEVERE_LOSS = -0.10
DRAWDOWN_ALERT = 0.25
RISK_DRAWDOWN_LIMIT = 0.20


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            if raw.endswith("%"):
                return float(raw[:-1]) / 100.0
            return float(raw)
        except ValueError:
            return None
    return None


def metrics_from_materials(materials: dict[str, Any] | None) -> dict[str, Any]:
    """从团队 / Mission / 回测上下文里取出 metrics，兼容几种嵌套写法。"""
    if not isinstance(materials, dict):
        return {}
    direct = materials.get("metrics")
    if isinstance(direct, dict):
        return direct
    for key in ("backtest", "lab", "run", "result"):
        node = materials.get(key)
        if isinstance(node, dict) and isinstance(node.get("metrics"), dict):
            return node["metrics"]
    return {}


def has_qmoji(text: str) -> bool:
    return bool(QMOJI_RE.search(text or ""))


def _mentions(text: str, *words: str) -> bool:
    return any(word in text for word in words)


def qmoji_for_metrics(
    text: str, *, role: str = "", tags: list[str] | None = None, metrics: dict[str, Any] | None = None
) -> str:
    """返回表情 id；没有合适表情时返回空字符串。"""
    body = text or ""
    role_id = (role or "").lower()
    role_tags = tags or []
    m = metrics or {}
    sharpe = _number(m.get("sharpe"))
    excess = _number(m.get("excess_return"))
    total = _number(m.get("total_return"))
    drawdown = _number(m.get("max_drawdown"))
    trades = _number(m.get("trades"))
    is_risk = role_id == "risk_officer" or "风控" in role_tags or "risk" in role_id
    is_perf_role = (
        role_id
        in {
            "backtest_analyst",
            "research_lead",
            "research_auditor",
            "quant_engineer",
            "factor_researcher",
            "portfolio_manager",
        }
        or "回测" in role_tags
        or "研究" in role_tags
        or "量化" in role_tags
    )

    # 1) 风控拦截优先：回撤超限、明确不通过、或手续费口径不清。
    if is_risk or _mentions(body, "风控", "风险官"):
        blocked = _mentions(body, "不通过", "拦截", "拒绝", "必须修正", "超出上限", "超过上限") or (
            drawdown is not None and drawdown >= RISK_DRAWDOWN_LIMIT
        )
        unclear = (
            _mentions(body, "手续费", "费用")
            and _mentions(body, "口径", "缺少", "未说明", "无法判断", "需要补充", "不现实", "不对")
        ) or _mentions(body, "缺少数据", "无法判断", "需要补充")
        if blocked:
            return "no"
        if unclear:
            return "question"

    # 2) 硬核量化梗优先于庆祝；否定句不算命中。
    if _mentions(body, "过拟合", "参数尖峰", "拟合噪声", "overfit") and not _mentions(
        body, "没有过拟合", "未发现过拟合", "不存在过拟合", "no overfit"
    ):
        return "overfit"
    if _mentions(body, "未来函数", "未来数据", "前视", "偷看未来", "lookahead") and not _mentions(
        body, "没有未来函数", "未发现未来函数", "不存在未来函数", "no lookahead"
    ):
        return "future"
    if _mentions(body, "插针", "长下影", "wick") and not _mentions(
        body, "没有插针", "未发现插针", "不存在插针", "no wick"
    ):
        return "wick"

    # 3) 亏损 / 大回撤。
    if is_perf_role and total is not None and total <= SEVERE_LOSS:
        return "cry"
    if is_perf_role and _mentions(body, "严重亏损", "大幅亏损", "亏损严重", "亏麻"):
        return "cry"
    if is_perf_role and drawdown is not None and drawdown >= DRAWDOWN_ALERT:
        return "drawdown"
    if is_perf_role and _mentions(body, "回撤过大", "回撤显著", "最大回撤偏高"):
        return "drawdown"

    # 4) 强结果才庆祝；交易数不足时不夸。
    enough_trades = trades is None or trades >= MIN_TRADES
    if (
        is_perf_role
        and sharpe is not None
        and excess is not None
        and sharpe >= STRONG_SHARPE
        and excess >= STRONG_EXCESS
        and enough_trades
    ):
        return "moon" if excess >= MOON_EXCESS else "alpha"

    # 5) 没有结构化指标时，用文本里的明确结论兜底。
    if _mentions(body, "不通过", "拦截", "拒绝"):
        return "no"
    if _mentions(body, "手续费口径", "缺少数据", "无法判断"):
        return "question"
    return ""


def decorate_qmoji(
    text: str,
    *,
    role: str = "",
    tags: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
) -> str:
    """在文本末尾追加一个 Qmoji；已有表情或没有合适表情时原样返回。"""
    body = (text or "").rstrip()
    if not body or has_qmoji(body):
        return body
    qid = qmoji_for_metrics(body, role=role, tags=tags, metrics=metrics)
    if not qid:
        return body
    return f"{body}\n\n:qbot-{qid}:"


__all__ = ["QMOJI_RE", "decorate_qmoji", "has_qmoji", "metrics_from_materials", "qmoji_for_metrics"]
