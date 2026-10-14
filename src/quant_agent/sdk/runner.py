"""策略加载与执行（回测 / 实时模拟共用同一套生命周期）。

为什么要有一个共享的 runner：同一份策略文件必须能在三种模式下跑出可对照的结果。
如果回测和模拟各写一套调度逻辑，两边一定会漂移，最后没人敢相信对照结论。

加载策略时把 SDK 的公开名字注入到模块命名空间，所以策略文件可以像聚宽那样
直接写 `order_target_percent(...)` 而不必 import——也允许显式 import，两种都支持。

安全边界：策略代码在**独立命名空间**里执行，不注入 `__builtins__` 之外的能力；
`FORBIDDEN_NAMES` 里的名字会被拦截，避免策略直接调用交易所或网络。
"""

from __future__ import annotations

import importlib.util
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import api, trade_mgmt
from .objects import Context, GlobalVars, Portfolio
from .runtime import Runtime, ScheduleEntry, bind, unbind
from .sandbox import StrategyTimeout, TimeGuard, audit, safe_builtins

LOAD_TIME_LIMIT = 5.0  # 模块级代码（常量、辅助函数定义）应当瞬间执行完

# 策略文件里出现这些名字就直接拒绝加载
FORBIDDEN_NAMES = (
    "place_order",
    "cancel_order",
    "withdraw",
    "api_secret",
    "private_key",
    "requests",
    "httpx",
    "urllib",
    "socket",
    "subprocess",
    "os.system",
)

# 注入给策略的 SDK 名字（无需 import 即可用）
INJECTED = [
    "initialize",
    "handle_data",
    "before_trading_start",
    "after_trading_end",
    "on_bar",
    "on_trade",
    "on_fill",
    "on_order_filled",
    "on_timer",
    "snapshot",
    "restore",
    "history",
    "attribute_history",
    "get_price",
    "get_current_data",
    "get_security_info",
    "order",
    "order_target",
    "order_value",
    "order_target_value",
    "order_target_percent",
    "cancel_all",
    "run_daily",
    "run_weekly",
    "run_monthly",
    "run_timer",
    "set_benchmark",
    "set_order_cost",
    "set_slippage",
    "set_option",
    "set_universe",
    "set_stop_loss",
    "set_take_profit",
    "set_trailing_stop",
    "set_roi",
    "log",
    "record",
    "g",
    "get_context",
    "get_param",
    "get_strategy_version",
    "factor",
    "predict",
    "optimize",
    "order_target_weights",
    "set_execution",
    "set_leverage",
    "get_funding_rate",
    "set_protections",
    "set_pairlist",
    "get_universe",
    "is_locked",
]

# 成交回报回调：三个名字收到同一份成交（v2.0 用 on_trade / on_fill，聚宽风格用 on_order_filled）
FILL_CALLBACKS = ("on_trade", "on_fill", "on_order_filled")


class StrategyLoadError(RuntimeError):
    """策略加载失败。消息面向使用者，直接显示在编辑器下方。"""


@dataclass
class StrategyModule:
    """加载好的策略：模块对象 + 生命周期函数 + 源码。"""

    module: types.ModuleType
    source: str
    name: str
    path: str | None = None
    scheduled: list[ScheduleEntry] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    filename: str = ""

    def fn(self, name: str):
        return getattr(self.module, name, None)

    def has(self, name: str) -> bool:
        return callable(getattr(self.module, name, None))


def check_source(source: str) -> list[str]:
    """静态检查策略源码，返回问题列表（空列表 = 通过）。

    两层：关键字黑名单（给出最直白的提示），再加 AST 沙箱审查
    （堵住 `__import__`、`getattr(x, "__class__")` 等绕过写法）。
    """
    issues: list[str] = []
    for token in FORBIDDEN_NAMES:
        if token in source:
            issues.append(f"策略中不允许出现 `{token}`：策略只能通过平台 API 表达仓位意图")
    if "import" in source:
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                module = stripped.split()[1].split(".")[0].rstrip(",")
                if module in {"requests", "httpx", "urllib", "socket", "subprocess", "shutil"}:
                    msg = f"策略中不允许导入 `{module}`：策略运行在无网络沙箱内"
                    if msg not in issues:
                        issues.append(msg)
    if "def " not in source:
        issues.append("策略至少要定义一个函数（通常从 `def initialize(ctx)` 开始）")
    for problem in audit(source):
        # 关键字层已经报过的模块不重复报
        if any(
            f"`{tok}`" in problem for tok in ("requests", "httpx", "urllib", "socket", "subprocess", "shutil")
        ) and any("不允许导入" in i for i in issues):
            continue
        issues.append(problem)
    return issues


