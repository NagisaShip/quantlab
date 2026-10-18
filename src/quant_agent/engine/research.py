"""研究服务：回测规格 → 取数 → 执行 → 清单；参数优化（样本内 / 样本外）。

对标聚宽"运行回测 / 参数调优"，但把可复现与防过拟合做成默认行为：
- 每次回测生成一份 manifest：代码哈希、参数、数据摘要、成本与成交模型、平台版本；
- 参数优化强制切分样本内 / 样本外，只用样本内挑参数，样本外只用来"验"，
  并给出秩相关、最优组合样本外排名等过拟合诊断，而不是只报一个最高收益。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..data.quality import TIMEFRAME_MS
from ..data.service import DataUnavailable
from ..exchanges.base import Bar
from ..sdk import RuntimeCost, run_backtest
from ..sdk.runtimes import LocalRunResult, RiskConfig

PLATFORM_VERSION = "0.1.0-preview"
BACKTEST_TIME_LIMIT = 60.0
OPT_RUN_TIME_LIMIT = 20.0
MAX_COMBINATIONS = 150
OBJECTIVES = {
    "sharpe": "夏普比率",
    "total_return": "累计收益",
    "calmar": "Calmar 比率",
    "sortino": "索提诺比率",
    "excess_return": "超额收益",
}


@dataclass
class BacktestSpec:
    securities: list[str] = field(default_factory=lambda: ["BTC/USDT"])
    timeframe: str = "1h"
    start: int | None = None
    end: int | None = None
    bars_limit: int = 1500
    warmup_bars: int = 100
    initial_cash: float = 10_000.0
    initial_currency: str = "USDT"
    taker_fee: float = 0.001
    slippage_bps: float = 5.0
    spread_bps: float = 2.0
    # 成本压力倍数（2 = 手续费、价差、滑点都翻倍）。在撮合时统一生效，策略自己 set_order_cost 也照样放大
    cost_scale: float = 1.0
    rebalance_tolerance: float = 0.005
    fill_at: str = "next_open"
    # 微结构成交：tick = 用 OHLC 路径模拟盘中逐笔；配合 50ms/1s 周期即子秒级撮合
    execution_mode: str = "bar"
    dynamic_slippage: bool = False
    vol_slippage: float = 0.25
    impact_a_bps: float = 0.0
    impact_b_bps: float = 0.0
    limit_move_pct: float = 0.0
    min_liquidity_usd: float = 0.0
    exchange: str = "binance"
    margin_mode: str = "isolated"
    position_mode: str = "one-way"
    contract_type: str = "linear"
    expiry_ts: int | None = None
    ledger_v2: bool = False
    source: str = "auto"
    benchmark: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    insufficient_funds: str = "reject"
    fee_currency: str = "quote"
    risk: dict[str, Any] = field(default_factory=dict)
    # 资金流：显式入金 / 出金，或定期定额入金（定投）。收益按单位净值（时间加权）计算，入金不算收益。
    cash_flows: list[dict[str, Any]] = field(default_factory=list)
    recurring_deposit: float = 0.0
    recurring_every_days: int = 0

    def flows_for(self, bars: dict[str, list[Bar]]) -> list[dict[str, Any]]:
        flows = [dict(f) for f in self.cash_flows]
        if self.recurring_deposit and self.recurring_every_days:
            timeline = sorted({b.close_time for series in bars.values() for b in series})
            if timeline:
                start = timeline[min(self.warmup_bars, len(timeline) - 1)]
                step = self.recurring_every_days * 86_400_000
                t = start + step
                while t <= timeline[-1]:
                    flows.append({"ts": t, "amount": self.recurring_deposit, "ref": f"recurring:{t}"})
                    t += step
        return flows

    def cost(self) -> RuntimeCost:
        return RuntimeCost(
            taker_fee=self.taker_fee,
            slippage_bps=self.slippage_bps,
            spread_bps=self.spread_bps,
            rebalance_tolerance=self.rebalance_tolerance,
            fill_at=self.fill_at,
            execution_mode=self.execution_mode,
            dynamic_slippage=self.dynamic_slippage,
            vol_slippage=self.vol_slippage,
            impact_a_bps=self.impact_a_bps,
            impact_b_bps=self.impact_b_bps,
            limit_move_pct=self.limit_move_pct,
            min_liquidity_usd=self.min_liquidity_usd,
            insufficient_funds=self.insufficient_funds,
            fee_currency=self.fee_currency,
        )

    def risk_config(self) -> RiskConfig:
        return RiskConfig.from_dict(self.risk)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_market(market: Any, spec: BacktestSpec) -> tuple[dict[str, list[Bar]], list[dict[str, Any]], list[str]]:
    """按规格取数。返回 (各标的 bar, 数据摘要, 警示)。

    指定起止日期时，自动向前多取 `warmup_bars` 根作为预热，保证计分从起始日开始。
    """
    from ..exchanges.contracts import SUPPORTED_PERP_EXCHANGES, contract_type_of

    if any(contract_type_of(sec) in ("linear", "inverse", "delivery") for sec in spec.securities):
        if (spec.exchange or "binance").lower() not in SUPPORTED_PERP_EXCHANGES:
            raise DataUnavailable(f"不支持的衍生品交易所：{spec.exchange}")
        if any(contract_type_of(sec) == "delivery" for sec in spec.securities) and not spec.expiry_ts:
            raise DataUnavailable("交割合约必须指定 expiry_ts（到期时间，毫秒 UTC）")
    step = TIMEFRAME_MS.get(spec.timeframe, 3_600_000)
    start = spec.start - spec.warmup_bars * step if spec.start else None
    bars: dict[str, list[Bar]] = {}
    summaries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for sec in spec.securities:
        ds = market.dataset(
            sec,
            spec.timeframe,
            limit=spec.bars_limit,
            exchange=spec.exchange,
            start=start,
            end=spec.end,
            source=spec.source,
        )
        bars[sec] = ds.bars
        summaries.append(ds.summary())
        if ds.coverage_warning:
            warnings.append(ds.coverage_warning)
    sources = {s["source"] for s in summaries}
    if len(sources) > 1:
        warnings.append("各标的的数据来源不一致（部分为合成数据），组合结果不能作为研究结论")
    if "synthetic" in sources:
        warnings.append("使用了教学合成数据：只能验证流程与代码，不能据此判断策略好坏")
    return bars, summaries, warnings


def load_funding(
    market: Any, spec: BacktestSpec, bars: dict[str, list[Bar]], datasets: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """永续合约资金费事件表（结算时刻、费率、标记价、来源、摘要）。

    回测在结算时刻逐笔结算；标记价用于资金费名义价值与强平报告。取不到标记价时保留空值，
    运行时回退到 bar 价格并在报告里明确标注，不编造价格。
    """
    tables: dict[str, Any] = {}
    summaries: list[dict[str, Any]] = []
    warnings: list[str] = []
    from ..exchanges.contracts import contract_type_of

    synthetic = {d.get("symbol") for d in datasets or [] if d.get("source") == "synthetic"}
    for sec, rows in bars.items():
        if contract_type_of(sec) not in ("linear", "inverse") or not rows:
            continue
        src = "synthetic" if sec in synthetic or spec.source == "synthetic" else spec.source
        events: list[dict[str, Any]] = []
        if hasattr(market, "funding_events"):
            events, label, err = market.funding_events(
                sec, exchange=spec.exchange, start=rows[0].open_time, end=rows[-1].close_time, source=src
            )
        elif hasattr(market, "funding_rates"):
            pairs, label, err = market.funding_rates(
                sec, exchange=spec.exchange, start=rows[0].open_time, end=rows[-1].close_time, source=src
            )
            events = [{"ts": t, "rate": r, "mark_price": ""} for t, r in pairs]
        else:
            events, label, err = [], "", "行情服务不支持资金费率"
        if events:
            payload = [[int(e["ts"]), str(e["rate"]), str(e.get("mark_price") or "")] for e in events]
            digest = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
            tables[f"funding:{sec}"] = {
                "times": [int(e["ts"]) for e in events],
                "rates": [str(e["rate"]) for e in events],
                "mark_prices": [str(e.get("mark_price") or "") for e in events],
                "interval_hours": 8,
                "source": label,
                "digest": digest,
            }
            summaries.append(
                {
                    "symbol": sec,
                    "kind": "funding",
                    "source": label,
                    "rows": len(events),
                    "mark_prices": sum(1 for e in events if e.get("mark_price")),
                    "digest": digest,
                    "start_ts": events[0]["ts"],
                    "end_ts": events[-1]["ts"],
                }
            )
            span = rows[-1].close_time - rows[0].open_time
            if label == "exchange" and len(events) < span / (8 * 3_600_000) * 0.8:
                warnings.append(
                    f"{sec} 的资金费率只取到 {len(events)} 条，早于这些记录的时段按 0 计（OKX 只保留约 3 个月的历史费率）"
                )
            if not any(e.get("mark_price") for e in events):
                warnings.append(f"{sec} 的资金费率没有标记价，结算回退到 bar 开盘/收盘价——资金费结果按简化模型计")
            if label == "synthetic":
                warnings.append(f"{sec} 用的是合成资金费率，只能验证流程")
        else:
            warnings.append(f"{sec} 没有取到资金费率（{err or '无数据'}），资金费按 0 计——资金费相关策略的结果不可信")
    return tables, summaries, warnings


def execute(
    source: str,
    *,
    name: str,
    bars: dict[str, list[Bar]],
    spec: BacktestSpec,
    params: dict[str, Any] | None = None,
    time_limit: float = BACKTEST_TIME_LIMIT,
    run_id: str = "local",
    strategy_version_id: str = "",
    lang: str = "python",
    research: dict[str, Any] | None = None,
) -> LocalRunResult:
    from ..strategies.lang import load

    module = load(lang, source, name=name, params=params if params is not None else (spec.params or None))
    warmup = spec.warmup_bars
    compiled = getattr(module, "compiled", None)
    if compiled is not None and warmup < compiled.warmup_bars:
        warmup = compiled.warmup_bars  # DSL 自动计算所需预热，保证第一个计分 bar 上指标已就绪
    return run_backtest(
        module,
        bars,
        initial_cash=spec.initial_cash,
        initial_currency=spec.initial_currency,
        cost=spec.cost(),
        warmup_bars=warmup,
        timeframe=spec.timeframe,
        benchmark=spec.benchmark,
        time_limit=time_limit,
        run_id=run_id,
        strategy_version_id=strategy_version_id,
        risk=spec.risk_config(),
        cash_flows=spec.flows_for(bars),
        research=research,
        cost_scale=spec.cost_scale,
        exchange=spec.exchange,
        margin_mode=spec.margin_mode,
        position_mode=spec.position_mode,
        contract_type=spec.contract_type,
        expiry_ts=spec.expiry_ts,
        ledger_v2=spec.ledger_v2,
    )


def manifest(source: str, spec: BacktestSpec, datasets: list[dict[str, Any]], result: Any) -> dict:
    """运行清单：复现这次回测所需的一切。result 可以是 LocalRunResult 或它的 to_dict()。"""
    if not isinstance(result, dict):
        result = {
            "params": result.params,
            "benchmark": result.benchmark,
            "cost_model": result.cost_model,
            "risk": result.risk,
            "assumptions": result.assumptions,
        }
    return {
        "platform_version": PLATFORM_VERSION,
        "code_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "params": result.get("params"),
        "securities": spec.securities,
        "benchmark": result.get("benchmark"),
        "timeframe": spec.timeframe,
        "data": [
            {
                k: d.get(k)
                for k in ("symbol", "source", "source_label", "digest", "bars", "start_ts", "end_ts", "exchange")
            }
            for d in datasets
        ],
        "cost_model": result.get("cost_model"),
        "risk": result.get("risk"),
        "fill_model": FILL_MODELS.get("tick" if spec.execution_mode == "tick" else spec.fill_at, spec.fill_at),
        "margin_mode": spec.margin_mode,
        "position_mode": spec.position_mode,
        "contract_type": spec.contract_type,
        "expiry_ts": spec.expiry_ts,
        "warmup_bars": spec.warmup_bars,
        "initial_cash": spec.initial_cash,
        "generated_at": int(time.time() * 1000),
    }


FILL_MODELS = {
    "next_open": "下一根 bar 开盘价 ±（半价差 + 滑点）",
    "close": "当根 bar 收盘价 ±（半价差 + 滑点）",
    "tick": "逐笔路径撮合：OHLC 路径 VWAP + 动态滑点/冲击；50ms~1s 周期即子秒级撮合",
}

SUMMARY_METRICS = (
    "total_return",
    "annualized_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "volatility",
    "win_rate",
    "benchmark_return",
    "simple_benchmark_return",
    "excess_vs_simple",
    "beats_simple",
    "excess_return",
    "alpha",
    "beta",
    "information_ratio",
    "trades",
    "fills",
    "calmar",
    "daily_win_rate",
    "profit_factor",
    "payoff_ratio",
    "turnover",
    "total_fees",
    "open_positions",
    "unrealized_pnl",
    "worst_day",
    "avg_holding_hours",
    "return_basis",
)


def summarize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {k: metrics.get(k) for k in SUMMARY_METRICS}


# ---------------------------------------------------------------------------
# 参数优化
# ---------------------------------------------------------------------------
def expand_grid(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = sorted(grid)
    values = [list(dict.fromkeys(grid[k])) for k in keys]
    if not keys or any(not v for v in values):
        return []
    return [dict(zip(keys, combo, strict=True)) for combo in itertools.product(*values)]


def _timeline(bars: dict[str, list[Bar]]) -> list[int]:
    return sorted({b.close_time for series in bars.values() for b in series})


def _slice(bars: dict[str, list[Bar]], lo_ts: int | None, hi_ts: int | None) -> dict[str, list[Bar]]:
    """取 (lo_ts, hi_ts] 区间的 bar。"""
    return {
        s: [b for b in series if (lo_ts is None or b.close_time > lo_ts) and (hi_ts is None or b.close_time <= hi_ts)]
        for s, series in bars.items()
    }


def split_bars(bars: dict[str, list[Bar]], ratio: float, warmup: int, *, embargo: int = 0) -> tuple[dict, dict, int]:
    """按统一时间轴切样本内 / 样本外。

    样本外向前带上 warmup 根预热（不计分）；embargo>0 时，切点之后再空出 embargo 根不计分
    （purge / embargo：避免指标窗口跨越切点造成的信息泄漏）。
    """
    timeline = _timeline(bars)
    cut_idx = max(warmup + 2, min(len(timeline) - 3 - embargo, int(len(timeline) * ratio)))
    cut_ts = timeline[cut_idx]
    oos_from = timeline[max(0, cut_idx + embargo - warmup)]
    ins = _slice(bars, None, cut_ts)
    oos = _slice(bars, oos_from, None)
    return ins, oos, cut_ts


def lock_holdout(bars: dict[str, list[Bar]], ratio: float, warmup: int) -> tuple[dict, dict | None, int | None]:
    """切出锁定测试区间（末尾 ratio）。调参过程完全看不到它，只在选定参数后评估一次。"""
    if not ratio:
        return bars, None, None
    timeline = _timeline(bars)
    lock_idx = max(warmup + 10, int(len(timeline) * (1 - ratio)))
    if lock_idx >= len(timeline) - 3:
        raise ValueError("锁定测试区间太短：请增加数据量或降低锁定比例")
    lock_ts = timeline[lock_idx]
    dev = _slice(bars, None, lock_ts)
    test = _slice(bars, timeline[max(0, lock_idx - warmup)], None)
    return dev, test, lock_ts


def _objective(metrics: dict[str, Any], key: str) -> float | None:
    v = metrics.get(key)
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return float(v)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else None


def normalize_space(space: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """参数空间统一成 {名字: {"low", "high", "step"?, "log"?} | {"choices": [...]}}。

    源码里可以写 `PARAM_SPACE = {"fast": (5, 50), "slow": (20, 200, 5), "mode": ["a", "b"]}`：
    元组是范围（第三个数是步长），列表是候选值。
    """
    out: dict[str, dict[str, Any]] = {}
    for k, v in (space or {}).items():
        if isinstance(v, dict):
            d = dict(v)
        elif isinstance(v, tuple) and len(v) in (2, 3):
            d = {"low": v[0], "high": v[1], **({"step": v[2]} if len(v) == 3 else {})}
        elif isinstance(v, list) and v:
            d = {"choices": list(v)}
        else:
            raise ValueError(f"参数 {k} 的范围写法不对：写 (下限, 上限[, 步长]) 或 [候选值...]")
        if "choices" in d:
            if not d["choices"]:
                raise ValueError(f"参数 {k} 没有候选值")
        else:
            lo, hi = d.get("low"), d.get("high")
            if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) or lo >= hi:
                raise ValueError(f"参数 {k} 的范围要满足 下限 < 上限")
            d["int"] = all(isinstance(x, int) and not isinstance(x, bool) for x in (lo, hi, d.get("step", 1)))
        out[str(k)] = d
    if not out:
        raise ValueError("智能调参需要参数范围：在策略里写 PARAM_SPACE = {...}，或在请求里给 space")
    return out


def auto_space(params: dict[str, Any]) -> dict[str, Any]:
    """策略没写 PARAM_SPACE 时，按 PARAMS 的默认值自动给范围：整数取 [默认/2, 默认×2]，
    小数取 [默认×0.5, 默认×1.5]，布尔取两值；其余（字符串等）不调。"""
    out: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if isinstance(v, bool):
            out[k] = [True, False]
        elif isinstance(v, int) and v > 1:
            out[k] = (max(1, v // 2), v * 2)
        elif isinstance(v, float) and v > 0:
            out[k] = (round(v * 0.5, 6), round(v * 1.5, 6))
    return out


def declared_space(source: str) -> dict[str, Any]:
    """读源码顶层的 PARAM_SPACE 字面量（不执行代码）。"""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PARAM_SPACE" for t in node.targets
        ):
            try:
                val = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return {}
            return val if isinstance(val, dict) else {}
    return {}


def _bayes_search(
    evaluate: Any,
    space: dict[str, dict[str, Any]],
    *,
    n_trials: int,
    seed: int,
    objective: str,
    multi: bool,
    done: list[dict[str, Any]],
    progress: Any,
) -> list[dict[str, Any]]:
    """optuna 搜索；返回帕累托前沿（多目标）的参数列表，单目标返回空。"""
    try:
        import optuna
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作的提示
        raise ValueError("智能调参需要安装 optuna：pip install optuna") from exc
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    if multi:
        sampler: Any = optuna.samplers.NSGAIISampler(seed=seed, population_size=max(8, n_trials // 5))
        study = optuna.create_study(directions=["maximize", "minimize"], sampler=sampler)
    else:
        sampler = optuna.samplers.TPESampler(seed=seed, n_startup_trials=min(10, n_trials // 3))
        study = optuna.create_study(direction="maximize", sampler=sampler)

    def dists() -> dict[str, Any]:
        out = {}
        for k, d in space.items():
            if "choices" in d:
                out[k] = optuna.distributions.CategoricalDistribution(d["choices"])
            elif d["int"]:
                out[k] = optuna.distributions.IntDistribution(int(d["low"]), int(d["high"]), step=int(d.get("step", 1)))
            else:
                out[k] = optuna.distributions.FloatDistribution(
                    float(d["low"]), float(d["high"]), step=d.get("step"), log=bool(d.get("log"))
                )
        return out

    def score(row: dict[str, Any]) -> Any:
        m = row.get("in_sample") or {}
        v = _objective(m, objective)
        v = -1e9 if v is None else v
        dd = m.get("max_drawdown")
        dd = 1.0 if dd is None else float(dd)
        return (v, dd) if multi else v

    D = dists()
    replayed = 0
    for row in done:  # 检查点恢复：已算完的试验原样放回，不重算
        p = row.get("params") or {}
        if set(p) == set(D):
            try:
                study.add_trial(
                    optuna.trial.create_trial(
                        params=p,
                        distributions=D,
                        values=list(score(row)) if multi else None,
                        value=None if multi else score(row),
                    )
                )
                evaluate(p)
                replayed += 1
            except ValueError:
                continue

    def obj(trial: Any) -> Any:
        params: dict[str, Any] = {}
        for k, d in space.items():
            if "choices" in d:
                params[k] = trial.suggest_categorical(k, d["choices"])
            elif d["int"]:
                params[k] = trial.suggest_int(k, int(d["low"]), int(d["high"]), step=int(d.get("step", 1)))
            else:
                params[k] = trial.suggest_float(
                    k, float(d["low"]), float(d["high"]), step=d.get("step"), log=bool(d.get("log"))
                )
        row = evaluate(params)
        if progress:
            progress(len(study.trials), n_trials)
        return score(row)

    study.optimize(obj, n_trials=max(0, n_trials - replayed), catch=(Exception,))
    if multi:
        return [t.params for t in study.best_trials]
    return []


def optimize(
    source: str,
    *,
    name: str,
    bars: dict[str, list[Bar]],
    spec: BacktestSpec,
    grid: dict[str, list[Any]],
    split: float = 0.7,
    objective: str = "sharpe",
    progress: Any = None,
    embargo_bars: int = 0,
    holdout: float = 0.0,
    done_rows: dict[str, dict[str, Any]] | None = None,
    on_row: Any = None,
    lang: str = "python",
    method: str = "grid",
    space: dict[str, Any] | None = None,
    n_trials: int = 0,
    seed: int = 42,
    multi_objective: bool = False,
) -> dict[str, Any]:
    """参数搜索 + 样本外检验；可选 purge/embargo 间隔与锁定测试区间。

    method="grid"：网格穷举；method="bayes"：optuna 贝叶斯搜索（TPE），在 space 声明的范围里试 n_trials 组；
    multi_objective=True 时用 NSGA-II 同时最大化目标、最小化最大回撤，最优取帕累托前沿里目标最高的那组。
    两种方法都只用样本内挑参数，样本外和锁定区间只用来验。
    done_rows / on_row 用于检查点恢复：已算完的组合直接复用，每算完一组回调一次。
    """
    import json as _json

    if objective not in OBJECTIVES:
        raise ValueError(f"未知优化目标 {objective}；可选：{', '.join(OBJECTIVES)}")
    if method not in ("grid", "bayes"):
        raise ValueError("method 只能是 grid 或 bayes")
    if method == "bayes":
        space = normalize_space(space or {})
        n_trials = int(n_trials or 50)
        if not 5 <= n_trials <= MAX_COMBINATIONS:
            raise ValueError(f"试验次数应在 5 ~ {MAX_COMBINATIONS} 之间")
        combos: list[dict[str, Any]] = []
    else:
        combos = expand_grid(grid)
        if not combos:
            raise ValueError("参数网格为空：每个参数至少给一个取值")
        if len(combos) > MAX_COMBINATIONS:
            raise ValueError(f"组合数 {len(combos)} 超过上限 {MAX_COMBINATIONS}，请缩小网格")
    if not 0.4 <= split <= 0.9:
        raise ValueError("样本内比例应在 0.4 ~ 0.9 之间")

    if not 0 <= holdout <= 0.4:
        raise ValueError("锁定测试区间比例应在 0 ~ 0.4 之间")
    if embargo_bars < 0:
        raise ValueError("embargo 不能为负")
    dev, locked, lock_ts = lock_holdout(bars, holdout, spec.warmup_bars)
    ins, oos, cut_ts = split_bars(dev, split, spec.warmup_bars, embargo=embargo_bars)
    rows: list[dict[str, Any]] = []
    base = dict(spec.params or {})

    seen: dict[str, dict[str, Any]] = {}

    def evaluate(combo: dict[str, Any]) -> dict[str, Any]:
        key = _json.dumps(combo, sort_keys=True)
        if key in seen:  # 贝叶斯搜索可能重复建议同一组：直接复用，不重复计数
            return seen[key]
        if done_rows and key in done_rows:
            rows.append(done_rows[key])
            seen[key] = done_rows[key]
            return done_rows[key]
        params = {**base, **combo}
        row: dict[str, Any] = {"params": combo}
        try:
            r_in = execute(
                source, name=name, bars=ins, spec=spec, params=params, time_limit=OPT_RUN_TIME_LIMIT, lang=lang
            )
            r_out = execute(
                source, name=name, bars=oos, spec=spec, params=params, time_limit=OPT_RUN_TIME_LIMIT, lang=lang
            )
            row["in_sample"] = summarize_metrics(r_in.metrics)
            row["out_sample"] = summarize_metrics(r_out.metrics)
            row["errors"] = (r_in.errors + r_out.errors)[:3]
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rows.append(row)
        seen[key] = row
        if on_row:
            on_row(row)
        return row

    pareto: list[dict[str, Any]] = []
    if method == "grid":
        for i, combo in enumerate(combos):
            evaluate(combo)
            if progress:
                progress(i + 1, len(combos))
    else:
        pareto = _bayes_search(
            evaluate,
            space or {},
            n_trials=n_trials,
            seed=seed,
            objective=objective,
            multi=multi_objective,
            done=list((done_rows or {}).values()),
            progress=progress,
        )
        combos = [r["params"] for r in rows]

    scored = [r for r in rows if "in_sample" in r and _objective(r["in_sample"], objective) is not None]
    scored.sort(key=lambda r: _objective(r["in_sample"], objective), reverse=True)
    for rank, r in enumerate(scored, start=1):
        r["in_rank"] = rank
    oos_sorted = sorted(
        [r for r in scored if _objective(r["out_sample"], objective) is not None],
        key=lambda r: _objective(r["out_sample"], objective),
        reverse=True,
    )
    for rank, r in enumerate(oos_sorted, start=1):
        r["out_rank"] = rank

    diagnostics: dict[str, Any] = {"combinations": len(combos), "valid": len(scored), "warnings": [], "method": method}
    best = scored[0] if scored else None
    if pareto:
        keys = {_json.dumps(p, sort_keys=True) for p in pareto}
        front = [r for r in scored if _json.dumps(r["params"], sort_keys=True) in keys]
        for r in front:
            r["pareto"] = True
        diagnostics["pareto_size"] = len(front)
        best = front[0] if front else best
    if best:
        paired = [r for r in scored if _objective(r["out_sample"], objective) is not None]
        rho = spearman(
            [_objective(r["in_sample"], objective) for r in paired],
            [_objective(r["out_sample"], objective) for r in paired],
        )
        diagnostics["rank_correlation"] = rho
        diagnostics["best_out_rank"] = best.get("out_rank")
        b_in = _objective(best["in_sample"], objective)
        b_out = _objective(best["out_sample"], objective)
        diagnostics["best_in"] = b_in
        diagnostics["best_out"] = b_out
        outs = sorted(_objective(r["out_sample"], objective) for r in paired)
        median_out = outs[len(outs) // 2] if outs else None
        diagnostics["median_out"] = median_out
        w = diagnostics["warnings"]
        if rho is not None and rho < 0.2:
            w.append(f"样本内与样本外排名几乎无关（秩相关 {rho:.2f}）：参数的'好坏'很可能是噪声，不宜据此选参")
        if b_in is not None and b_out is not None and b_in > 0 and b_out < b_in * 0.5:
            w.append("最优组合在样本外的表现不到样本内的一半，存在明显过拟合迹象")
        if best.get("out_rank") and len(paired) >= 4 and best["out_rank"] > len(paired) / 2:
            w.append(f"样本内第 1 名在样本外只排第 {best['out_rank']} / {len(paired)}")
        if len(combos) >= 50:
            w.append(f"共尝试 {len(combos)} 组参数：尝试越多，'最好那组'越可能是运气（多重检验）")
        if not w:
            w.append("未发现明显过拟合信号；仍建议用更长区间或实时模拟继续验证")
    if best and locked is not None:
        # 锁定测试区间：只评估选定的那一组，一次。结果不参与任何选择。
        r_lock = execute(
            source,
            name=name,
            bars=locked,
            spec=spec,
            params={**base, **best["params"]},
            time_limit=OPT_RUN_TIME_LIMIT,
            lang=lang,
        )
        diagnostics["holdout"] = {
            "from_ts": lock_ts,
            "metrics": summarize_metrics(r_lock.metrics),
            "note": "锁定测试区间在调参中不可见，仅对最终选定参数评估一次",
        }
        h = _objective(diagnostics["holdout"]["metrics"], objective)
        if (
            h is not None
            and diagnostics.get("best_in")
            and diagnostics["best_in"] > 0
            and h < diagnostics["best_in"] * 0.5
        ):
            diagnostics["warnings"].append("锁定测试区间的表现不到样本内的一半：选出的参数很可能过拟合")
    return {
        "method": method,
        "space": space if method == "bayes" else None,
        "n_trials": len(rows),
        "multi_objective": bool(multi_objective and method == "bayes"),
        "embargo_bars": embargo_bars,
        "holdout": holdout,
        "holdout_from": lock_ts,
        "objective": objective,
        "objective_label": OBJECTIVES[objective],
        "split": split,
        "cut_ts": cut_ts,
        "grid": grid,
        "rows": rows,
        "best": best,
        "diagnostics": diagnostics,
    }


def walk_forward(
    source: str,
    *,
    name: str,
    bars: dict[str, list[Bar]],
    spec: BacktestSpec,
    grid: dict[str, list[Any]],
    folds: int = 4,
    objective: str = "sharpe",
    embargo_bars: int = 0,
    progress: Any = None,
    lang: str = "python",
) -> dict[str, Any]:
    """滚动前推（walk-forward）：每一折只用此前的数据选参，在紧随其后的区间检验。

    时间轴均分为 folds+1 段：第 k 折用前 k 段（扩张窗口）调参，第 k+1 段（空出 embargo 根）检验。
    最终报告每折检验区间的表现，以及“各折检验收益拼接”的整体结果——这是对策略选参流程的检验，
    比单次切分更接近真实使用。
    """
    if objective not in OBJECTIVES:
        raise ValueError(f"未知优化目标 {objective}")
    if not 2 <= folds <= 8:
        raise ValueError("折数应在 2 ~ 8 之间")
    combos = expand_grid(grid)
    if not combos:
        raise ValueError("参数网格为空")
    if len(combos) * folds > MAX_COMBINATIONS * 2:
        raise ValueError(f"组合数 × 折数 = {len(combos) * folds} 超过上限 {MAX_COMBINATIONS * 2}")
    timeline = _timeline(bars)
    seg = len(timeline) // (folds + 1)
    if seg < spec.warmup_bars + 20:
        raise ValueError("数据不足以切出足够长的折：请增加数据量、减少折数或缩短预热")
    base = dict(spec.params or {})
    out_folds: list[dict[str, Any]] = []
    chained = 1.0
    done = 0
    for k in range(1, folds + 1):
        train_end = timeline[k * seg - 1]
        test_lo_idx = max(0, k * seg + embargo_bars - spec.warmup_bars)
        test_hi = timeline[min(len(timeline) - 1, (k + 1) * seg - 1)]
        train = _slice(bars, None, train_end)
        test = _slice(bars, timeline[test_lo_idx] - 1, test_hi)
        best, best_v = None, None
        for combo in combos:
            try:
                r = execute(
                    source,
                    name=name,
                    bars=train,
                    spec=spec,
                    params={**base, **combo},
                    time_limit=OPT_RUN_TIME_LIMIT,
                    lang=lang,
                )
                v = _objective(r.metrics, objective)
            except Exception:
                v = None
            done += 1
            if progress:
                progress(done, len(combos) * folds)
            if v is not None and (best_v is None or v > best_v):
                best, best_v = combo, v
        row: dict[str, Any] = {
            "fold": k,
            "train_end": train_end,
            "test_end": test_hi,
            "best_params": best,
            "train_score": best_v,
        }
        if best is not None:
            r_t = execute(
                source,
                name=name,
                bars=test,
                spec=spec,
                params={**base, **best},
                time_limit=OPT_RUN_TIME_LIMIT,
                lang=lang,
            )
            row["test"] = summarize_metrics(r_t.metrics)
            chained *= 1 + float(r_t.metrics.get("total_return") or 0.0)
        out_folds.append(row)
    tested = [f for f in out_folds if "test" in f]
    stable = len({str(sorted((f["best_params"] or {}).items())) for f in tested}) <= 1
    warnings = []
    if tested and sum(1 for f in tested if (f["test"].get("total_return") or 0) > 0) < len(tested) / 2:
        warnings.append("超过一半的检验折亏损：选参流程在样本外不稳定")
    if not stable:
        warnings.append("各折选出的参数不一致：参数对区间敏感，结论需谨慎")
    return {
        "objective": objective,
        "folds": out_folds,
        "embargo_bars": embargo_bars,
        "chained_test_return": chained - 1 if tested else None,
        "params_stable": stable,
        "warnings": warnings or ["各折检验未发现明显异常；仍需实时模拟验证"],
    }
