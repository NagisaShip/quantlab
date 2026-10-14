"""策略 API（对标聚宽的策略函数集）。

策略文件里这样用：

    from quant_agent.sdk import (
        order_target_percent, run_daily, history, log, g, record,
        set_benchmark, set_order_cost,
    )

    def initialize(ctx):
        set_benchmark("BTC/USDT")
        set_order_cost({"taker_fee": 0.001, "slippage_bps": 5})
        g.fast, g.slow = 12, 48
        run_daily(rebalance, time="close")

    def rebalance(ctx):
        closes = history(ctx.securities[0], g.slow, field="close")
        if len(closes) < g.slow:
            return
        fast_ma = sum(closes[-g.fast:]) / g.fast
        slow_ma = sum(closes) / g.slow
        order_target_percent(ctx.securities[0], 0.8 if fast_ma > slow_ma else 0.0)

约定：
- 所有下单函数只表达**目标**，成交价、费用、滑点由引擎计算。
- `amount` / `value` / `percent` 三种目标口径互不混用，一个函数一种口径。
- 数量为负表示卖出；`order` 与聚宽一致，正数买入、负数卖出。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .runtime import OrderRequest, ScheduleEntry, current

__all__ = [
    "after_trading_end",
    "attribute_history",
    "before_trading_start",
    "cancel_all",
    "factor",
    "g",
    "get_context",
    "get_current_data",
    "get_funding_rate",
    "get_param",
    "get_price",
    "get_security_info",
    "get_strategy_version",
    "get_universe",
    "handle_data",
    "history",
    "initialize",
    "is_locked",
    "log",
    "on_bar",
    "on_fill",
    "on_order_filled",
    "on_timer",
    "on_trade",
    "optimize",
    "order",
    "order_target",
    "order_target_percent",
    "order_target_value",
    "order_target_weights",
    "order_value",
    "predict",
    "record",
    "restore",
    "run_daily",
    "run_monthly",
    "run_timer",
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
    "snapshot",
]


# ---------------------------------------------------------------------------
# 生命周期：只是占位声明，策略文件里覆写它们
# ---------------------------------------------------------------------------
def initialize(ctx: Any) -> None:
    """策略启动时调用一次。用来设置基准、成本、初始化 g、注册定时任务。"""


def handle_data(ctx: Any, data: Any = None) -> None:
    """每根 bar 调用一次（未注册 run_daily 时的默认入口）。"""


def before_trading_start(ctx: Any) -> None:
    """每个交易日/时段开始前调用。"""


def after_trading_end(ctx: Any) -> None:
    """每个交易日/时段结束后调用。"""


def on_order_filled(ctx: Any, order: Any) -> None:
    """成交回报。实时模式下由引擎异步触发。"""


def on_bar(ctx: Any, bar: Any = None) -> None:
    """每根 bar 调用一次。

    与 `handle_data` 是同一个触发点的两个名字：策略覆写任意一个都会在每根 bar 被调用，
    两个都覆写时两个都会被调用。回测与实时模拟的行为完全一致。
    """


def on_trade(ctx: Any, trade: Any) -> None:
    """成交回报（v2.0 协议名）。与 `on_order_filled`、`on_fill` 等价，都会收到同一份成交。"""


def on_fill(ctx: Any, fill: Any) -> None:
    """成交回报（v2.0 协议名）。与 `on_trade`、`on_order_filled` 收到同一份成交。"""


def on_timer(ctx: Any) -> None:
    """定时回调。需要先用 `run_timer(interval_ms)` 注册才会被调用。"""


def snapshot(ctx: Any) -> Any:
    """返回策略自己的可 JSON 序列化状态，随检查点一起落库。

    只在实时模拟（paper）下被调用。返回值必须能被 `json.dumps` 序列化，
    恢复时原样交给 `restore`。返回 None 表示没有额外状态。
    """


def restore(ctx: Any, state: Any) -> None:
    """从检查点恢复策略自己的状态。与 `snapshot` 配对，进程重启后调用一次。"""


# ---------------------------------------------------------------------------
# 上下文与全局变量
# ---------------------------------------------------------------------------
def get_param(name: str, default: Any = None) -> Any:
    """读取策略参数。参数在策略顶部用 `PARAMS = {"fast": 12}` 声明，
    回测 / 参数优化 / 实时模拟时可被覆盖，策略代码不用改。"""
    return current().context().params.get(name, default)


def get_context():
    """取当前运行的 context（等价于聚宽里传进来的 context）。"""
    return current().context()


def get_strategy_version() -> str:
    """当前运行的策略版本 ID（等价于 `ctx.strategy_version_id`）。

    策略可以用它把成交、日志、信号追溯到确切的策略版本。
    """
    return current().context().strategy_version_id


class _GlobalProxy:
    """`g`：每次运行独立的全局变量容器，写法与聚宽一致。"""

    def __getattr__(self, name: str) -> Any:
        return getattr(current().context()._g, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(current().context()._g, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(current().context()._g, name)

    def __repr__(self) -> str:
        return f"g{current().context()._g.to_dict()!r}"


g = _GlobalProxy()


# ---------------------------------------------------------------------------
# 数据访问
# ---------------------------------------------------------------------------
def history(
    security: str,
    count: int,
    field: str | Iterable[str] = "close",
    *,
    skip_paused: bool = True,
    unit: str | None = None,
) -> Any:
    """取最近 count 根**已收盘** bar。

    field 为字符串时返回一维列表；为多个字段时返回 dict[field] -> list。
    `unit="4h"`：把策略自身周期合成更大周期（必须是整数倍），只返回已完整收盘的大周期 bar。
    读别的标的（如用 BTC 判断大盘）：把它也放进标的池，`history("BTC/USDT", 50)` 即可。
    `skip_paused` 保留参数以兼容写法，当前数据源不存在停牌概念。
    """
    _ = skip_paused
    rt = current()
    bars = rt.bars(security, int(count), unit) if unit else rt.bars(security, int(count))
    fields = [field] if isinstance(field, str) else list(field)
    if len(fields) == 1:
        return [b.get(fields[0]) for b in bars]
    return {f: [b.get(f) for b in bars] for f in fields}


def attribute_history(
    security: str,
    count: int,
    unit: str = "1h",
    fields: Iterable[str] = ("close",),
    *,
    df: bool = False,
) -> Any:
    """聚宽同名函数。`unit` 只作说明用途，实际周期由运行配置决定。"""
    _ = unit, df
    return history(security, count, list(fields))


def get_price(
    security: str,
    count: int | None = None,
    fields: Iterable[str] = ("close",),
    *,
    end_dt: int | None = None,
) -> Any:
    """取历史价格。`end_dt` 只接受不大于当前时刻的值——禁止未来数据。"""
    ctx = current().context()
    if end_dt is not None and end_dt > ctx.current_dt:
        raise ValueError(f"get_price 的 end_dt={end_dt} 晚于当前时刻 {ctx.current_dt}；策略不允许读取未来数据")
    return history(security, count or 1, list(fields))


def get_current_data() -> dict[str, dict[str, Any]]:
    """当前可交易标的的即时快照（价格、可交易性）。"""
    rt = current()
    return {s: {"last_price": rt.price(s), "is_tradable": rt.is_tradable(s)} for s in rt.securities()}


def get_security_info(security: str) -> dict[str, Any]:
    """标的基础信息。"""
    base, _, quote = security.partition("/")
    return {"security": security, "base": base or security, "quote": quote or "USDT", "type": "spot"}


# ---------------------------------------------------------------------------
# 交易：只表达目标，成交由引擎负责
# ---------------------------------------------------------------------------
def order(
    security: str,
    amount: float,
    *,
    reason: str = "",
    limit_price: float | None = None,
    expire_bars: int | None = None,
    time_in_force: str = "GTC",
    on_expire: str = "cancel",
) -> Any:
    """按**数量**下单。正数买入，负数卖出（与聚宽一致）。"""
    amount = float(amount)
    if amount == 0:
        return None
    return current().submit(
        OrderRequest(
            security=security,
            side="buy" if amount > 0 else "sell",
            amount=abs(amount),
            reason=reason or "order",
            target_kind="amount",
            target_value=amount,
            limit_price=limit_price,
            expire_bars=expire_bars,
            time_in_force=time_in_force,
            on_expire=on_expire,
        )
    )


def order_target(
    security: str,
    amount: float,
    *,
    reason: str = "",
    limit_price: float | None = None,
    expire_bars: int | None = None,
    time_in_force: str = "GTC",
    on_expire: str = "cancel",
) -> Any:
    """把持仓调到指定**数量**。"""
    target = float(amount)
    held = current().context().position(security).amount
    if abs(target - held) <= 1e-12:
        return None
    return current().submit(
        OrderRequest(
            security=security,
            side="buy" if target > held else "sell",
            amount=abs(target - held),
            reason=reason or "order_target",
            target_kind="amount_target",
            target_value=target,
            limit_price=limit_price,
            expire_bars=expire_bars,
            time_in_force=time_in_force,
            on_expire=on_expire,
        )
    )


def order_value(
    security: str,
    value: float,
    *,
    reason: str = "",
    limit_price: float | None = None,
    expire_bars: int | None = None,
    time_in_force: str = "GTC",
    on_expire: str = "cancel",
) -> Any:
    """按**金额**下单。正数买入，负数卖出。

    注意这是"加减多少钱"，相对当前持仓：`order_value(s, 2000)` 表示再买 2000 元，
    `order_value(s, -1000)` 表示卖出 1000 元。要"把市值调到 2000 元"请用
    `order_target_value`。
    """
    value = float(value)
    if value == 0:
        return None
    return current().submit(
        OrderRequest(
            security=security,
            side="buy" if value > 0 else "sell",
            amount=abs(value),
            reason=reason or "order_value",
            target_kind="value",
            target_value=value,  # 保留符号：金额口径的加减仓方向由它决定
            limit_price=limit_price,
            expire_bars=expire_bars,
            time_in_force=time_in_force,
            on_expire=on_expire,
        )
    )


def order_target_value(
    security: str,
    value: float,
    *,
    reason: str = "",
    limit_price: float | None = None,
    expire_bars: int | None = None,
    time_in_force: str = "GTC",
    on_expire: str = "cancel",
) -> Any:
    """把该标的**市值**调到指定金额。"""
    return current().submit(
        OrderRequest(
            security=security,
            side="buy",
            amount=0.0,
            reason=reason or "order_target_value",
            target_kind="value_target",
            target_value=float(value),
            limit_price=limit_price,
            expire_bars=expire_bars,
            time_in_force=time_in_force,
            on_expire=on_expire,
        )
    )


def order_target_percent(
    security: str,
    percent: float,
    *,
    reason: str = "",
    limit_price: float | None = None,
    expire_bars: int | None = None,
    time_in_force: str = "GTC",
    on_expire: str = "cancel",
) -> Any:
    """把该标的市值调到**总权益的百分之几**。0.8 表示八成仓，0 表示清仓。

    注意：这个函数每次调用都会重新对齐到目标比例。若在每根 bar 都调用它，
    价格波动会让比例持续漂移，从而触发频繁微调。平台的调仓容差
    （`RuntimeCost.rebalance_tolerance`，默认 0.5%）会挡掉这类无意义成交；
    更稳妥的写法是在信号发生变化时才调用。
    """
    return current().submit(
        OrderRequest(
            security=security,
            side="buy",
            amount=0.0,
            reason=reason or "order_target_percent",
            target_kind="percent_target",
            target_value=float(percent),
            limit_price=limit_price,
            expire_bars=expire_bars,
            time_in_force=time_in_force,
            on_expire=on_expire,
        )
    )


def set_leverage(security: str, leverage: float) -> None:
    """设置永续合约（如 `BTC/USDT:PERP`）的杠杆，对之后的开仓生效。逐仓、单向持仓；默认 1 倍。

    永续合约可以做空：`order_target_percent('BTC/USDT:PERP', -0.5)` 表示空头名义价值 = 权益的 50%。
    """
    fn = getattr(current(), "set_leverage", None)
    if not callable(fn):
        raise RuntimeError("当前运行环境不支持永续合约")
    fn(security, float(leverage))


def get_funding_rate(security: str) -> dict[str, Any] | None:
    """永续合约最近一次**已结算**的资金费率：{"ts", "rate"}，正数表示多头付给空头。只看得到当前时刻以前的数据。"""
    fn = getattr(current(), "funding_rate", None)
    return fn(security) if callable(fn) else None


def cancel_all(security: str | None = None) -> int:
    """撤销未成交的委托（排队中的市价单与挂出的限价单）。返回撤销笔数。"""
    rt = current()
    fn = getattr(rt, "cancel_all", None)
    n = fn(security) if callable(fn) else 0
    rt.log("info", f"cancel_all：撤销 {n} 笔未成交委托")
    return n


def set_stop_loss(security: str, pct: float) -> None:
    """为该标的设置止损（相对持仓均价下跌 pct，如 0.05 = 5%）。同根 bar 同时触及止盈时止损优先。"""
    if not 0 <= float(pct) < 1:
        raise ValueError("止损比例应在 [0, 1) 之间")
    current().set_bracket(security, stop_loss=float(pct))


def set_trailing_stop(security: str | None, pct: float, *, activate: float = 0.0) -> None:
    """移动止损：持仓期间最高价（空头为最低价）回撤 pct 就平仓，止损价只会收紧。

    `activate=0.02` 表示浮盈达到 2% 才启动。security 传 None 对所有标的生效。
    每根 bar 收盘后更新，从下一根开始生效（不会用同一根的最高价去止损同一根）。
    """
    if not 0 < float(pct) < 1:
        raise ValueError("移动止损比例应在 (0, 1) 之间")
    current().set_trailing(security or "*", float(pct), float(activate))


def set_roi(table: dict[int, float], security: str | None = None) -> None:
    """分段止盈表 {持有分钟: 目标收益率}，持有越久要求越低。

    例：`set_roi({0: 0.06, 60: 0.03, 240: 0.0})`：开仓 1 小时内赚 6% 就走，1–4 小时赚 3% 就走，4 小时后不亏就走。
    """
    current().set_roi(dict(table or {}), security)


def set_protections(items: list[dict[str, Any]]) -> None:
    """组合保护规则（只拦开仓，不拦平仓）。例：

        set_protections([
            {"method": "StoplossGuard", "lookback_bars": 24, "trade_limit": 3, "stop_bars": 12},
            {"method": "CooldownPeriod", "stop_bars": 2},
        ])

    可用：CooldownPeriod、StoplossGuard、MaxDrawdown、LowProfitPairs，参数见文档。
    """
    current().set_protections(list(items or []))


def set_pairlist(cfg: dict[str, Any]) -> None:
    """动态标的池：从候选里按成交额、波动率、上市时长等过滤，每隔 refresh_bars 根重选；下架公告一到就移除。"""
    current().set_pairlist(dict(cfg or {}))


def get_universe() -> list[str]:
    """当前可开新仓的标的（动态标的池选中的、且没有下架公告）。没配标的池时就是全部标的。"""
    rt = current()
    fn = getattr(rt, "universe", None)
    return list(fn()) if callable(fn) else list(rt.securities())


def is_locked(security: str | None = None) -> bool:
    """这个标的（不传则看全局）现在是否被保护规则锁住、不能开新仓。"""
    g = getattr(current(), "guards", None)
    if g is None or not g.protections:
        return False
    return g.active_lock(security or "*") is not None


def set_take_profit(security: str, pct: float) -> None:
    """为该标的设置止盈（相对持仓均价上涨 pct）。"""
    if float(pct) < 0:
        raise ValueError("止盈比例不能为负")
    current().set_bracket(security, take_profit=float(pct))


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def run_daily(func: Any, time: str = "every_bar", **kwargs: Any) -> None:
    """每个 bar 或指定时刻运行一次。

    time 取值：`open`（bar 开始）、`close`（bar 收盘）、`every_bar`、`HH:MM`。
    """
    _ = kwargs
    current().schedule(ScheduleEntry(func_name=func.__name__, period="daily", time=time))


def run_weekly(func: Any, weekday: int = 1, time: str = "open", **kwargs: Any) -> None:
    """每周指定星期几运行（weekday：1=周一 … 7=周日）。"""
    _ = kwargs
    current().schedule(ScheduleEntry(func_name=func.__name__, period="weekly", time=time, weekday=int(weekday)))


def run_monthly(func: Any, monthday: int = 1, time: str = "open", **kwargs: Any) -> None:
    """每月指定日期运行（monthday：1…28）。"""
    _ = kwargs
    current().schedule(ScheduleEntry(func_name=func.__name__, period="monthly", time=time, monthday=int(monthday)))


def run_timer(interval_ms: int, func: Any = None, **kwargs: Any) -> None:
    """按固定时间间隔运行（毫秒）。

    `func` 省略时改为调度策略里的 `on_timer(ctx)`——这是 v2.0 协议里的定时回调。
    间隔按 bar 的收盘时刻判断，因此实际触发粒度不会细于一根 bar。
    """
    _ = kwargs
    name = (getattr(func, "__name__", None) or "on_timer") if callable(func) else "on_timer"
    current().schedule(ScheduleEntry(func_name=name, period="timer", time="every_bar", interval_ms=int(interval_ms)))


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def set_benchmark(security: str) -> None:
    """设置比较基准，用于计算超额收益。"""
    current().set_benchmark(security)


def set_order_cost(
    cost: dict[str, float] | None = None,
    *,
    type: str = "crypto",
    **kwargs: float,
) -> None:
    """设置手续费与滑点。

    可传 dict，也可用关键字：`set_order_cost(taker_fee=0.001, slippage_bps=5)`。
    这些是**实验假设**，不代表交易所实际收费。
    """
    merged: dict[str, float] = {}
    if cost:
        merged.update({k: float(v) for k, v in cost.items()})
    merged.update({k: float(v) for k, v in kwargs.items()})
    _ = type
    current().set_order_cost(merged)


def set_slippage(bps: float) -> None:
    """设置滑点（基点）。"""
    current().set_slippage(float(bps))


def set_execution(name: str) -> None:
    """按「组合与执行 → 下单执行」里保存的执行方案计成交成本。

    价差 + 滑点改为 a + b·√(下单量 / 当根成交量)（bp），手续费仍按回测的成本设置。
    只在平台回测里生效（回测前从方案库读出成本曲线）；其它场景保持原来的成本设置。
    """
    rt = current()
    fn = getattr(rt, "research_table", None)
    tbl = fn("execution", name) if fn else None
    setter = getattr(rt, "set_impact_model", None)
    if not tbl or setter is None:
        return
    setter(tbl)


def set_option(name: str, value: Any) -> None:
    """设置运行选项，例如 `set_option("avoid_future_data", True)`。"""
    current().set_option(name, value)


def set_universe(securities: str | Iterable[str]) -> None:
    """设置标的池。传字符串或字符串列表。"""
    ctx = current().context()
    ctx.securities = [securities] if isinstance(securities, str) else list(securities)


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
class _Logger:
    """`log.info(...)` 风格的日志器。"""

    def __getattr__(self, level: str):
        if level not in {"debug", "info", "warning", "warn", "error", "critical"}:
            raise AttributeError(f"未知日志级别：{level}")
        real = "warning" if level == "warn" else level

        def emit(*parts: Any, sep: str = " ", **kwargs: Any) -> None:
            _ = kwargs
            current().log(real, sep.join(str(p) for p in parts))

        return emit


log = _Logger()


def record(**values: Any) -> None:
    """把自定义指标画到回测图上。例如 `record(fast_ma=fast, slow_ma=slow)`。"""
    current().record(dict(values))


# ---------------------------------------------------------------------------
# 研究成果：因子库、模型库、组合方案（平台回测前按时间点算好，见 research/signals.py）
# ---------------------------------------------------------------------------
def _research(kind: str, name: str, security: str | None) -> Any:
    rt = current()
    fn = getattr(rt, "research_value", None)
    if fn is None:
        rt.log("warning", f"{kind}() 只在平台回测里可用")
        return None if security else {}
    return fn(kind, name, security)


def factor(name: str, security: str | None = None) -> Any:
    """读取因子在当前 bar 的值。name 是因子库里的名字，或直接写因子表达式。

    不传 security 时返回 {标的: 值}（没有值的标的不出现）；传了返回单个数或 None。
    只用到已收盘的数据，和因子库里的评估同口径。
    """
    return _research("factor", name, security)


def predict(name: str, security: str | None = None) -> Any:
    """读取模型库里某个模型在当前 bar 的预测。回测中按“滚动重训”生成：每隔一段用当时之前的数据重训，
    最早一段训练窗口内返回空。分类模型返回上涨概率，回归模型返回预测值。"""
    return _research("predict", name, security)


def optimize(
    name: str, expected: dict[str, float] | None = None, *, securities: list[str] | None = None
) -> dict[str, float]:
    """按组合与风控里保存的配置方案算目标权重：{标的: 权重}。

    expected 是预期收益（例如 predict() 的结果）；不传时按方案自己的设置（等风险、最小方差等不需要预期收益）。
    风险模型用当前 bar 之前的收益历史估计，约束（单币上限、总杠杆、现金下限）都在方案里。
    """
    rt = current()
    fn = getattr(rt, "research_table", None)
    cfg = fn("optimize", name) if fn else None
    if not cfg:
        return {}
    try:
        from ..research.portfolio import optimize_live
    except ImportError:
        rt.log("warning", "optimize() 需要 numpy")
        return {}
    secs = list(securities or rt.securities())
    lookback = int((cfg.get("config") or {}).get("lookback") or 120)
    closes = {s: [b["close"] for b in rt.bars(s, lookback + 1)] for s in secs}
    return optimize_live(cfg.get("config") or {}, closes, expected or {})


def order_target_weights(
    weights: dict[str, float],
    *,
    algo: str | None = None,
    slices: int | None = None,
    securities: list[str] | None = None,
) -> list[Any]:
    """按目标权重调仓：weights 里没有的标的调到 0。algo / slices 记录执行意图（回测按平台成本模型成交，
    拆单执行的效果在「组合与执行 → 下单执行」里单独研究），不改变回测的成交口径。"""
    if algo:
        current().log("info", f"执行意图：{algo}" + (f" × {slices} 片" if slices else ""))
    out = []
    universe = list(securities or current().securities())
    for sec in universe:
        if sec not in weights:
            out.append(order_target_percent(sec, 0.0))
    for sec, w in sorted(weights.items(), key=lambda kv: kv[1]):  # 先减仓再加仓，腾出现金
        out.append(order_target_percent(sec, float(w)))
    return out
