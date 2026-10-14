"""固定观察窗口（对应 plan.md 第 5 章 / 实验三第 5 节）。

口径要点，全部照抄方案而不是自己发明：

- 前 100 根 bar 只用于指标预热，不计成绩。回测从预热结束后的第一根开始计分。
- 7 天检查点 = 起点之后的**前 168 小时**；30 天 = **前 720 小时**。
  注意是"固定起点后的前 N 小时"，不是"滚动近 N 天"。两个概念必须分开。
- 窗口未满时 `complete=False`，并给出实际观测小时数——界面显示"进行中"，
  绝不冒充完整的 7 天 / 30 天结果。
- 滚动视图（近 7 天 / 近 30 天）单独计算，与固定窗口分开呈现。

这些规则的意义在于：只有同一批次、同一起点、同一窗口的结果才可比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .metrics import Metrics, compute_metrics

HOUR_MS = 3_600_000
WARMUP_BARS = 100
WINDOW_DAYS: tuple[int, ...] = (7, 30)


@dataclass
class WindowResult:
    days: int
    label: str
    complete: bool
    hours: float  # 实际观测小时数
    cutoff_ts: int
    metrics: Metrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "label": self.label,
            "complete": self.complete,
            "hours": self.hours,
            "cutoff_ts": self.cutoff_ts,
            "metrics": self.metrics.to_dict(),
        }


@dataclass
class WindowSet:
    """一个 run 的全部固定窗口，加上滚动视图。"""

    start_ts: int
    end_ts: int
    warmup_bars: int
    scored_bars: int
    windows: dict[str, WindowResult] = field(default_factory=dict)
    rolling: dict[str, WindowResult] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "warmup_bars": self.warmup_bars,
            "scored_bars": self.scored_bars,
            "windows": {k: v.to_dict() for k, v in self.windows.items()},
            "rolling": {k: v.to_dict() for k, v in self.rolling.items()},
        }


def _slice(
    equity: list[float],
    timestamps: list[int],
    *,
    since: int | None = None,
    until: int | None = None,
) -> tuple[list[float], list[int]]:
    eq: list[float] = []
    ts: list[int] = []
    for e, t in zip(equity, timestamps, strict=False):
        if since is not None and t <= since:
            continue
        if until is not None and t > until:
            continue
        eq.append(e)
        ts.append(t)
    return eq, ts


def compute_windows(
    equity: list[float],
    timestamps: list[int],
    *,
    initial: float,
    timeframe: str = "1h",
    warmup_bars: int = WARMUP_BARS,
    trades: list[dict[str, Any]] | None = None,
) -> WindowSet:
    """从完整净值曲线切出固定窗口与滚动视图。

    `equity[0]` 视为起点净值（预热刚结束），因此 `timestamps[0]` 是实验起点。
    `trades` 用来统计各窗口内的成交笔数——只算落在这个窗口里的那些，
    否则窗口里会显示全周期的成交数，读起来是错的。
    """
    if not equity or not timestamps:
        # 还没有任何观测点时，也要把窗口结构给出来：
        # 界面显示"窗口未满 · 已观测 0 小时"，比整块空白更容易理解。
        empty = Metrics()
        result = WindowSet(start_ts=0, end_ts=0, warmup_bars=warmup_bars, scored_bars=0)
        for days in WINDOW_DAYS:
            result.windows[str(days)] = WindowResult(
                days=days,
                label=f"{days} 天",
                complete=False,
                hours=0.0,
                cutoff_ts=0,
                metrics=empty,
            )
            result.rolling[str(days)] = WindowResult(
                days=days,
                label=f"近 {days} 天",
                complete=False,
                hours=0.0,
                cutoff_ts=0,
                metrics=empty,
            )
        return result

    start = timestamps[0]
    end = timestamps[-1]
    result = WindowSet(
        start_ts=start,
        end_ts=end,
        warmup_bars=warmup_bars,
        scored_bars=len(equity),
    )

    def window_metrics(eq: list[float], ts: list[int]) -> Metrics:
        m = compute_metrics(eq, timeframe=timeframe, trade_returns=None)
        if trades and ts:
            lo, hi = ts[0], ts[-1]
            m.trades = sum(1 for t in trades if lo <= t.get("exit_ts", 0) <= hi)
        return m

    for days in WINDOW_DAYS:
        cutoff = start + days * 24 * HOUR_MS
        eq, ts = _slice(equity, timestamps, until=cutoff)
        hours = ((ts[-1] - start) / HOUR_MS) if ts else 0.0
        result.windows[str(days)] = WindowResult(
            days=days,
            label=f"{days} 天",
            complete=bool(ts) and ts[-1] >= cutoff,
            hours=round(hours, 2),
            cutoff_ts=cutoff,
            metrics=window_metrics(eq, ts),
        )

    # 滚动视图：以整段数据的末端为锚，回看 N 天。与固定窗口严格分开。
    for days in WINDOW_DAYS:
        since = end - days * 24 * HOUR_MS
        eq, ts = _slice(equity, timestamps, since=since)
        hours = ((ts[-1] - ts[0]) / HOUR_MS) if len(ts) > 1 else 0.0
        result.rolling[str(days)] = WindowResult(
            days=days,
            label=f"近 {days} 天",
            complete=hours >= days * 24 - 1,
            hours=round(hours, 2),
            cutoff_ts=end,
            metrics=window_metrics(eq, ts),
        )

    _ = initial
    return result


def window_status_text(window: WindowResult) -> str:
    """界面用的一句话状态，明确区分"完整"与"进行中"。"""
    if window.complete:
        return f"{window.days} 天窗口完整"
    return f"{window.days} 天窗口未满 · 已观测 {window.hours:.0f} 小时（{window.hours / 24:.1f} 天）"