def strategy_filename(name: str) -> str:
    """策略代码帧的文件名。时长守卫靠它识别"哪些帧属于策略"。"""
    return f"<strategy:{name}>"


def load_source(
    source: str,
    *,
    name: str = "user_strategy",
    path: str | None = None,
    params: dict[str, Any] | None = None,
) -> StrategyModule:
    """从源码字符串加载策略模块。

    用 `exec` 而不是 `importlib` 是为了不落盘、便于在编辑器里即时运行；
    命名空间里只注入 SDK 公开名字，策略拿不到平台内部对象。
    """
    issues = check_source(source)
    if issues:
        raise StrategyLoadError("；".join(issues))

    module = types.ModuleType(name)
    module.__dict__["__name__"] = name
    module.__dict__["__doc__"] = "由平台加载的策略模块"
    module.__dict__["__builtins__"] = safe_builtins()
    for attr in INJECTED:
        module.__dict__[attr] = getattr(api, attr)

    filename = strategy_filename(name)
    try:
        code = compile(source, filename, "exec")
    except SyntaxError as exc:
        raise StrategyLoadError(f"语法错误：第 {exc.lineno} 行 {exc.msg}") from exc

    try:
        with TimeGuard(filename, LOAD_TIME_LIMIT):
            exec(code, module.__dict__)
    except StrategyTimeout as exc:
        raise StrategyLoadError(f"加载策略超时：{exc}") from exc
    except Exception as exc:
        raise StrategyLoadError(f"加载策略时出错：{type(exc).__name__}: {exc}") from exc

    if not (module.__dict__.get("initialize") or module.__dict__.get("handle_data")):
        # 注入的默认实现总是存在，所以这里判断的是策略有没有覆写
        entry_names = ("def initialize", "def handle_data", "def on_bar")
        if not any(n in source for n in entry_names):
            raise StrategyLoadError("策略必须实现 `initialize(ctx)`、`handle_data(ctx)` 或 `on_bar(ctx)`")

    declared = module.__dict__.get("PARAMS")
    merged: dict[str, Any] = dict(declared) if isinstance(declared, dict) else {}
    if params:
        unknown = sorted(set(params) - set(merged)) if merged else []
        if unknown:
            raise StrategyLoadError(
                f"参数 {', '.join(unknown)} 不在策略的 PARAMS 中；可用参数：{', '.join(sorted(merged)) or '无'}"
            )
        for key, value in params.items():
            merged[key] = _coerce_like(merged.get(key), value)
        module.__dict__["PARAMS"] = merged
    return StrategyModule(module=module, source=source, name=name, path=path, params=merged, filename=filename)


def _coerce_like(template: Any, value: Any) -> Any:
    """参数覆盖时保持声明的类型：PARAMS 里写 12 就是整数，写 0.8 就是浮点数。"""
    if isinstance(template, bool):
        return bool(value)
    if isinstance(template, int) and not isinstance(template, bool):
        return round(float(value))
    if isinstance(template, float):
        return float(value)
    return value


def declared_params(source: str) -> dict[str, Any]:
    """不执行代码，静态读出 `PARAMS = {...}` 的字面量声明（供参数表单与参数优化使用）。"""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return {}
            return dict(value) if isinstance(value, dict) else {}
    return {}


def load_file(path: str | Path) -> StrategyModule:
    """从 .py 文件加载策略。"""
    p = Path(path)
    if not p.exists():
        raise StrategyLoadError(f"策略文件不存在：{p}")
    source = p.read_text(encoding="utf-8")
    return load_source(source, name=p.stem, path=str(p))


def resolve_module_path(module_name: str) -> str | None:
    """辅助：把 `pkg.mod` 解析成文件路径（用于从模块名加载）。"""
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError):
        return None
    return spec.origin if spec else None


