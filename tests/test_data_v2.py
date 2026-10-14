"""数据层第二版：缓存补缺、来源模式、未收盘过滤、OKX 收盘时间。"""

from __future__ import annotations

import httpx
import pytest

from quant_agent.data.service import DataUnavailable, MarketDataService, synthetic_bars
from quant_agent.exchanges.base import Bar

H = 3_600_000


class FakeAdapter:
    """按请求区间返回确定性的小时线，并记录被调用的区间。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[int, int]] = []
        self.fail = fail

    def fetch_bars(self, symbol, timeframe, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise RuntimeError("网络不可达")
        out = []
        t = start - start % H
        while t < end:
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=t,
                    close_time=t + H,
                    open=100,
                    high=101,
                    low=99,
                    close=100 + (t // H) % 7,
                    volume=10,
                    closed=t + H <= end,
                )
            )
            t += H
        return out


def test_cache_only_fetches_missing_tail(tmp_path):
    adapter = FakeAdapter()
    svc = MarketDataService({"fake": adapter}, cache_root=tmp_path)
    end = 1_700_000_000_000 - 1_700_000_000_000 % H
    bars, source = svc.get_bars("BTC/USDT", "1h", exchange="fake", start=end - 200 * H, end=end)
    assert source == "exchange" and len(bars) == 200
    assert len(adapter.calls) == 1

    # 同区间再取：完全走缓存
    svc.get_bars("BTC/USDT", "1h", exchange="fake", start=end - 200 * H, end=end)
    assert len(adapter.calls) == 1

    # 往后延 10 根：只补尾巴
    bars2, _ = svc.get_bars("BTC/USDT", "1h", exchange="fake", start=end - 200 * H, end=end + 10 * H)
    assert len(adapter.calls) == 2
    assert adapter.calls[-1][0] >= end - H
    assert len(bars2) == 210
    entry = svc.cache.entries()[0]
    assert entry["bars"] == 210 and entry["exchange"] == "fake"


def test_unclosed_bars_are_never_returned(tmp_path):
    svc = MarketDataService({"fake": FakeAdapter()}, cache_root=tmp_path)
    bars, _ = svc.get_bars("BTC/USDT", "1h", exchange="fake", limit=100)
    assert all(b.closed for b in bars)


def test_exchange_mode_fails_loudly(tmp_path):
    svc = MarketDataService({"fake": FakeAdapter(fail=True)}, cache_root=tmp_path)
    with pytest.raises(DataUnavailable, match="网络不可达"):
        svc.get_bars("BTC/USDT", "1h", exchange="fake", limit=100, source="exchange")
    # auto 模式回退合成数据，并记下失败原因
    bars, source = svc.get_bars("BTC/USDT", "1h", exchange="fake", limit=100)
    assert source == "synthetic" and len(bars) == 100
    assert "网络不可达" in svc.last_error["fake"]


def test_synthetic_respects_range_and_symbol_scale():
    end = 1_700_000_000_000 - 1_700_000_000_000 % H
    bars = synthetic_bars("ETH/USDT", "1h", 50, end=end)
    assert bars[-1].close_time == end
    assert 1_000 < bars[0].open < 10_000


def test_okx_close_time_is_open_plus_step():
    from quant_agent.exchanges.okx import OKXAdapter

    t0 = 1_700_000_000_000 - 1_700_000_000_000 % H

    def handler(request: httpx.Request) -> httpx.Response:
        after = int(request.url.params.get("after"))
        if after <= t0:
            return httpx.Response(200, json={"code": "0", "data": []})
        rows = [[str(t0 + i * H), "1", "2", "0.5", "1.5", "10", "0", "0", "1"] for i in (2, 1, 0)]
        return httpx.Response(200, json={"code": "0", "data": rows})

    okx = OKXAdapter(transport=httpx.MockTransport(handler))
    bars = okx.fetch_bars("BTC/USDT", "1h", t0, t0 + 3 * H)
    assert [b.close_time - b.open_time for b in bars] == [H, H, H]


def test_anchored_synthetic_prices_agree_across_windows():
    """平台页面取合成行情时，同一时刻不管取多少根，最新价都一样（顶栏、首页、行情页不再各说各话）。"""
    from quant_agent.data.service import synthetic_bars

    end = 1_760_000_000_000
    a = synthetic_bars("BTC/USDT", "1h", 50, end=end, anchored=True)
    b = synthetic_bars("BTC/USDT", "1h", 1500, end=end, anchored=True)
    assert a[-1].close == b[-1].close and a[0] == b[-50]
    closes = [x.close for x in b]
    assert 20_000 < min(closes) and max(closes) < 180_000  # 有界，不会漂到离谱的价位
    # 默认（不锚定）的序列和取数时刻无关，测试结果可复现
    c = synthetic_bars("BTC/USDT", "1h", 300, end=end)
    d = synthetic_bars("BTC/USDT", "1h", 300, end=end + 3_600_000 * 7)
    assert [x.close for x in c] == [x.close for x in d]
