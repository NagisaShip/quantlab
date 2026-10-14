"""市场状态：趋势 / 震荡 / 高波动，按日收盘价判断，只用当天及以前的数据。

- 高波动：近 20 天日收益的年化波动率高于过去一年里这个数的 80% 分位（不足一年用已有部分），且至少 60%；
- 趋势：近 20 天的效率比（净涨跌幅 ÷ 每天涨跌幅绝对值之和）≥ 0.3；
- 其余为震荡。

调度器用它给每个策略按「在这种状态下历史上表现如何」加减权重（见 ``weights``）。
"""

from __future__ import annotations

import math
from typing import Any

REGIMES = {"trend": "趋势", "range": "震荡", "high_vol": "高波动"}
WIN = 20


def classify(closes: list[float]) -> list[str | None]:
    """每天一个标签；前 WIN 天数据不够，给 None。"""
    out: list[str | None] = []
    rets = [b / a - 1 if a else 0.0 for a, b in zip(closes, closes[1:], strict=False)]
    vols: list[float] = []
    for i in range(len(closes)):
        if i < WIN:
            out.append(None)
            continue
        win = rets[i - WIN : i]
        mu = sum(win) / WIN
        vol = math.sqrt(sum((r - mu) ** 2 for r in win) / (WIN - 1)) * math.sqrt(365)
        vols.append(vol)
        hist = sorted(vols[-365:])
        p80 = hist[int(0.8 * (len(hist) - 1))]
        moves = sum(abs(r) for r in win) or 1e-12
        er = abs(closes[i] / closes[i - WIN] - 1) / moves if closes[i - WIN] else 0.0
        if len(hist) >= 20 and vol >= max(p80, 0.6):
            out.append("high_vol")
        elif er >= 0.3:
            out.append("trend")
        else:
            out.append("range")
    return out


def multipliers(daily_returns: list[float], labels: list[str | None]) -> dict[str, float]:
    """策略在各状态下的历史表现 → 权重倍数：t 值每 +1 加 25%，限制在 0.25～2 倍；样本少于 10 天的状态不调。"""
    pairs = [(r, lab) for r, lab in zip(daily_returns, labels, strict=False) if lab]
    if len(pairs) < 20:
        return {}
    allr = [r for r, _ in pairs]
    mu_all = sum(allr) / len(allr)
    sd = math.sqrt(sum((r - mu_all) ** 2 for r in allr) / (len(allr) - 1)) or 1e-12
    out = {}
    for key in REGIMES:
        rs = [r for r, lab in pairs if lab == key]
        if len(rs) < 10:
            continue
        t = (sum(rs) / len(rs)) / sd * math.sqrt(len(rs))
        out[key] = round(max(0.25, min(2.0, 1 + 0.25 * t)), 3)
    return out


def from_backtest(bt: dict[str, Any]) -> dict[str, float]:
    """用回测的日记录：基准收盘价分状态，策略日收益算倍数。"""
    daily = bt.get("daily") or []
    bench = [float(d.get("benchmark") or 0) for d in daily]
    rets = [float(d.get("return") or 0) for d in daily]
    if len(daily) < WIN + 20 or not all(bench):
        return {}
    labels = classify(bench)
    # 当天的状态用前一天收盘判断，避免拿当天收益去解释当天
    return multipliers(rets[1:], labels[:-1])


def weights(
    members: dict[str, dict[str, Any]], regime: str | None, *, tilt: float = 0.5, floor: float = 0.0
) -> dict[str, float]:
    from ..live.netting import allocate

    return allocate(members, regime=regime, tilt=tilt, floor=floor)
