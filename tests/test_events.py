"""第 5 阶段信息源：交易所公告、新闻标注、宏观日历、强平流、Deribit 波动率、FRED 发布滞后、事件因子。

全部用 httpx.MockTransport，不连外网。重点验证：每条数据都有 available_at，物化后的因子字段不会看到未来。
"""

from __future__ import annotations

import json

import httpx
import numpy as np
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.llm.base import ChatResult
from quant_agent.research import factor_expr as FX
from quant_agent.research.datahub import CONNECTOR_BY_ID, DataHub
from quant_agent.research.events import (
    EventHub,
    LiquidationRecorder,
    classify_announcement,
    coins_in,
    deribit_dvol,
    deribit_skew,
    parse_force_order,
    rule_tag,
)
from quant_agent.store import JsonStore

H = 3_600_000
DAY = 86_400_000
NOW = 1_790_000_000_000 // H * H


def _routes(table):
    def handler(req: httpx.Request) -> httpx.Response:
        for (host, path), resp in table.items():
            if req.url.host == host and req.url.path == path:
                return resp(req) if callable(resp) else resp
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


ANN = {
    ("www.binance.com", "/bapi/composite/v1/public/cms/article/list/query"): lambda req: httpx.Response(
        200,
        json={
            "data": {
                "catalogs": [
                    {
                        "articles": {
                            "48": [
                                {
                                    "code": "a1",
                                    "title": "Binance Will List Foo (FOO) with Seed Tag Applied",
                                    "releaseDate": NOW - 10 * H,
                                }
                            ],
                            "161": [
                                {
                                    "code": "a2",
                                    "title": "Binance Will Delist BAR, BAZ on 2026-10-20",
                                    "releaseDate": NOW - 5 * H,
                                }
                            ],
                            "157": [
                                {
                                    "code": "a3",
                                    "title": "Binance Will Perform Wallet Maintenance for Ethereum (ETH)",
                                    "releaseDate": NOW - 2 * H,
                                }
                            ],
                        }[req.url.params["catalogId"]]
                    }
                ]
            }
        },
    ),
    ("www.okx.com", "/api/v5/support/announcements"): lambda req: httpx.Response(
        200,
        json={
            "code": "0",
            "data": [
                {
                    "details": [
                        {
                            "title": "OKX to list QUX (QUX) for spot trading",
                            "url": "https://okx.com/a/q",
                            "pTime": str(NOW - 8 * H),
                        }
                    ]
                    if req.url.params["annType"] == "announcements-new-listings"
                    else []
                }
            ],
        },
    ),
}


def test_title_parsing():
    assert coins_in("Binance Will List Foo (FOO) and BAR/USDT") == ["FOO", "BAR"]
    assert "BTC" in coins_in("Bitcoin ETF sees record inflows")
    assert "USDT" not in coins_in("New USDT pairs (USDT)")
    assert classify_announcement("Binance Will Delist XYZ") == "delisting"
    assert classify_announcement("Notice of Wallet Maintenance") == "maintenance"
    assert classify_announcement("OKX to list ABC") == "listing"
    t = rule_tag("Major exchange hacked, $100M stolen from Ethereum bridge")
    assert t["direction"] == -1 and t["type"] == "hack" and "ETH" in t["coins"]


def test_announcements_saved_with_available_at_and_materialized_without_lookahead(tmp_path):
    store = JsonStore(tmp_path)
    hub = DataHub(store, transport=_routes(ANN))
    res = hub.sync("announcements")
    assert res["ok"] and res["new"] == 4
    ev = EventHub(store, hub, clock=lambda: NOW)
    rows = ev.events()
    assert all(r["available_at"] > r["ts"] for r in rows)
    types = {r["type"] for r in rows}
    assert {"listing", "delisting", "maintenance"} <= types
    delist = next(r for r in rows if r["type"] == "delisting")
    assert set(delist["coins"]) >= {"BAR", "BAZ"} and delist["direction"] == -1

    ds = {d["key"]: d for d in hub.datasets()}
    key = "ev_listing:FOO/USDT:events"
    assert key in ds and ds[key]["lag_ms"] == 0
    series = hub.series(key)
    ann_ts = NOW - 10 * H
    before = [r for r in series if r[1] < ann_ts]
    after = [r for r in series if r[1] >= ann_ts + H]
    assert all(r[2] == 0 for r in before)  # 公告之前看不到它
    assert after and after[0][2] > 0
    # 24 小时半衰期
    one_day = next(r for r in series if r[1] >= ann_ts + 24 * H + 5 * 60_000)
    assert 0.25 < one_day[2] / after[0][2] < 0.6
    assert hub.dataset("ev_delisting:BAR/USDT:events")


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin ETF approval sparks rally</title><link>https://x/1</link>
<pubDate>Mon, 12 Oct 2026 08:00:00 GMT</pubDate></item>
<item><title>Solana DeFi protocol exploited for $20M</title><link>https://x/2</link>
<pubDate>Mon, 12 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def _news_routes(cryptopanic: bool = False):
    t = {
        ("www.coindesk.com", "/arc/outboundfeeds/rss/"): httpx.Response(200, text=RSS),
        ("cointelegraph.com", "/rss"): httpx.Response(200, text="<rss><channel></channel></rss>"),
    }
    if cryptopanic:
        t[("cryptopanic.com", "/api/developer/v2/posts/")] = httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 9,
                        "title": "ETH upgrade goes live",
                        "published_at": "2026-10-12T10:00:00Z",
                        "currencies": [{"code": "ETH"}],
                        "votes": {"positive": 5},
                    }
                ]
            },
        )
    return _routes(t)