class StrategyRunner:
    """在给定 Runtime 上跑一个策略，处理完整生命周期与调度。"""

    def __init__(self, strategy: StrategyModule, runtime: Runtime, ctx: Context) -> None:
        self.strategy = strategy
        self.runtime = runtime
        self.ctx = ctx
        object.__setattr__(ctx, "_g", GlobalVars())
        object.__setattr__(ctx, "_runtime", runtime)
        object.__setattr__(ctx, "strategy_name", strategy.name)
        ctx._seal()
        if strategy.params and not ctx.params:
            ctx.params = dict(strategy.params)
        self.schedules: list[ScheduleEntry] = []
        self.records: dict[str, list[tuple[int, Any]]] = {}
        self.errors: list[str] = []
        self._fired_periods: set[str] = set()  # 周期任务去重（weekly/monthly 每天只触发一次）
        self._timer_last: dict[str, int] = {}  # run_timer 各函数上次触发的时刻
        if strategy.has("confirm_entry") and hasattr(runtime, "confirm_hook"):
            fn = strategy.fn("confirm_entry")
            runtime.confirm_hook = lambda sec, side, amount, price, tag: self.call(
                fn, sec, side, amount, price, tag, default=True
            )

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """调用 initialize，收集策略注册的调度项。"""
        token = bind(self.runtime)
        try:
            # 组合保护 / 动态标的池也可以写成模块常量（Freqtrade 风格）；initialize 里再调 set_* 会覆盖
            for const, setter in (("PROTECTIONS", "set_protections"), ("PAIRLIST", "set_pairlist")):
                val = getattr(getattr(self.strategy, "module", None), const, None)
                if val and hasattr(self.runtime, setter):
                    getattr(self.runtime, setter)(val)
            init = self.strategy.fn("initialize")
            if callable(init):
                init(self.ctx)
            self.schedules = self._collect_schedules()
        except Exception as exc:
            self.errors.append(f"initialize 失败：{type(exc).__name__}: {exc}")
            raise StrategyLoadError(self.errors[-1]) from exc
        finally:
            unbind(token)

    def _collect_schedules(self) -> list[ScheduleEntry]:
        """从 runtime 取策略通过 run_daily 等注册的调度项。"""
        getter = getattr(self.runtime, "schedules", None)
        return list(getter() if callable(getter) else []) or []

    def on_bar(self, ts: int, *, is_first: bool = False, is_last: bool = False) -> None:
        """推进一根 bar：按调度决定这一根要跑哪些函数。"""
        self.ctx.previous_dt = self.ctx.current_dt
        self.ctx.current_dt = ts
        self.ctx.bar_index += 1
        token = bind(self.runtime)
        try:
            guards = getattr(self.runtime, "guards", None)
            if guards is not None and guards.delisted:
                self.runtime.poll_delistings()  # 下架公告到了可见时刻：移出标的池（delist_exit 时平仓）
            if guards is not None and guards.pairlist:
                guards.refresh()  # 动态标的池：到点就按截至现在的数据重选
            if is_first and self.strategy.has("before_trading_start"):
                self._safe("before_trading_start", ts)
            for fn in self._due_functions(ts):
                self._safe(fn, ts)
            # 未注册任何调度时，handle_data 与 on_bar 都是默认入口（两者可同时存在）
            if not self.schedules:
                if self.strategy.has("handle_data"):
                    self._safe("handle_data", ts)
                if self.strategy.has("on_bar"):
                    self._safe("on_bar", ts, bar=self._bar_payload(ts))
            if is_last and self.strategy.has("after_trading_end"):
                self._safe("after_trading_end", ts)
            # 持仓管理：更新最高/最低价与移动止损，再问策略 custom_exit / adjust_position / custom_stoploss
            trade_mgmt.run_callbacks(self)
        finally:
            unbind(token)

    def call(self, fn: Any, *args: Any, default: Any = None) -> Any:
        """调用一个策略回调（第一个参数是 ctx）。出错记进 errors，返回 default。"""
        try:
            return fn(self.ctx, *args)
        except StrategyTimeout:
            raise
        except Exception as exc:
            self.errors.append(
                f"{getattr(fn, '__name__', '回调')} 在第 {self.ctx.bar_index} 根 bar 出错：{type(exc).__name__}: {exc}"
            )
            return default

    def _due_functions(self, ts: int) -> list[str]:
        """判断这一根 bar 该跑哪些已注册函数。

        周期任务（weekly / monthly）在每个"周期实例"里只触发一次：
        用日期去重，否则周一的 24 根小时线会各触发一次，语义就不对了。
        `run_timer` 按收盘时刻的间隔判断，首次注册后先等一个完整间隔再触发。
        """
        from datetime import datetime, timezone

        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        out: list[str] = []
        for entry in self.schedules:
            if entry.period == "timer":
                interval = max(1, int(entry.interval_ms or 0))
                last = self._timer_last.get(entry.func_name)
                if last is None:
                    # 首次见到该调度项：记下起点，等满一个间隔再触发
                    self._timer_last[entry.func_name] = ts
                    continue
                if ts - last < interval:
                    continue
                self._timer_last[entry.func_name] = ts
                out.append(entry.func_name)
                continue
            if entry.period == "daily":
                out.append(entry.func_name)
                continue
            if entry.period == "weekly":
                # weekday 1=周一，Python 的 weekday() 0=周一
                if dt.weekday() != (entry.weekday or 1) - 1:
                    continue
            elif entry.period == "monthly":
                if dt.day != (entry.monthday or 1):
                    continue
            else:
                continue
            key = f"{entry.period}:{entry.func_name}:{day_key}"
            if key in self._fired_periods:
                continue
            self._fired_periods.add(key)
            out.append(entry.func_name)
        return out

    def _bar_payload(self, ts: int) -> dict[str, Any]:
        """给 `on_bar(ctx, bar)` 的 bar 参数：当前时刻各标的的已收盘 bar。

        只用**已经可见**的数据（与 `history` 同一口径，不含未来 bar）。取不到就返回空 dict，
        策略应当以 `ctx.current_dt` 为准，不要把 bar 当成必然存在。
        """
        out: dict[str, Any] = {}
        for sec in getattr(self.ctx, "securities", None) or []:
            try:
                bars = self.runtime.bars(sec, 1)
            except Exception:
                continue
            if bars:
                out[sec] = bars[-1]
        return out

    def _safe(self, fn_name: str, ts: int, *, bar: Any = None) -> None:
        fn = self.strategy.fn(fn_name)
        if not callable(fn):
            return
        try:
            if bar is not None:
                fn(self.ctx, bar)
            else:
                fn(self.ctx)
        except StrategyTimeout:
            raise
        except Exception as exc:
            self.errors.append(f"{fn_name} 在第 {self.ctx.bar_index} 根 bar（{ts}）出错：{exc}")

    def dispatch_trades(self, fills: list[Any]) -> None:
        """成交回报：依次调用策略的 on_trade / on_fill / on_order_filled。

        三个名字是同一份成交的三个入口（v2.0 协议用 on_trade / on_fill，聚宽风格用
        on_order_filled）。策略覆写哪个都能收到，覆写多个会各收到一次。
        """
        if not fills:
            return
        names = [n for n in FILL_CALLBACKS if self.strategy.has(n)]
        if not names:
            return
        token = bind(self.runtime)
        try:
            for f in fills:
                trade = f.to_dict() if hasattr(f, "to_dict") else f
                for n in names:
                    fn = self.strategy.fn(n)
                    try:
                        fn(self.ctx, trade)
                    except StrategyTimeout:
                        raise
                    except Exception as exc:
                        self.errors.append(f"{n} 出错：{type(exc).__name__}: {exc}")
        finally:
            unbind(token)

    # ---------- 检查点钩子 ----------
    def snapshot_state(self) -> Any:
        """调用策略的 `snapshot(ctx)`，拿到策略自己的状态。

        返回 None 表示策略没有实现该钩子，或明确表示没有额外状态。
        钩子抛错不阻断检查点：记进 errors，其余状态照常落库。
        """
        if not self.strategy.has("snapshot"):
            return None
        token = bind(self.runtime)
        try:
            return self.strategy.fn("snapshot")(self.ctx)
        except StrategyTimeout:
            raise
        except Exception as exc:
            self.errors.append(f"snapshot 出错：{type(exc).__name__}: {exc}")
            return None
        finally:
            unbind(token)

    def restore_state(self, state: Any) -> None:
        """把检查点里的策略状态交回 `restore(ctx, state)`。"""
        if state is None or not self.strategy.has("restore"):
            return
        token = bind(self.runtime)
        try:
            self.strategy.fn("restore")(self.ctx, state)
        except StrategyTimeout:
            raise
        except Exception as exc:
            self.errors.append(f"restore 出错：{type(exc).__name__}: {exc}")
        finally:
            unbind(token)

    # ---------- 记录 ----------
    def note_record(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.records.setdefault(k, []).append((self.ctx.current_dt, v))

    def record_series(self) -> dict[str, list[dict[str, Any]]]:
        return {k: [{"t": t, "v": v} for t, v in points] for k, points in self.records.items()}

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.strategy.name,
            "path": self.strategy.path,
            "schedules": [s.__dict__ for s in self.schedules],
            "recorded_series": sorted(self.records),
            "errors": self.errors,
            "globals": self.ctx._g.to_dict(),
            "context": self.ctx.to_dict(),
        }


# ---------------------------------------------------------------------------
# 供引擎复用的账户小工具
# ---------------------------------------------------------------------------
def apply_fill(
    portfolio: Portfolio,
    security: str,
    side: str,
    amount: float,
    price: float,
    fee: float,
) -> None:
    """把一笔成交记入账户的 Decimal 账本（供引擎和测试复用；策略无法调用）。"""
    if amount <= 0:
        return
    portfolio._ledger.trade(instrument=security, side=side, qty=amount, price=price, fee=fee)


def mark_to_market(portfolio: Portfolio, prices: dict[str, float]) -> None:
    """按最新价格重估所有持仓。"""
    portfolio._ledger.mark({k: v for k, v in prices.items() if v})
