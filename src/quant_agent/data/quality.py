"""行情数据校验与 CSV 导入（对应 plan.md 第 4 章 / 实验三第 4 节）。

这一层是"可信实验"的地基：拿进来的数据本身必须是干净的、来源可查的。
所以这里的检查全部是**硬失败**——宁可报错让人重导，也不悄悄填补或跳过。

严格检查项：
- 时间：升序、无重复、**无缺口**（首版只接受连续 1 小时行情）
- 数值：全部为有限值；OHLC 之间关系成立（high ≥ max(open, close) 等）
- 规模：行数上下限、文件大小上限
- 时间格式：秒/毫秒时间戳，或**带时区**的 ISO 字符串（不接受裸时间）

导入后的数据带一份摘要（行数、起止、来源标签），随 run 一起冻结，
这样"这份结果是用哪份数据跑出来的"永远可查。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime

from ..exchanges.base import Bar

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

MIN_BARS = 102  # 100 根预热 + 至少 2 根计分
MAX_BARS = 5000
MAX_CSV_BYTES = 2_000_000
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close")


class DataError(ValueError):
    """数据不合法。消息面向使用者，直接展示在界面上。"""


@dataclass
class DataSet:
    """一份可用于回测的行情，带来源与摘要。"""

    symbol: str
    timeframe: str
    bars: list[Bar]
    source: str  # synthetic | exchange | csv
    source_label: str = ""
    digest: str = ""
    coverage_hours: float = 0.0
    requested_bars: int = 0
    coverage_warning: str = ""
    exchange: str = ""

    def freeze(self, source_label: str) -> DataSet:
        self.source_label = source_label
        payload = [[b.open_time, b.open, b.high, b.low, b.close, b.volume] for b in self.bars]
        self.digest = hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
        if not self.coverage_hours and self.bars:
            step_hours = TIMEFRAME_MS.get(self.timeframe, 3_600_000) / 3_600_000
            self.coverage_hours = round(len(self.bars) * step_hours, 2)
        return self

    def summary(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": len(self.bars),
            "source": self.source,
            "source_label": self.source_label,
            "digest": self.digest,
            "start_ts": self.bars[0].open_time if self.bars else 0,
            "end_ts": self.bars[-1].close_time if self.bars else 0,
            "coverage_hours": self.coverage_hours,
            "requested_bars": self.requested_bars,
            "coverage_warning": self.coverage_warning,
            "exchange": self.exchange,
        }

    def to_csv(self) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["timestamp", *REQUIRED_COLUMNS[1:], "volume"])
        for b in self.bars:
            writer.writerow([b.open_time, b.open, b.high, b.low, b.close, b.volume])
        return out.getvalue()


def _finite(value: object, low: float, high: float, label: str) -> float:
    if isinstance(value, bool):
        raise DataError(f"{label} 需要数值")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise DataError(f"{label} 需要数值，收到 {value!r}") from exc
    if not math.isfinite(number):
        raise DataError(f"{label} 必须是有限数值（NaN 与无穷都不允许）")
    if not low <= number <= high:
        raise DataError(f"{label} 超出合理范围 [{low:g}, {high:g}]，收到 {number:g}")
    return number


def parse_timestamp(value: object) -> int:
    """接受秒/毫秒时间戳，或带时区的 ISO 字符串。

    刻意拒绝不带时区的 ISO 时间："2026-09-01T00:00:00" 这种写法
    在不同环境下会被解释成不同时刻，数据不一致的根源就在这里。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ms = int(value)
        if ms < 100_000_000_000:  # 秒级时间戳
            ms *= 1000
        return ms
    if not isinstance(value, str) or not value.strip():
        raise DataError("时间戳不能为空")
    text = value.strip()
    if text.isdigit():
        return parse_timestamp(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataError(f"无法解析时间 {text!r}；请用毫秒时间戳或带时区的 ISO 格式") from exc
    if dt.tzinfo is None:
        raise DataError(f"ISO 时间必须带时区，例如 {text}+00:00（裸时间在不同环境含义不同）")
    return int(dt.timestamp() * 1000)


def validate_bars(
    raw_bars: list[dict],
    *,
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    require_continuity: bool = True,
) -> list[Bar]:
    """校验并规范化一串原始 bar。

    默认要求严格连续（首版口径）：升序、无重复、无缺口。
    """
    step = TIMEFRAME_MS.get(timeframe)
    if step is None:
        raise DataError(f"不支持的周期 {timeframe}；可用：{'、'.join(TIMEFRAME_MS)}")

    if not isinstance(raw_bars, list) or not raw_bars:
        raise DataError("行情为空")
    if len(raw_bars) < MIN_BARS:
        raise DataError(f"行情至少需要 {MIN_BARS} 行（前 100 行用于指标预热，其余计分），当前 {len(raw_bars)} 行")
    if len(raw_bars) > MAX_BARS:
        raise DataError(f"行情最多 {MAX_BARS} 行，当前 {len(raw_bars)} 行；请截取所需的观察区间")

    out: list[Bar] = []
    for i, raw in enumerate(raw_bars):
        if not isinstance(raw, dict):
            raise DataError(f"第 {i + 1} 行不是对象")
        missing = [k for k in REQUIRED_COLUMNS if k not in raw]
        if missing:
            raise DataError(f"第 {i + 1} 行缺少字段：{'、'.join(missing)}")
        try:
            ts = parse_timestamp(raw["timestamp"])
        except DataError as exc:
            raise DataError(f"第 {i + 1} 行：{exc}") from exc

        bar = Bar(
            symbol=symbol,
            timeframe=timeframe,
            open_time=ts,
            close_time=ts + step,
            open=_finite(raw["open"], 1e-12, 1e12, f"第 {i + 1} 行 open"),
            high=_finite(raw["high"], 1e-12, 1e12, f"第 {i + 1} 行 high"),
            low=_finite(raw["low"], 1e-12, 1e12, f"第 {i + 1} 行 low"),
            close=_finite(raw["close"], 1e-12, 1e12, f"第 {i + 1} 行 close"),
            volume=_finite(raw.get("volume") or 0, 0, 1e18, f"第 {i + 1} 行 volume"),
            closed=True,
        )
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
            raise DataError(
                f"第 {i + 1} 行 OHLC 关系无效：high={bar.high:g} low={bar.low:g} open={bar.open:g} close={bar.close:g}"
            )
        if bar.high < bar.low:
            raise DataError(f"第 {i + 1} 行 high 小于 low")

        if out:
            gap = bar.open_time - out[-1].open_time
            if gap == 0:
                raise DataError(f"第 {i + 1} 行与上一行时间重复：{bar.open_time}")
            if gap < 0:
                raise DataError(f"第 {i + 1} 行时间早于上一行（行情必须按时间升序）")
            if require_continuity and gap != step:
                hours = gap / step
                raise DataError(
                    f"第 {i + 1} 行时间不连续：距离上一行 {hours:g} 个 {timeframe}，"
                    "首版只接受无缺口的连续行情；请补齐缺口或改用允许缺口的数据源"
                )
        out.append(bar)
    return out


def parse_csv(text: str, *, symbol: str = "BTC/USDT", timeframe: str = "1h") -> list[Bar]:
    """解析并校验 CSV 行情。

    必需列：timestamp, open, high, low, close；volume 可选。
    """
    if not isinstance(text, str) or not text.strip():
        raise DataError("CSV 内容为空")
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise DataError(f"CSV 最大 {MAX_CSV_BYTES // 1_000_000} MB")

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = reader.fieldnames or []
    if not columns:
        raise DataError("CSV 缺少表头行")
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise DataError(f"CSV 缺少必需列：{'、'.join(missing)}；必需列为 {', '.join(REQUIRED_COLUMNS)}（volume 可选）")

    from ..platform.csvsafe import is_formula

    if any(is_formula(c) for c in columns):
        raise DataError("CSV 表头包含疑似公式的单元格（以 = + - @ 开头），已拒绝")
    rows: list[dict] = []
    for i, row in enumerate(reader):
        if i >= MAX_BARS:
            raise DataError(f"CSV 最多 {MAX_BARS} 行数据")
        bad = next((v for v in row.values() if isinstance(v, str) and is_formula(v)), None)
        if bad is not None:
            raise DataError(f"第 {i + 2} 行包含疑似公式注入的单元格 {bad[:20]!r}，已整体拒绝")
        rows.append(row)
    bars = validate_bars(rows, symbol=symbol, timeframe=timeframe)
    import hashlib
    import time as _t

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    now = int(_t.time() * 1000)
    for n, b in enumerate(bars):
        b.receive_ts, b.sequence, b.source = now, n + 1, "csv"
        b.instrument_id, b.raw_ref = f"csv:{symbol}", f"csv:sha256:{digest}#row{n + 2}"
    return bars


def sample_csv(symbol: str = "BTC/USDT", timeframe: str = "1h", bars: int = 200) -> str:
    """生成一份教学 CSV 样本，让人能立刻跑通导入流程。"""
    from ..data.service import synthetic_bars

    data = synthetic_bars(symbol, timeframe, max(bars, MIN_BARS))
    return DataSet(symbol=symbol, timeframe=timeframe, bars=data, source="synthetic").freeze("教学合成数据").to_csv()
