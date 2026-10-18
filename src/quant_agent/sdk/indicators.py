"""平台预置指标算子（NumPy 实现，供策略直接调用）。

策略里最容易被写慢的是「每个 bar 都从 history 重新切片、再用 Python for 循环递推」：
1m 线一年 52 万根时，复杂度会退化到 O(n²)。这里的函数一次算完整条序列，
再配合 ``ctx.indicators.ema(security, period)`` 的运行时缓存，每根 bar 只读一个值。

约定：
- 输入是等长的 list / tuple / ndarray；
- 返回与输入等长的列表；
- 预热期不足的值返回 ``float("nan")``（比较结果为 False，不会误触发信号）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def _arr(values: Sequence[float]) -> np.ndarray:
    return np.asarray(list(values), dtype=float)


def sma(values: Sequence[float], period: int) -> list[float]:
    """简单移动平均。"""
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须大于 0")
    a = _arr(values)
    out = np.full(a.shape, np.nan, dtype=float)
    if len(a) >= period:
        c = np.cumsum(np.insert(a, 0, 0.0))
        out[period - 1 :] = (c[period:] - c[:-period]) / period
    return out.tolist()


def ema(values: Sequence[float], period: int) -> list[float]:
    """指数移动平均（adjust=False，首值作为种子，与常见交易平台口径一致）。"""
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须大于 0")
    a = _arr(values)
    out = np.empty_like(a)
    if not len(a):
        return out.tolist()
    alpha = 2.0 / (period + 1.0)
    out[0] = a[0]
    # 一次 O(n) 递推；调用方用 ctx.indicators.ema 时整个回测只执行一次。
    for i in range(1, len(a)):
        out[i] = alpha * a[i] + (1.0 - alpha) * out[i - 1]
    return out.tolist()


def rma(values: Sequence[float], period: int) -> list[float]:
    """Wilder 平滑（RMA），ATR / RSI 使用。前 period-1 个值为 NaN，首个有效值是前 period 个的均值。"""
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须大于 0")
    a = _arr(values)
    out = np.full(a.shape, np.nan, dtype=float)
    if len(a) < period:
        return out.tolist()
    out[period - 1] = float(np.mean(a[:period]))
    alpha = 1.0 / period
    for i in range(period, len(a)):
        out[i] = alpha * a[i] + (1.0 - alpha) * out[i - 1]
    return out.tolist()


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    """真实波幅：max(H-L, |H-PC|, |L-PC|)，首根为 H-L。"""
    h, low, c = _arr(highs), _arr(lows), _arr(closes)
    if not (len(h) == len(low) == len(c)):
        raise ValueError("highs / lows / closes 长度必须一致")
    prev = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum(h - low, np.maximum(np.abs(h - prev), np.abs(low - prev)))
    if len(tr):
        tr[0] = h[0] - low[0]
    return tr.tolist()


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[float]:
    """平均真实波幅（Wilder）。"""
    return rma(true_range(highs, lows, closes), period)


def rsi(values: Sequence[float], period: int = 14) -> list[float]:
    """相对强弱指标（Wilder），预热期返回 NaN。"""
    period = int(period)
    a = _arr(values)
    out = np.full(a.shape, np.nan, dtype=float)
    if len(a) <= period:
        return out.tolist()
    diff = np.diff(a)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = rma(gains, period)
    avg_loss = rma(losses, period)
    for i in range(period, len(a)):
        g, loss = avg_gain[i - 1], avg_loss[i - 1]
        if math.isnan(g) or math.isnan(loss):
            continue
        out[i] = 100.0 if loss == 0 else (100.0 - 100.0 / (1.0 + g / loss))
    return out.tolist()


def zscore(values: Sequence[float], period: int) -> list[float]:
    """滚动 Z 分数： (x - 均值) / 标准差。"""
    period = int(period)
    if period <= 0:
        raise ValueError("period 必须大于 0")
    a = _arr(values)
    out = np.full(a.shape, np.nan, dtype=float)
    for i in range(period - 1, len(a)):
        win = a[i - period + 1 : i + 1]
        sd = float(np.std(win, ddof=0))
        if sd > 0:
            out[i] = (a[i] - float(np.mean(win))) / sd
    return out.tolist()


__all__ = ["atr", "ema", "rma", "rsi", "sma", "true_range", "zscore"]