def test_news_tagged_by_llm_records_model_prompt_and_hindsight(tmp_path):
    calls = []

    def llm(system, user):
        calls.append(user)
        return ChatResult(
            content=json.dumps(
                [
                    {"i": 0, "type": "flows", "coins": ["BTC"], "direction": 1, "strength": 0.7},
                    {"i": 1, "type": "hack", "coins": ["SOL"], "direction": -1, "strength": 0.6},
                ]
            ),
            model="tagger-model",
            provider="p",
        )

    store = JsonStore(tmp_path)
    hub = DataHub(store, transport=_news_routes(), llm=llm)
    res = hub.sync("news")
    assert res["new"] == 2 and res["tagger"]["kind"] == "llm" and res["tagger"]["model"] == "tagger-model"
    rows = {r["title"][:7]: r for r in EventHub(store, hub).events()}
    btc = rows["Bitcoin"]
    assert btc["direction"] == 1 and btc["coins"] == ["BTC"] and btc["tagger"]["prompt_version"] == "news-tag-v1"
    assert btc["tagger"]["prompt_sha"] and btc["available_at"] == btc["ts"] + 5 * 60_000
    # 2026-10-12 的新闻在 2026-10 之后才打标签：标注时间晚于发布超过一天 → 回看偏差风险
    assert isinstance(btc["hindsight_risk"], bool)
    ds = hub.dataset("ev_news:BTC/USDT:events")
    assert ds and ds["tagger"]["model"] == "tagger-model"
    # 再同步不会重复标注
    hub.sync("news")
    assert len(calls) == 1


def test_news_falls_back_to_rules_and_uses_cryptopanic_key(tmp_path):
    def broken(system, user):
        raise RuntimeError("no model")

    store = JsonStore(tmp_path)
    hub = DataHub(store, transport=_news_routes(cryptopanic=True), llm=broken, secrets_fn=lambda: {"datahub:news": "k"})
    res = hub.sync("news")
    assert res["new"] == 3 and res["tagger"]["kind"] == "rules" and "fallback_reason" in res["tagger"]
    sol = next(r for r in EventHub(store, hub).events() if "Solana" in r["title"])
    assert sol["direction"] == -1 and "SOL" in sol["coins"] and not sol["hindsight_risk"]


def test_macro_calendar_known_in_advance(tmp_path):
    from datetime import datetime, timezone

    ev_time = datetime.fromtimestamp((NOW + 30 * H) / 1000, tz=timezone.utc).isoformat()
    rows = [
        {"title": "CPI m/m", "country": "USD", "date": ev_time, "impact": "High"},
        {"title": "Minor", "country": "USD", "date": ev_time, "impact": "Low"},
        {"title": "ECB", "country": "EUR", "date": ev_time, "impact": "High"},
    ]
    store = JsonStore(tmp_path)
    hub = DataHub(
        store,
        transport=_routes({("nfs.faireconomy.media", "/ff_calendar_thisweek.json"): httpx.Response(200, json=rows)}),
    )
    ev = EventHub(store, hub, transport=hub.transport, clock=lambda: NOW)
    res = ev.sync("macro_calendar")
    assert res["new"] == 1
    e = ev.events()[0]
    assert e["available_at"] <= NOW < e["ts"] and e["strength"] == 1.0
    ser = hub.series("ev_macro:*:events")
    # 离事件还有 30 小时：不在未来 24 小时窗口里，值为 0
    assert ser and all(r[2] == 0.0 for r in ser)
    # 时间走到事件前 10 小时：窗口覆盖到它，值为 1
    ev.now = lambda: NOW + 20 * H
    ev.materialize()
    ser = hub.series("ev_macro:*:events")
    assert ser[-1][2] == 1.0 and ser[0][2] == 0.0


