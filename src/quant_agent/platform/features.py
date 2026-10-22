"""Workspace-level capability switches for markets, execution and research.

The registry is the single source of truth used by the API, UI, Agent tools and
documentation.  A capability that has not been implemented can never be turned
on by a workspace administrator: silently accepting such a switch would create
the exact "the UI lets me select it but the engine cannot run it" failure mode
this project is designed to avoid.
"""

from __future__ import annotations

from typing import Any

# implementation: ready | research | partial | not_implemented | blocked
FEATURES: dict[str, dict[str, Any]] = {
    "market.spot": {
        "default": True,
        "label": "现货",
        "implementation": "ready",
        "group": "市场",
        "note": "Binance / OKX / Bybit 公共现货行情；回测与模拟交易可用",
    },
    "market.perp_linear": {
        "default": True,
        "label": "线性永续",
        "implementation": "ready",
        "group": "市场",
        "note": "Binance / OKX / Bybit USDT-M 线性永续；账本 v2 支持逐仓/全仓与单向/对冲",
    },
    "market.perp_inverse": {
        "default": False,
        "label": "反向永续",
        "implementation": "research",
        "group": "市场",
        "note": "币本位结算、反向盈亏、合约面值与强平已接入账本；数据与交易所规则仍按研究近似标注",
    },
    "market.delivery": {
        "default": False,
        "label": "交割合约",
        "implementation": "research",
        "group": "市场",
        "note": "到期结算与合约面值已接入账本；交割价来源和交易所规则仍需账单核验",
    },
    "margin.cross": {
        "default": False,
        "label": "全仓保证金",
        "implementation": "ready",
        "group": "保证金",
        "note": "账户级权益、联合维持保证金与一次性全仓强平；简化为单结算币种账户模型",
    },
    "position.hedge": {
        "default": False,
        "label": "对冲模式",
        "implementation": "ready",
        "group": "持仓",
        "note": "独立多空腿、position_side 成交归属、逐腿资金费与强平",
    },
    "execution.real_spot": {
        # 真实资金默认关闭：必须由本地管理员/服务器管理员显式开启，开启前先过二次确认与风险说明。
        "default": False,
        "label": "真实资金现货",
        "implementation": "partial",
        "group": "真实资金",
        "note": "仅微实盘：Binance / OKX 现货、已毕业策略、部署开关与逐次确认；默认关闭，需管理员显式开启",
    },
    "execution.real_perp": {
        "default": False,
        "label": "真实资金永续",
        "implementation": "partial",
        "group": "真实资金",
        "note": "独立服务器开关、密钥隔离、管理员确认码、单笔/总名义/杠杆/日亏限制、交易所止损与自动急停；仅 Binance/OKX 线性 USDT-M",
    },
    "package.qstrat_perp": {
        "default": True,
        "label": ".qstrat 永续包",
        "implementation": "research",
        "group": "导入导出",
        "note": "仅线性 USDT-M 永续；导入时校验标的后缀、合约类型和标准答案",
    },
    "import.freqtrade_short": {
        "default": False,
        "label": "Freqtrade 做空/杠杆",
        "implementation": "research",
        "group": "导入导出",
        "note": "仅限线性永续；现货标的会拒绝做空，杠杆在初始化时设置",
    },
    "research.mentor": {
        "default": True,
        "label": "研究导师课程",
        "implementation": "ready",
        "group": "研究",
        "note": "从研究假设到样本外验证、成本与衍生品风险的课程化检查",
    },
    "data.catalog": {
        "default": True,
        "label": "完整数据目录",
        "implementation": "ready",
        "group": "数据",
        "note": "数据源、字段、覆盖范围、时点可用性与本地缓存状态",
    },
}

_STATUS_LABELS = {
    "ready": "已实现",
    "research": "研究中",
    "partial": "部分实现",
    "not_implemented": "未实现",
    "blocked": "已阻断",
}


def defaults() -> dict[str, bool]:
    return {k: bool(v["default"]) for k, v in FEATURES.items()}


def registry() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, spec in FEATURES.items():
        out[key] = {
            **spec,
            "key": key,
            "status_label": _STATUS_LABELS.get(spec["implementation"], spec["implementation"]),
        }
    return out


def enabled(settings: dict[str, Any] | None) -> dict[str, bool]:
    """Return effective switches; unimplemented/blocked capabilities stay off."""
    out = defaults()
    raw = (settings or {}).get("features") or {}
    for key, value in raw.items():
        spec = FEATURES.get(key)
        if not spec:
            continue
        if spec["implementation"] in ("not_implemented", "blocked"):
            out[key] = False
        else:
            out[key] = bool(value)
    return out


def validate_patch(raw: dict[str, Any] | None) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for key, value in (raw or {}).items():
        spec = FEATURES.get(key)
        if not spec:
            raise ValueError(f"未知功能开关：{key}")
        if value and spec["implementation"] in ("not_implemented", "blocked"):
            raise ValueError(f"{spec['label']} 尚未实现，不能开启（当前状态：{spec['implementation']}）")
        out[key] = bool(value)
    return out


def current() -> dict[str, bool]:
    """Effective switches for the current request workspace."""
    from ..api.deps import get_services
    from . import context

    ws = get_services().auth.workspace(context.current_workspace())
    return enabled(ws.get("settings"))


def require(key: str) -> None:
    from .errors import AppError, ErrorCode

    spec = FEATURES.get(key)
    if spec is None:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"未知功能开关：{key}")
    if not current().get(key):
        raise AppError(
            ErrorCode.PERMISSION_DENIED,
            f"当前工作区未启用「{spec['label']}」；请工作区管理员在设置 → 功能开关中开启",
            details={"feature": key, "implementation": spec["implementation"]},
        )


def require_market(symbol: str, exchange: str = "binance") -> None:
    """Check the market switch for a concrete instrument."""
    from ..exchanges.contracts import contract_type_of

    kind = contract_type_of(str(symbol))
    if kind in ("linear", "inverse", "delivery"):
        if (exchange or "binance").lower() not in ("binance", "okx", "bybit"):
            raise ValueError(f"不支持的衍生品交易所：{exchange}")
        require({"linear": "market.perp_linear", "inverse": "market.perp_inverse", "delivery": "market.delivery"}[kind])
    else:
        require("market.spot")


__all__ = [
    "FEATURES",
    "current",
    "defaults",
    "enabled",
    "registry",
    "require",
    "require_market",
    "validate_patch",
]
