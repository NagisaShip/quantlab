"""行情校验与 CSV 导入测试（对应 plan.md 第 4 章）。"""

from __future__ import annotations

import csv
import io

import pytest

from quant_agent.data import (
    MAX_BARS,
    MIN_BARS,
    DataError,
    DataSet,
    parse_csv,
    parse_timestamp,
    sample_csv,
    validate_bars,
)


def _csv_from(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def _rows(bars) -> list[dict]:
    return [
        {
            "timestamp": str(b.open_time),
            "open": str(b.open),
            "high": str(b.high),
            "low": str(b.low),
            "close": str(b.close),
            "volume": str(b.volume),
        }
        for b in bars
    ]


@pytest.fixture
def bars():
    return parse_csv(sample_csv("BTC/USDT", "1h", 200))


# ---------------- 时间解析 ----------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1788220800000", 1788220800000),
        ("1788220800", 1788220800000),  # 秒级自动升到毫秒
        ("2026-09-01T00:00:00Z", 1788220800000),
        ("2026-09-01T00:00:00+00:00", 1788220800000),
        ("2026-09-01T08:00:00+08:00", 1788220800000),  # 带偏移，换算到同一时刻
    ],
)
def test_parse_timestamp_formats(raw, expected):
    assert parse_timestamp(raw) == expected


def test_naive_iso_is_rejected():
    with pytest.raises(DataError, match="必须带时区"):
        parse_timestamp("2026-09-01T00:00:00")


def test_empty_and_unknown_timestamp_rejected():
    with pytest.raises(DataError):
        parse_timestamp("")
    with pytest.raises(DataError, match="无法解析"):
        parse_timestamp("昨天")


# ---------------- 严格校验 ----------------
def test_csv_roundtrip_is_stable(bars):
    again = parse_csv(DataSet("BTC/USDT", "1h", bars, "csv").to_csv())
    assert again == bars


def test_missing_gap_is_rejected(bars):
    rows = _rows(bars)
    rows[110]["timestamp"] = str(int(rows[110]["timestamp"]) + 3_600_000)
    with pytest.raises(DataError, match="时间不连续"):
        parse_csv(_csv_from(rows))


def test_duplicate_timestamp_is_rejected(bars):
    rows = _rows(bars)
    rows[110]["timestamp"] = rows[109]["timestamp"]
    with pytest.raises(DataError, match="时间重复"):
        parse_csv(_csv_from(rows))


def test_out_of_order_is_rejected(bars):
    rows = _rows(bars)
    rows[110]["timestamp"] = str(int(rows[110]["timestamp"]) - 7_200_000)
    with pytest.raises(DataError, match="必须按时间升序"):
        parse_csv(_csv_from(rows))


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "abc", ""])
def test_non_finite_values_are_rejected(bars, value):
    rows = _rows(bars)
    rows[10]["close"] = value
    with pytest.raises(DataError):
        parse_csv(_csv_from(rows))


def test_ohlc_relationship_is_enforced(bars):
    rows = _rows(bars)
    rows[10]["high"] = "1"  # high 低于 open/close
    with pytest.raises(DataError, match="OHLC 关系无效"):
        parse_csv(_csv_from(rows))


def test_missing_required_column(bars):
    """缺列必须报错。这里把 high 列从表头与每一行同时移除。"""
    rows = _rows(bars)
    for row in rows:
        row.pop("high")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["timestamp", "open", "low", "close", "volume"])
    writer.writeheader()
    writer.writerows(rows)
    with pytest.raises(DataError, match="缺少必需列"):
        parse_csv(out.getvalue())


def test_volume_is_optional(bars):
    """volume 可选：只给 OHLC 也要能导入，缺省的成交量记为 0。"""
    rows = _rows(bars)
    for row in rows:
        row.pop("volume")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["timestamp", "open", "high", "low", "close"])
    writer.writeheader()
    writer.writerows(rows)
    parsed = parse_csv(out.getvalue())
    assert len(parsed) == len(bars)
    assert all(b.volume == 0 for b in parsed)


def test_row_count_limits():
    with pytest.raises(DataError, match=f"至少需要 {MIN_BARS} 行"):
        validate_bars([{"timestamp": i * 3_600_000, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(50)])
    with pytest.raises(DataError, match=f"最多 {MAX_BARS} 行"):
        validate_bars(
            [{"timestamp": i * 3_600_000, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(MAX_BARS + 1)]
        )


def test_csv_size_limit():
    with pytest.raises(DataError, match="最大"):
        parse_csv("x" * 2_100_000)


def test_empty_csv_rejected():
    with pytest.raises(DataError):
        parse_csv("")
    with pytest.raises(DataError, match="缺少必需列"):
        parse_csv("a,b,c\n1,2,3\n")


def test_unknown_timeframe_rejected():
    with pytest.raises(DataError, match="不支持的周期"):
        validate_bars(
            [{"timestamp": i * 60_000, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(200)],
            timeframe="7s",
        )


# ---------------- 摘要与冻结 ----------------
def test_dataset_digest_is_stable_and_content_sensitive(bars):
    a = DataSet("BTC/USDT", "1h", bars, "csv").freeze("用户 CSV")
    b = DataSet("BTC/USDT", "1h", bars, "csv").freeze("用户 CSV")
    assert a.digest == b.digest
    assert a.source_label == "用户 CSV"

    changed = list(bars)
    changed[10] = type(changed[10])(**{**changed[10].__dict__, "close": changed[10].close + 1})
    c = DataSet("BTC/USDT", "1h", changed, "csv").freeze("用户 CSV")
    assert c.digest != a.digest

    summary = a.summary()
    assert summary["bars"] == len(bars)
    assert summary["start_ts"] == bars[0].open_time