def test_force_order_parsing_and_hourly_recorder(tmp_path):
    msg = {
        "e": "forceOrder",
        "E": NOW + 10,
        "o": {"s": "BTCUSDT", "S": "SELL", "q": "0.5", "z": "0.5", "ap": "60000", "p": "59990", "T": NOW + 10},
    }
    row = parse_force_order(msg)
    assert row == {"symbol": "BTC/USDT", "side": "long", "notional": 30000.0, "ts": NOW + 10}
    assert parse_force_order({"e": "trade"}) is None
    hub = DataHub(JsonStore(tmp_path))
    rec = LiquidationRecorder(hub)
    rec.add(msg)
    rec.add({**msg, "o": {**msg["o"], "S": "BUY", "z": "1", "ap": "60000"}})
    assert rec.flush(now=NOW + 30 * 60_000) == []  # 这一小时还没结束
    out = rec.flush(now=NOW + H)
    keys = {r["key"] for r in out}
    assert keys == {"liq_long:BTC/USDT:binance_ws", "liq_short:BTC/USDT:binance_ws"}
    assert hub.series("liq_long:BTC/USDT:binance_ws") == [[NOW, NOW + H, 30000.0]]


def test_deribit_dvol_and_skew():
    def handler(req):
        if req.url.path.endswith("get_volatility_index_data"):
            return httpx.Response(
                200, json={"result": {"data": [[NOW - H, 50, 52, 49, 51], [NOW, 51, 53, 50, 52]], "continuation": None}}
            )
        exp = "13NOV26"
        rows = [
            {"instrument_name": f"BTC-{exp}-54000-P", "mark_iv": 58.0, "underlying_price": 60000},
            {"instrument_name": f"BTC-{exp}-66000-C", "mark_iv": 50.0, "underlying_price": 60000},
            {"instrument_name": f"BTC-{exp}-60000-C", "mark_iv": 52.0, "underlying_price": 60000},
        ]
        return httpx.Response(200, json={"result": rows})

    c = httpx.Client(base_url="https://www.deribit.com", transport=httpx.MockTransport(handler))
    rows = deribit_dvol(c, "BTC", NOW - DAY, NOW)
    assert rows == [(NOW - H, NOW, 51.0), (NOW, NOW + H, 52.0)]  # 小时 K 线收盘后才可用
    from datetime import datetime, timezone

    now = int(datetime(2026, 10, 14, tzinfo=timezone.utc).timestamp() * 1000)
    assert deribit_skew(c, "BTC", now) == (now, now, 8.0)


def test_fred_monthly_series_use_publication_lag(tmp_path):
    def handler(req):
        return httpx.Response(200, json={"observations": [{"date": "2026-08-01", "value": "320.1"}]})

    hub = DataHub(JsonStore(tmp_path), transport=httpx.MockTransport(handler), secrets_fn=lambda: {"datahub:fred": "k"})
    out = hub.sync("fred", series="CPIAUCSL", name="cpi")
    rec = out["datasets"][0]
    assert rec["lag_days"] == 45 and rec["lag_ms"] == 45 * DAY


def test_event_fields_usable_in_factor_expressions():
    for f in ("ev_news", "ev_listing", "ev_delisting", "liq_long", "dvol", "iv_skew", "ev_macro"):
        FX.parse(f"ts_mean({f}, 3)")
    hub = DataHub(JsonStore.__new__(JsonStore))
    times = np.arange(500) * H
    close = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 500)))
    for f in ("ev_news", "liq_short", "dvol", "iv_skew", "ev_delisting"):
        v = hub.synthetic(f, times, close, "BTC/USDT")
        assert np.isfinite(v).all()
    assert (hub.synthetic("ev_delisting", times, close, "X") <= 0).all()
    for cid in ("announcements", "news", "liquidations", "deribit", "macro_calendar"):
        assert CONNECTOR_BY_ID[cid]["fields"]


def test_datahub_api_events_and_liquidation_status(tmp_path, monkeypatch):
    reset_services()
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        cons = c.get("/api/datahub/connectors").json()["connectors"]
        ids = {x["id"] for x in cons}
        assert {"announcements", "news", "liquidations", "deribit", "macro_calendar"} <= ids
        news = next(x for x in cons if x["id"] == "news")
        assert news["has_key"] is False
        r = c.get("/api/datahub/events").json()
        assert r["events"] == [] and r["total"] == 0
        st = c.get("/api/datahub/liquidations").json()
        assert st["stats"]["running"] is False
        assert c.post("/api/datahub/sync", json={"connector": "liquidations"}).status_code == 400
    reset_services()
