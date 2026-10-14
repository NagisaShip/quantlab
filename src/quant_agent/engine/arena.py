"""统一赛道与团队比较（对应 plan.md 第 4 章 / 实验三第 4、6 节）。

这是"公平比较"落地的地方，规则照抄方案：

- **统一赛道**：所有策略共用同一份行情、同一初始资金、同一成本规则、同一区间。
  赛道参数在批次开始时冻结，写进批次记录。
- **自由实验**：不同币种 / 周期 / 资金 / 成本的结果单独展示，不进入正式比较。
- **批次隔离**：改参数或改代码生成**新批次**，原批次成绩不被覆盖、不被重算。
- **并列呈现**：排行榜同时给出净收益、最大回撤、超额收益与运行完整性，
  刻意不合并成"综合分"——方案明确要求不急于合并为单一分数。
- **基准恒存在**：平台自建买入持有基准与现金基准，即使团队列表里没有持有策略。
- **样本不足就说不适用**：无成交策略的胜率、盈亏比、利润因子显示为 None，
  由界面显示"不适用"，而不是编一个 0。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..data.quality import DataSet
from ..strategies.sdk import StrategyBase
from .backtest import BacktestEngine, BacktestResult, CostModel, stress_scenarios
from .windows import WARMUP_BARS, window_status_text

DEFAULT_TRACK: dict[str, Any] = {
    "market": "spot",
    "exchange": "binance",
    "symbols": ["BTC/USDT"],
    "initial_cash": 10_000.0,
    "leverage": 1.0,
    "direction": "long_only",
    "timeframe": "1h",
    "warmup_bars": WARMUP_BARS,
    "cost": {
        "taker_fee": 0.001,
        "maker_fee": 0.0005,
        "slippage_bps": 5.0,
        "spread_bps": 2.0,
        "max_volume_participation": 0.1,
    },
    "timezone": "UTC",
    "note": "以上均为实验参数，不代表交易所实际费率，也不是投资建议",
}


@dataclass
class Entrant:
    """一个参赛策略：由调用方构造，可能是模板、规则或 Agent 生成的代码。"""

    strategy_id: str
    name: str
    team: str
    instance: StrategyBase
    origin: str = "user"  # user | agent | builtin
    version: str = ""
    kind: str = "template"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Standing:
    """排行榜里的一行。指标并列展示，不做加权合并。"""

    strategy_id: str
    name: str
    team: str
    origin: str
    version: str
    kind: str
    params: dict[str, Any]
    metrics: dict[str, Any]
    windows: dict[str, Any]
    completeness: dict[str, Any]
    equity_curve: list[float]
    timestamps: list[int]
    drawdown: list[float]
    orders: list[dict[str, Any]]
    is_benchmark: bool = False

    @property
    def return_7d(self) -> float | None:
        return (self.windows.get("windows", {}).get("7", {}).get("metrics") or {}).get("total_return")

    @property
    def return_30d(self) -> float | None:
        return (self.windows.get("windows", {}).get("30", {}).get("metrics") or {}).get("total_return")

    def to_dict(self, *, max_points: int = 400) -> dict[str, Any]:
        eq, ts, dd = self.equity_curve, self.timestamps, self.drawdown
        if len(eq) > max_points:
            step = max(1, len(eq) // max_points)
            eq, ts, dd = eq[::step], ts[::step], dd[::step]
            # 抽稀后必须补回最后一个真实点，否则曲线末端会骗人
            if eq[-1] != self.equity_curve[-1]:
                eq = [*eq, self.equity_curve[-1]]
                ts = [*ts, self.timestamps[-1]]
                dd = [*dd, self.drawdown[-1]]
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "team": self.team,
            "origin": self.origin,
            "version": self.version,
            "kind": self.kind,
            "params": self.params,
            "metrics": self.metrics,
            "windows": self.windows,
            "completeness": self.completeness,
            "equity_curve": eq,
            "timestamps": ts,
            "drawdown": dd,
            "orders": self.orders[:50],
            "is_benchmark": self.is_benchmark,
            "return_7d": self.return_7d,
            "return_30d": self.return_30d,
        }


@dataclass
class Batch:
    """一个冻结的实验批次。"""

    id: str
    track: dict[str, Any]
    dataset: dict[str, Any]
    started_at: int
    finished_at: int = 0
    standings: list[Standing] = field(default_factory=list)
    scenarios: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    track_digest: str = ""

    def to_dict(self, *, with_series: bool = True) -> dict[str, Any]:
        rows = [
            s.to_dict()
            if with_series
            else {k: v for k, v in s.to_dict().items() if k not in ("equity_curve", "timestamps", "drawdown", "orders")}
            for s in self.standings
        ]
        return {
            "id": self.id,
            "track": self.track,
            "track_digest": self.track_digest,
            "dataset": self.dataset,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "count": len(self.standings),
            "standings": rows,
            "scenarios": self.scenarios,
            "notes": self.notes,
            "window_labels": {k: window_status_text(_as_window(v)) for k, v in _first_windows(self.standings).items()},
        }


def _as_window(payload: dict) -> Any:
    """把一个窗口 dict 包成带 days/hours/complete 的小对象，供 window_status_text 使用。"""
    return type("W", (), payload)()


def _first_windows(standings: list[Standing]) -> dict[str, dict]:
    for s in standings:
        w = s.windows.get("windows")
        if w:
            return w
    return {}


def track_digest(track: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(track, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


class Arena:
    """跑统一赛道、出排行榜。"""

    def __init__(self, *, store: Any | None = None) -> None:
        self.store = store

    # ---------------- 赛道 ----------------
    @staticmethod
    def build_track(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """合成一份赛道配置；非法字段一律拒绝，避免"悄悄用了另一个参数"。"""
        track = json.loads(json.dumps(DEFAULT_TRACK))
        for key, value in (overrides or {}).items():
            if key == "cost" and isinstance(value, dict):
                track["cost"].update(value)
            elif key in track:
                track[key] = value
            else:
                raise ValueError(f"赛道不支持的配置项：{key}")
        if track["market"] != "spot":
            raise ValueError("首版统一赛道仅支持现货（market=spot）")
        if float(track["leverage"]) != 1.0:
            raise ValueError("首版统一赛道不支持杠杆（leverage 必须为 1.0）")
        if track["direction"] != "long_only":
            raise ValueError("首版统一赛道仅做多（direction=long_only）")
        if not track["symbols"]:
            raise ValueError("赛道至少需要一个交易对")
        cash = float(track["initial_cash"])
        if not 100 <= cash <= 1_000_000:
            raise ValueError("初始资金需在 100 至 1,000,000 之间")
        return track

    @staticmethod
    def cost_model(track: dict[str, Any]) -> CostModel:
        return CostModel(**track["cost"])

    # ---------------- 运行 ----------------
    def run(
        self,
        entrants: list[Entrant],
        data: DataSet,
        *,
        track_overrides: dict[str, Any] | None = None,
        with_scenarios: bool = False,
        extra_benchmarks: list[Entrant] | None = None,
    ) -> Batch:
        """跑一个批次。所有参赛者与基准共用同一份数据与成本。"""
        if not entrants and not extra_benchmarks:
            raise ValueError("批次至少需要一个参赛策略")

        track = self.build_track(track_overrides)
        symbol = track["symbols"][0]
        timeframe = track["timeframe"]
        warmup = int(track["warmup_bars"])
        if len(data.bars) < warmup + 2:
            raise ValueError(f"行情 {len(data.bars)} 根不足以覆盖 {warmup} 根预热 + 至少 2 根计分")

        batch = Batch(
            id=uuid.uuid4().hex[:12],
            track=track,
            dataset=data.summary(),
            started_at=int(time.time() * 1000),
            track_digest=track_digest(track),
        )
        batch.notes.append(
            f"统一赛道：{track['market']} · {symbol} · {timeframe} · "
            f"初始资金 {track['initial_cash']:,.0f} · 无杠杆 · 仅做多 · {track['timezone']} 计时"
        )
        batch.notes.append(f"数据来源：{data.source_label or data.source}（摘要 {data.digest}）")
        batch.notes.append(
            f"成本假设：手续费 {track['cost']['taker_fee'] * 100:.3f}% · "
            f"滑点 {track['cost']['slippage_bps']:g} bps · 半价差 {track['cost']['spread_bps']:g} bps"
        )
        batch.notes.append(f"前 {warmup} 根 bar 仅用于指标预热，不计成绩")
        if data.coverage_warning:
            batch.notes.append(f"⚠ {data.coverage_warning}")

        engine = BacktestEngine(initial_cash=track["initial_cash"], cost_model=self.cost_model(track))

        all_entrants = list(entrants) + list(extra_benchmarks or [])
        for entrant in all_entrants:
            result = engine.run(
                entrant.instance,
                data.bars,
                run_id=f"{batch.id}:{entrant.strategy_id}",
                symbol=symbol,
                timeframe=timeframe,
                warmup_bars=warmup,
            )
            batch.standings.append(self._to_standing(entrant, result, track))

        # 平台自建基准：即使团队没提交持有策略，基准也必须在
        for bench in self._platform_benchmarks():
            if any(s.strategy_id == bench.strategy_id for s in batch.standings):
                continue
            result = engine.run(
                bench.instance,
                data.bars,
                run_id=f"{batch.id}:{bench.strategy_id}",
                symbol=symbol,
                timeframe=timeframe,
                warmup_bars=warmup,
            )
            batch.standings.append(self._to_standing(bench, result, track))

        if with_scenarios and entrants:
            batch.scenarios = self._scenarios(entrants, data, track, symbol, timeframe, warmup)

        batch.finished_at = int(time.time() * 1000)
        if self.store is not None:
            self.store.upsert("batches", {**batch.to_dict(with_series=True), "track_digest": batch.track_digest})
        return batch

    def _to_standing(self, entrant: Entrant, result: BacktestResult, track: dict[str, Any]) -> Standing:
        _ = track
        return Standing(
            strategy_id=entrant.strategy_id,
            name=entrant.name,
            team=entrant.team,
            origin=entrant.origin,
            version=entrant.version,
            kind=entrant.kind,
            params=entrant.params or dict(result.params),
            metrics=result.metrics.to_dict(),
            windows=result.windows,
            completeness=self._completeness(result),
            equity_curve=result.equity_curve,
            timestamps=result.timestamps,
            drawdown=result.drawdown,
            orders=[t.__dict__ for t in result.trades],
            is_benchmark=entrant.origin == "builtin",
        )

    @staticmethod
    def _completeness(result: BacktestResult) -> dict[str, Any]:
        w = result.windows.get("windows", {}) or {}
        days7 = w.get("7") or {}
        days30 = w.get("30") or {}
        return {
            "scored_bars": result.metrics.observations,
            "warmup_bars": result.warmup_bars,
            "hours": max(days30.get("hours") or 0, days7.get("hours") or 0),
            "window_7_complete": bool(days7.get("complete")),
            "window_30_complete": bool(days30.get("complete")),
            "has_trades": result.metrics.trades > 0,
            "no_data_gaps": True,  # 数据连续性在导入阶段已强制校验
        }

    @staticmethod
    def _platform_benchmarks() -> list[Entrant]:
        from ..strategies.templates import build_strategy

        return [
            Entrant(
                "bench:buy_hold",
                "买入持有基准",
                "平台基准",
                build_strategy("buy_hold"),
                origin="builtin",
                kind="benchmark",
            ),
            Entrant("bench:cash", "现金基准", "平台基准", build_strategy("cash"), origin="builtin", kind="benchmark"),
        ]

    def _scenarios(
        self,
        entrants: list[Entrant],
        data: DataSet,
        track: dict[str, Any],
        symbol: str,
        timeframe: str,
        warmup: int,
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for label, cost in stress_scenarios(self.cost_model(track)).items():
            engine = BacktestEngine(initial_cash=track["initial_cash"], cost_model=cost)
            rows: dict[str, Any] = {}
            for entrant in entrants:
                r = engine.run(
                    entrant.instance,
                    data.bars,
                    run_id=f"s:{label}:{entrant.strategy_id}",
                    symbol=symbol,
                    timeframe=timeframe,
                    warmup_bars=warmup,
                )
                rows[entrant.name] = {
                    "total_return": r.metrics.total_return,
                    "max_drawdown": r.metrics.max_drawdown,
                    "sharpe": r.metrics.sharpe,
                    "trades": r.metrics.trades,
                }
            out[label] = rows
        return out

    # ---------------- 排行榜 ----------------
    @staticmethod
    def leaderboard(
        batch: Batch,
        *,
        window: str = "30",
        include_benchmarks: bool = True,
        only_complete: bool = False,
    ) -> dict[str, Any]:
        """按指定窗口出排行榜。

        排序只用净收益；但每行同时呈现回撤、超额收益与完整性，
        让人能一眼看出"高收益是不是靠重仓和没跑满换来的"。
        """
        rows: list[dict[str, Any]] = []
        bench_return = None
        for s in batch.standings:
            if s.is_benchmark and s.strategy_id == "bench:buy_hold":
                bench_return = (s.windows.get("windows", {}).get(window, {}).get("metrics") or {}).get("total_return")
        for s in batch.standings:
            if s.is_benchmark and not include_benchmarks:
                continue
            w = (s.windows.get("windows", {}) or {}).get(window)
            if w is None:
                continue
            if only_complete and not w.get("complete"):
                continue
            m = w.get("metrics", {})
            ret = m.get("total_return")
            rows.append(
                {
                    "strategy_id": s.strategy_id,
                    "name": s.name,
                    "team": s.team,
                    "origin": s.origin,
                    "version": s.version,
                    "kind": s.kind,
                    "params": s.params,
                    "is_benchmark": s.is_benchmark,
                    "window": w.get("label"),
                    "hours": w.get("hours"),
                    "complete": w.get("complete"),
                    "return": ret,
                    "max_drawdown": m.get("max_drawdown"),
                    "excess_return": (ret - bench_return) if (ret is not None and bench_return is not None) else None,
                    "trades": m.get("trades"),
                    "win_rate": m.get("win_rate"),
                    "sharpe": m.get("sharpe"),
                    "fees": s.metrics.get("turnover"),
                    "completeness": s.completeness,
                    "notes": m.get("notes", []),
                }
            )
        rows.sort(key=lambda r: (r["return"] is None, -(r["return"] or 0)))
        return {
            "batch_id": batch.id,
            "window": window,
            "benchmark_return": bench_return,
            "rows": rows,
            "conventions": [
                "排行榜并列呈现净收益、最大回撤、超额收益与运行完整性，不合并为单一分数",
                "同一窗口内收益以区间起点净值为分母，回撤在该区间内重新计算",
                "超额收益相对同币种、同资金、同成本、同区间的买入持有基准",
                f"{window} 天检查点为起点后的前 {int(window) * 24} 小时，未满窗口标注为进行中",
                "样本不足或无成交时，胜率 / 夏普等指标显示为不适用",
            ],
        }
