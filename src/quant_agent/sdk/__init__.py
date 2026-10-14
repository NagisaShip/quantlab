"""quant_agent.sdk —— 策略 SDK。

对标聚宽的策略开发体验：一个策略就是一个 `.py` 文件，里面写
`initialize(ctx)` 与若干回调，用 `order_*` 表达目标仓位。
同一份文件在回测、实时模拟、实盘对照三种模式下都不需要改一个字。

最小示例：

    def initialize(ctx):
        set_benchmark("BTC/USDT")
        g.fast, g.slow = 12, 48
        run_daily(rebalance, time="close")

    def rebalance(ctx):
        s = ctx.securities[0]
        closes = history(s, g.slow, "close")
        if len(closes) < g.slow:
            return
        fast = sum(closes[-g.fast:]) / g.fast
        slow = sum(closes) / g.slow
        order_target_percent(s, 0.8 if fast > slow else 0.0)

本地跑一遍：

    from quant_agent.sdk import load_file, run_backtest, RuntimeCost
    result = run_backtest(load_file("strategy.py"), {"BTC/USDT": bars})
    print(result.metrics["total_return"])
"""

from __future__ import annotations

from .api import (
    after_trading_end,
    attribute_history,
    before_trading_start,
    cancel_all,
    factor,
    g,
    get_context,
    get_current_data,
    get_funding_rate,
    get_param,
    get_price,
    get_security_info,
    get_universe,
    handle_data,
    history,
    initialize,
    is_locked,
    log,
    on_order_filled,
    optimize,
    order,
    order_target,
    order_target_percent,
    order_target_value,
    order_target_weights,
    order_value,
    predict,
    record,
    run_daily,
    run_monthly,
    run_weekly,
    set_benchmark,
    set_execution,
    set_leverage,
    set_option,
    set_order_cost,
    set_pairlist,
    set_protections,
    set_roi,
    set_slippage,
    set_stop_loss,
    set_take_profit,
    set_trailing_stop,
    set_universe,
)
from .ledger import InsufficientFunds, Ledger, LedgerError
from .objects import Context, GlobalVars, Portfolio, Position
from .runner import (
    FORBIDDEN_NAMES,
    INJECTED,
    StrategyLoadError,
    StrategyModule,
    StrategyRunner,
    apply_fill,
    check_source,
    declared_params,
    load_file,
    load_source,
    mark_to_market,
)
from .runtime import OrderRequest, OrderResult, Runtime, ScheduleEntry, current
from .runtimes import BacktestRuntime, Fill, RiskConfig, RuntimeCost, Signal, TargetPosition, run_backtest

__all__ = [
    "FORBIDDEN_NAMES",
    "INJECTED",
    "BacktestRuntime",
    "Context",
    "Fill",
    "GlobalVars",
    "InsufficientFunds",
    "Ledger",
    "LedgerError",
    "OrderRequest",
    "OrderResult",
    "Portfolio",
    "Position",
    "RiskConfig",
    "Runtime",
    "RuntimeCost",
    "ScheduleEntry",
    "Signal",
    "StrategyLoadError",
    "StrategyModule",
    "StrategyRunner",
    "TargetPosition",
    "after_trading_end",
    "apply_fill",
    "attribute_history",
    "before_trading_start",
    "cancel_all",
    "check_source",
    "current",
    "declared_params",
    "factor",
    "g",
    "get_context",
    "get_current_data",
    "get_funding_rate",
    "get_param",
    "get_price",
    "get_security_info",
    "get_universe",
    "handle_data",
    "history",
    "initialize",
    "is_locked",
    "load_file",
    "load_source",
    "log",
    "mark_to_market",
    "on_order_filled",
    "optimize",
    "order",
    "order_target",
    "order_target_percent",
    "order_target_value",
    "order_target_weights",
    "order_value",
    "predict",
    "record",
    "run_backtest",
    "run_daily",
    "run_monthly",
    "run_weekly",
    "set_benchmark",
    "set_execution",
    "set_leverage",
    "set_option",
    "set_order_cost",
    "set_pairlist",
    "set_protections",
    "set_roi",
    "set_slippage",
    "set_stop_loss",
    "set_take_profit",
    "set_trailing_stop",
    "set_universe",
]
