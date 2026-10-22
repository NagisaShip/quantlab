"""Describe shared engine settings without maintaining a second set of defaults."""

from __future__ import annotations

from copy import deepcopy

TITLES = {
    "optimizer": "优化方法",
    "risk_model": "风险模型",
    "lookback": "历史窗口",
    "rebalance": "调仓间隔",
    "expected": "预期收益来源",
    "constraints": "组合约束",
    "risk": "风险控制",
    "leverage": "总杠杆",
    "max_weight": "单资产最大权重",
    "min_weight": "单资产最小权重",
    "turnover": "换手",
    "algo": "拆单算法",
    "horizon": "执行时间步",
    "slices": "切片数",
    "style": "执行风格",
    "participation": "参与率",
    "max_participation": "最大参与率",
    "urgency": "紧急程度",
    "display": "冰山显示比例",
    "reprice_bps": "重新报价阈值bp",
    "finish": "到期补齐",
    "state_model": "市场状态模型",
    "fees": "费用",
    "maker_bps": "挂单费率bp",
    "taker_bps": "吃单费率bp",
    "episodes": "仿真片段数",
    "order": "母单",
    "side": "方向",
    "size_pct": "母单成交量比例",
    "notional": "名义金额",
    "impact": "冲击模型",
    "resilience": "恢复速度",
    "permanent": "永久冲击",
    "adapt": "自适应控制",
    "behind": "落后阈值",
    "ahead": "领先阈值",
    "pause_illiquid": "流动性不足时暂停",
    "target_vol": "目标波动",
    "max_drawdown": "最大回撤",
    "gross": "总敞口",
    "long_only": "仅多头",
    "max_leverage": "最大杠杆",
    "risk_aversion": "风险厌恶",
    "timeframe": "K线周期",
}


def engine_schema(domain):
    from .engines import execution, portfolio

    engine = portfolio if domain == "portfolio" else execution
    enums = (
        {"optimizer": list(portfolio.OPTIMIZERS), "risk_model": list(portfolio.RISK_MODELS)}
        if domain == "portfolio"
        else {
            "algo": list(execution.ALGOS),
            "style": list(execution.STYLES),
            "state_model": ["rules", "hmm"],
            "side": ["buy", "sell", "both"],
        }
    )

    def describe(key, value):
        schema = {"title": TITLES.get(key, key), "default": deepcopy(value)}
        if isinstance(value, dict):
            schema.update(
                type="object", properties={k: describe(k, v) for k, v in value.items()}, additionalProperties=True
            )
        elif isinstance(value, bool):
            schema["type"] = "boolean"
        elif isinstance(value, int):
            schema["type"] = "integer"
        elif isinstance(value, float):
            schema["type"] = "number"
        elif isinstance(value, list):
            schema["type"] = "array"
        elif isinstance(value, str):
            schema["type"] = "string"
        else:
            schema.update(widget="json", description="null 表示使用引擎自动设置或关闭此项，具体含义见实现函数。")
        if key in enums:
            schema["enum"] = enums[key]
        return schema

    return {key: describe(key, value) for key, value in engine.DEFAULT_CONFIG.items()}
