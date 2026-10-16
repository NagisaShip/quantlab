"""Freqtrade 导入：常见策略能导入，逐 bar 运行与整段向量化计算的信号一致，导出能还原。"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from quant_agent.data.quality import TIMEFRAME_MS
from quant_agent.exchanges.base import Bar
from quant_agent.sdk import load_source, run_backtest
from quant_agent.sdk.ft import DataFrame, vectorized_signals
from quant_agent.strategies.freqtrade_import import convert, export, is_freqtrade

SAMPLES = Path(__file__).parent / "freqtrade_samples"
UNSUPPORTED = {"pattern_recognition", "pandas_ta_strategy", "futures_short"}


def _sources() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(SAMPLES.glob("*.py"))}


def _bars(tf: str, n: int) -> list[Bar]:
    step = TIMEFRAME_MS[tf]
    t0 = 1_700_000_000_000 - 1_700_000_000_000 % 86_400_000
    out, p = [], 100.0
    for i in range(n):
        o = p
        p = 100 * (1 + 0.1 * math.sin(i / 23) + 0.04 * math.sin(i / 6.1) + 0.015 * math.sin(i * 1.7)) + 0.01 * i
        hi, lo = max(o, p) * (1.002 + 0.002 * abs(math.sin(i))), min(o, p) * (0.998 - 0.002 * abs(math.cos(i)))
        out.append(
            Bar("BTC/USDT", tf, t0 + i * step, t0 + (i + 1) * step - 1, o, hi, lo, p, 1000 + 400 * abs(math.cos(i / 7)))
        )
    return out


def test_common_strategies_import_and_unsupported_explain():
    srcs = _sources()
    ok, failed = [], {}
    for name, src in srcs.items():
        assert is_freqtrade(src)
        rep = convert(src)
        if rep.ok:
            ok.append(name)
        else:
            failed[name] = rep.errors
    common = [n for n in srcs if n not in UNSUPPORTED]
    assert len([n for n in ok if n in common]) >= 16, failed
    for n in UNSUPPORTED:
        assert failed.get(n), n


@pytest.mark.parametrize("name", sorted(set(p.stem for p in SAMPLES.glob("*.py")) - UNSUPPORTED))
def test_bar_by_bar_signals_match_vectorized(name):
    src = (SAMPLES / f"{name}.py").read_text(encoding="utf-8")
    rep = convert(src, smoke=False)
    if not rep.ok:
        pytest.skip("; ".join(rep.errors))
    tf = rep.timeframe if rep.timeframe in TIMEFRAME_MS else "1h"
    bars = _bars(tf, 900)
    mod = load_source(rep.source, name=f"ft_{name}", params={"ft_record": True})
    ft = mod.fn("FT")
    res = run_backtest(mod, {"BTC/USDT": bars}, timeframe=tf)
    assert not (getattr(res, "errors", None) or []), res.errors
    live = {ts: (e, x) for ts, _pair, e, x in ft.signals}
    assert live, "逐 bar 运行没有记录到信号"

    # 参照：整段一次算完（像 Freqtrade 回测）
    df = DataFrame(
        {
            "date": np.array([b.open_time for b in bars], dtype="int64").astype("datetime64[ms]"),
            "open": np.array([b.open for b in bars]),
            "high": np.array([b.high for b in bars]),
            "low": np.array([b.low for b in bars]),
            "close": np.array([b.close for b in bars]),
            "volume": np.array([b.volume for b in bars]),
        }
    )
    ref = vectorized_signals(type(ft.s), df, params=ft.params)
    by_close = {b.close_time: i for i, b in enumerate(bars)}
    by_open = {b.open_time: i for i, b in enumerate(bars)}
    keyed = by_close if sum(t in by_close for t in live) >= sum(t in by_open for t in live) else by_open
    start = max(int(getattr(ft.s, "startup_candle_count", 0) or 0), 30)
    pairs = [(live[t], ref[keyed[t]]) for t in live if t in keyed and keyed[t] >= start]
    assert len(pairs) > 300
    same = sum(1 for a, b in pairs if a == b)
    assert same / len(pairs) >= 0.99, f"{name}: {same}/{len(pairs)}"


def test_export_round_trip_keeps_class_and_writes_params():
    src = (SAMPLES / "sample_strategy.py").read_text(encoding="utf-8")
    rep = convert(src, smoke=False)
    assert rep.ok and rep.params
    name, val = next((k, v) for k, v in rep.params.items() if isinstance(v, int) and not isinstance(v, bool))
    out = export(rep.source, {name: val + 1})
    assert "quant_agent" not in out
    assert "from freqtrade" in out or "import talib" in out
    assert f"class {rep.class_name}" in out
    again = convert(out, smoke=False)
    assert again.ok, again.errors
    assert again.params[name] == val + 1
    with pytest.raises(ValueError):
        export("def initialize(ctx):\n    pass\n")


# ---------------------------------------------------------------- 接口
@pytest.fixture()
def client(tmp_path):
    from conftest import login
    from fastapi.testclient import TestClient

    from quant_agent.api.app import create_app
    from quant_agent.api.deps import AppServices, reset_services, set_services

    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_api_check_import_export(client):
    src = (SAMPLES / "strategy001.py").read_text(encoding="utf-8")
    r = client.post("/api/freqtrade/check", json={"source": src})
    assert r.status_code == 200 and r.json()["ok"], r.text
    r = client.post("/api/freqtrade/import", json={"source": src, "name": "S1 导入", "checkup": False})
    body = r.json()
    assert r.status_code == 200 and body["strategy"], r.text
    slug = body["strategy"]["slug"]
    meta = body["strategy"]
    assert meta["timeframe"] == "5m" and meta["source"] == "freqtrade"
    out = client.get(f"/api/freqtrade/export/{slug}")
    assert out.status_code == 200 and "class Strategy001" in out.text and "quant_agent" not in out.text
    bad = client.post(
        "/api/freqtrade/import",
        json={"source": (SAMPLES / "futures_short.py").read_text(encoding="utf-8"), "checkup": False},
    )
    assert bad.json()["strategy"] is None and bad.json()["report"]["errors"]
    assert client.post("/api/freqtrade/check", json={"source": "x = 1\n"}).status_code == 400
    assert "RSI" in client.get("/api/freqtrade/support").json()["ta"]


def test_api_rewrite_falls_back_without_model(client):
    src = (SAMPLES / "futures_short.py").read_text(encoding="utf-8")
    r = client.post("/api/freqtrade/rewrite", json={"source": src, "errors": ["杠杆"]})
    assert r.status_code == 200
    assert "mode" in r.json()


# ---------------------------------------------------------------- ccxt 行情
def test_ccxt_adapter_pages_ohlcv_with_fake_client():
    from quant_agent.exchanges import public_adapter
    from quant_agent.exchanges.base import UnsupportedFeature
    from quant_agent.exchanges.ccxt_adapter import PAGE, CcxtAdapter, available

    step = 3_600_000
    t0 = 1_700_000_000_000 - 1_700_000_000_000 % step

    class Fake:
        timeframes = {"1h": "1h"}
        calls = 0

        def fetch_ohlcv(self, symbol, tf, since=None, limit=None):
            Fake.calls += 1
            n0 = max(0, (since - t0) // step)
            return [[t0 + i * step, 1, 2, 0.5, 1.5, 10] for i in range(n0, min(n0 + limit, 1200))]

        def load_markets(self):
            return {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "spot": True,
                    "base": "BTC",
                    "quote": "USDT",
                    "precision": {"price": 0.01, "amount": 0.0001},
                    "limits": {"cost": {"min": 5}},
                }
            }

        def fetch_ticker(self, s):
            return {"last": 1.5, "bid": 1.4, "ask": 1.6}

    ad = CcxtAdapter("kraken", client=Fake())
    bars = ad.fetch_bars("BTC/USDT", "1h", t0, t0 + 1100 * step)
    assert len(bars) == 1101 and bars[0].open_time == t0 and bars[-1].close_time == t0 + 1101 * step
    assert Fake.calls == 3 and PAGE == 500
    assert ad.get_instruments()[0].min_notional == "5"
    assert ad.fetch_ticker("BTC/USDT")["price"] == 1.5
    with pytest.raises(UnsupportedFeature):
        ad.fetch_bars("BTC/USDT", "4h", t0, t0 + step)
    if not available():
        with pytest.raises(UnsupportedFeature):
            public_adapter("ccxt:kraken")
