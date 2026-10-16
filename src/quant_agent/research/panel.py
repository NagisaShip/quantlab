"""面板数据：把多个标的的 K 线与信息源字段对齐成 `[T, N]` 数组，供因子求值与评估。

- 时间轴取所有标的收盘时刻的并集，缺失记为 NaN（上市晚的币前面是空的，不会被补成假数据）。
- 外部字段按 `available_at ≤ 收盘时刻` 做 as-of 对齐（见 datahub）。
- 没有真实数据时按来源设置决定：`auto` 退回合成并标注，`exchange` 直接报错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .datahub import FIELD_SCOPE, DataHub
from .factor_expr import PRICE_FIELDS, _quiet

UNIVERSES: dict[str, dict[str, Any]] = {
    "major10": {
        "name": "主流 10",
        "symbols": [
            "BTC/USDT",
            "ETH/USDT",
            "BNB/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "DOGE/USDT",
            "ADA/USDT",
            "AVAX/USDT",
            "LINK/USDT",
            "DOT/USDT",
        ],
    },
    "major20": {
        "name": "主流 20",
        "symbols": [
            "BTC/USDT",
            "ETH/USDT",
            "BNB/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "DOGE/USDT",
            "ADA/USDT",
            "AVAX/USDT",
            "LINK/USDT",
            "DOT/USDT",
            "TRX/USDT",
            "LTC/USDT",
            "BCH/USDT",
            "ATOM/USDT",
            "NEAR/USDT",
            "UNI/USDT",
            "APT/USDT",
            "ARB/USDT",
            "OP/USDT",
            "FIL/USDT",
        ],
    },
    "btc_eth": {"name": "BTC + ETH", "symbols": ["BTC/USDT", "ETH/USDT"]},
}


class PanelError(RuntimeError):
    """面板构建失败（例如要求只用真实数据但拿不到）。"""


@dataclass
class Panel:
    times: np.ndarray  # [T] 收盘时间（毫秒）
    symbols: list[str]
    data: dict[str, np.ndarray]  # 字段 → [T, N]
    timeframe: str
    sources: dict[str, str] = field(default_factory=dict)  # 标的 → 行情来源
    field_sources: dict[str, str] = field(default_factory=dict)  # 字段 → 来源说明
    warnings: list[str] = field(default_factory=list)

    @property
    def synthetic(self) -> bool:
        return any(v == "synthetic" for v in self.sources.values()) or any(
            "合成" in v for v in self.field_sources.values()
        )

    def slice(self, lo: int, hi: int) -> Panel:
        return Panel(
            self.times[lo:hi],
            self.symbols,
            {k: v[lo:hi] for k, v in self.data.items()},
            self.timeframe,
            self.sources,
            self.field_sources,
            self.warnings,
        )

    def subset(self, cols: list[int]) -> Panel:
        return Panel(
            self.times,
            [self.symbols[i] for i in cols],
            {k: v[:, cols] for k, v in self.data.items()},
            self.timeframe,
            {s: self.sources.get(s, "") for s in (self.symbols[i] for i in cols)},
            self.field_sources,
            self.warnings,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "bars": len(self.times),
            "symbols": self.symbols,
            "timeframe": self.timeframe,
            "start": int(self.times[0]) if len(self.times) else None,
            "end": int(self.times[-1]) if len(self.times) else None,
            "sources": self.sources,
            "field_sources": self.field_sources,
            "synthetic": self.synthetic,
            "warnings": self.warnings,
        }


def build_panel(
    market: Any,
    hub: DataHub | None,
    symbols: list[str],
    timeframe: str,
    *,
    fields: list[str],
    start: int | None = None,
    end: int | None = None,
    bars_limit: int = 1500,
    source: str = "auto",
) -> Panel:
    if not symbols:
        raise PanelError("标的池为空")
    series: dict[str, dict[int, tuple[float, float, float, float, float]]] = {}
    sources: dict[str, str] = {}
    warnings: list[str] = []
    if hub is not None:  # 紧封测试集：研究流程看不到最后一段数据
        from .discipline import clamp_end

        end, note = clamp_end(hub.store, end)
        if note:
            warnings.append(note)
            if start and start >= end:
                raise PanelError("请求的区间完全落在紧封测试集里")
    for sym in symbols:
        try:
            bars, src = market.get_bars(sym, timeframe, limit=bars_limit, start=start, end=end, source=source)
        except Exception as exc:
            warnings.append(f"{sym} 行情获取失败，已跳过：{str(exc)[:120]}")
            continue
        if not bars:
            warnings.append(f"{sym} 没有行情，已跳过")
            continue
        series[sym] = {b.close_time: (b.open, b.high, b.low, b.close, b.volume) for b in bars}
        sources[sym] = src
    if not series:
        raise PanelError("所有标的的行情都获取失败：" + "；".join(warnings[:3]))
    return _assemble(
        series,
        [s for s in symbols if s in series],
        sources,
        warnings,
        hub,
        timeframe,
        fields=fields,
        source=source,
        max_bars=None if start else bars_limit,
    )


def panel_from_bars(
    bars_by_symbol: dict[str, list[Any]],
    hub: DataHub | None,
    timeframe: str,
    *,
    fields: list[str],
    source: str = "auto",
    sources: dict[str, str] | None = None,
) -> Panel:
    """用已经取好的 K 线（例如回测正在用的那一份）组装面板，外部字段同样按时点对齐。"""
    series = {
        sym: {b.close_time: (b.open, b.high, b.low, b.close, b.volume) for b in bars}
        for sym, bars in bars_by_symbol.items()
        if bars
    }
    if not series:
        raise PanelError("行情为空")
    return _assemble(
        series,
        [s for s in bars_by_symbol if s in series],
        dict(sources or {}),
        [],
        hub,
        timeframe,
        fields=fields,
        source=source,
        max_bars=None,
    )


def _assemble(
    series: dict[str, dict[int, tuple[float, float, float, float, float]]],
    syms: list[str],
    sources: dict[str, str],
    warnings: list[str],
    hub: DataHub | None,
    timeframe: str,
    *,
    fields: list[str],
    source: str,
    max_bars: int | None,
) -> Panel:
    times = np.array(sorted({t for s in syms for t in series[s]}), dtype=np.int64)
    if max_bars and len(times) > max_bars * 1.5:
        times = times[-int(max_bars) :]
    pos = {int(t): i for i, t in enumerate(times)}
    T, N = len(times), len(syms)
    arr = {k: np.full((T, N), np.nan) for k in ("open", "high", "low", "close", "volume")}
    for j, s in enumerate(syms):
        for t, (o, h, lo, c, v) in series[s].items():
            i = pos.get(int(t))
            if i is None:
                continue
            arr["open"][i, j], arr["high"][i, j], arr["low"][i, j] = o, h, lo
            arr["close"][i, j], arr["volume"][i, j] = c, v
    data: dict[str, np.ndarray] = dict(arr)
    with np.errstate(all="ignore"):
        data["amount"] = data["close"] * data["volume"]
        data["vwap"] = (data["high"] + data["low"] + data["close"]) / 3
        prev = np.vstack([np.full((1, N), np.nan), data["close"][:-1]])
        data["ret"] = data["close"] / prev - 1
        with _quiet():
            mkt = np.nanmean(data["ret"], axis=1, keepdims=True) if N else np.zeros((T, 1))
        data["mkt_ret"] = np.repeat(mkt, N, axis=1)
    if any(v == "synthetic" for v in sources.values()):
        warnings.append("部分标的使用了合成行情，结论不能作为研究结论")

    field_sources: dict[str, str] = {}
    # 合成序列只在测试 / 离线演示时允许（QAGENT_ALLOW_SYNTHETIC=1）；平台默认如实报「未接入数据」
    synth_ok = source != "exchange" and os.environ.get("QAGENT_ALLOW_SYNTHETIC", "0") == "1"
    for f in fields:
        if f in PRICE_FIELDS or f in data:
            continue
        col = np.full((T, N), np.nan)
        used: set[str] = set()
        synth_cols = []
        for j, s in enumerate(syms):
            rec = hub.find(f, s, allow_synthetic=synth_ok) if hub else None
            if rec is not None:
                col[:, j] = hub.asof(rec["key"], times)  # type: ignore[union-attr]
                used.add(rec["source"])
            elif synth_ok and hub is not None and not f.startswith("x_"):
                if FIELD_SCOPE.get(f) == "market":
                    col[:, :] = hub.synthetic(f, times, data["close"][:, 0], "market")[:, None]
                    synth_cols.append("全市场")
                    break
                col[:, j] = hub.synthetic(f, times, data["close"][:, j], s)
                synth_cols.append(s)
            if FIELD_SCOPE.get(f) == "market" and rec is not None:
                col[:, :] = col[:, [j]]
                break
        if np.all(np.isnan(col)):
            if source == "exchange" or not synth_ok:
                raise PanelError(f"未接入数据：没有字段 {f} 的真实数据。请先在「行情与数据 → 研究数据」同步")
            if f.startswith("x_"):
                raise PanelError(f"没有找到自定义数据 {f}，请先在「行情与数据」页导入")
        if synth_cols:
            used.add("合成")
            warnings.append(
                f"字段 {f} 缺少真实数据，{'、'.join(synth_cols[:4])}{'等' if len(synth_cols) > 4 else ''}"
                "用了合成序列；到「行情与数据」同步后结论才有意义"
            )
        data[f] = col
        field_sources[f] = "、".join(sorted(used)) or "无"
    return Panel(times, syms, data, timeframe, sources, field_sources, warnings)


__all__ = ["UNIVERSES", "Panel", "PanelError", "build_panel", "panel_from_bars"]
