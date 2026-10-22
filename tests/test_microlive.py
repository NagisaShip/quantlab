"""微实盘：实盘客户端只做现货、Key 三项自检、资金上限按交易所余额硬核对、止损单挂不上就停开仓、
开关 / 风险告知 / 档位 / 启动确认码 / 一键停止的端到端流程。不连任何交易所。"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_demo_trading import FLIP, FakeBroker, _intent

from quant_agent.api import tasks
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.data.quality import TIMEFRAME_MS
from quant_agent.engine.live import clock_synthetic_bars
from quant_agent.live import demo_broker as DB
from quant_agent.live import micro as MI
from quant_agent.live.oms import OmsLimits, OmsState
from quant_agent.platform import context
from quant_agent.platform.errors import AppError
from quant_agent.research import graduation as GR


# ---------------------------------------------------------------- 客户端
def test_live_brokers_hit_live_hosts_spot_only():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/api/v3/account":
            return httpx.Response(200, json={"balances": [{"asset": "USDT", "free": "10", "locked": "0"}]})
        return httpx.Response(
            200, json={"code": "0", "data": [{"details": [{"ccy": "USDT", "availBal": "5", "frozenBal": "0"}]}]}
        )

    b = MI.make_live_broker("binance", {"api_key": "k", "api_secret": "s"}, transport=httpx.MockTransport(handler))
    assert b.balances() == {"USDT": Decimal("10")}
    with pytest.raises(DB.BrokerError):
        b.place_market("BTC/USDT:PERP", "buy", Decimal("1"), "q1")
    with pytest.raises(DB.BrokerError):
        b.prepare_perp("BTC/USDT:PERP", Decimal("2"))
    o = MI.make_live_broker(
        "okx", {"api_key": "k", "api_secret": "s", "passphrase": "p"}, transport=httpx.MockTransport(handler)
    )
    o.balances()
    assert [r.url.host for r in seen] == ["api.binance.com", "www.okx.com"]
    assert "x-simulated-trading" not in seen[1].headers
    with pytest.raises(AppError):
        MI.make_live_broker("bybit", {"api_key": "k", "api_secret": "s"})
    # 模拟盘模块里依然没有实盘环境
    assert all(
        "demo" in m["base"] or "testnet" in m["base"] or m["base"] == "https://www.okx.com"
        for m in DB.ENVIRONMENTS.values()
    )


def test_key_check_requires_trade_no_withdraw_ip(monkeypatch):
    def probe(flags):
        monkeypatch.setattr(MI, "permission_probe", lambda ex, creds: {**flags, "account_id": "u***1"})
        return MI.key_check("binance", {"api_key": "k", "api_secret": "s"})

    assert probe({"can_trade": True, "can_withdraw": False, "ip_restricted": True})["ok"]
    r = probe({"can_trade": True, "can_withdraw": True, "ip_restricted": True})
    assert not r["ok"] and any("提现" in x for x in r["reasons"])
    assert not probe({"can_trade": True, "can_withdraw": False, "ip_restricted": False})["ok"]
    assert not probe({"can_trade": True, "can_withdraw": False, "ip_restricted": None})["ok"]
    assert not probe({"can_trade": False, "can_withdraw": False, "ip_restricted": True})["ok"]


# ---------------------------------------------------------------- OMS
def _oms(cap=100.0, blocked=None, usdt="1000"):
    b = FakeBroker(usdt=usdt, price=10.0)
    oms = MI.LiveOMS(
        b, OmsLimits(max_order_notional=10_000), OmsState(), initial_cash=cap, cap=cap, quote="USDT", blocked_fn=blocked
    )
    oms.snapshot_baseline()
    return oms, b


def test_live_oms_cap_uses_exchange_balance():
    oms, b = _oms(cap=100)
    assert oms.submit(_intent(qty="6", price="10", coid="a"))["status"] == "filled"  # 60
    rej = oms.submit(_intent(qty="5", price="10", coid="b"))  # 60 + 50 > 100
    assert rej["status"] == "rejected" and "资金上限" in rej["message"]
    # 有人在账户里另外花了钱：按交易所余额算，仍然挡住
    b.bal["USDT"] -= Decimal("35")
    assert oms.submit(_intent(qty="0.6", price="10", coid="c"))["status"] == "rejected"
    # 卖出（减仓）永远允许
    assert oms.submit(_intent(side="sell", qty="6", price="10", coid="d"))["status"] == "filled"


def test_live_oms_kill_blocks_opens_not_reductions():
    flag = {"why": None}
    oms, _b = _oms(cap=1000, blocked=lambda: flag["why"])
    assert oms.submit(_intent(qty="2", price="10", coid="a"))["status"] == "filled"
    flag["why"] = "已一键停止"
    r = oms.submit(_intent(qty="1", price="10", coid="b"))
    assert r["status"] == "rejected" and "一键停止" in r["message"]
    assert oms.submit(_intent(side="sell", qty="2", price="10", coid="c"))["status"] == "filled"
    assert oms.submit({**_intent(qty="1", price="10", coid="p"), "perp": True})["status"] == "rejected"


def test_live_oms_halts_when_stop_rejected():
    oms, b = _oms()
    b.place_stop = lambda *a, **k: (_ for _ in ()).throw(DB.BrokerError("不支持"))
    oms._protect_place({"security": "BTC/USDT", "side": "sell", "qty": "1", "trigger": "9", "client_order_id": "qp1"})
    assert oms.state.halted and "裸仓" in oms.state.halt_reason


# ---------------------------------------------------------------- 端到端
@pytest.fixture()
def env(tmp_path, monkeypatch):
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)
    holder = {"broker": FakeBroker(usdt="500")}
    monkeypatch.setattr(MI, "broker_factory", lambda ex, creds: holder["broker"])
    monkeypatch.setattr(
        MI,
        "permission_probe",
        lambda ex, creds: {"can_trade": True, "can_withdraw": False, "ip_restricted": True, "account_id": "u***1"},
    )
    tf, step = "1h", TIMEFRAME_MS["1h"]
    clock = {"end": 1_700_000_000_000 + 400 * step}

    def bars(services, session, count):
        out = {}
        for sec in session.securities or [session.symbol]:
            got = clock_synthetic_bars(sec, tf, clock["end"] - count * step, clock["end"])
            out[sec] = got
            holder["broker"].price = Decimal(str(got[-1].close))
        return out

    monkeypatch.setattr(tasks, "latest_bars", bars)
    monkeypatch.setattr(services.market, "ticker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    with TestClient(create_app()) as c:
        login(c)
        yield c, holder, clock, step
    reset_services()


def _graduated_strategy(c, name="微实盘策略"):
    r = c.post(
        "/api/studio/strategies", json={"name": name, "code": FLIP, "securities": ["BTC/USDT"], "timeframe": "1h"}
    )
    slug = r.json()["strategy"]["slug"]
    c.post(f"/api/studio/strategies/{slug}/approve")
    with context.bind(c.headers["X-Workspace-Id"], user_id="u"):
        get_services().store.upsert(GR.GRAD, {"id": slug, "slug": slug, "stage": "graduated", "history": []})
    return slug


def test_microlive_end_to_end(env, monkeypatch):
    c, holder, clock, step = env
    slug = _graduated_strategy(c)
    # 真实资金能力默认关闭：验证微实盘流程前，先由工作区管理员显式开启。
    enabled = c.put("/api/workspace/features", json={"features": {"execution.real_spot": True}})
    assert enabled.status_code == 200, enabled.text
    st = c.get("/api/microlive").json()["status"]
    assert not st["ready"] and any("QAGENT_MICROLIVE" in b for b in st["blockers"])
    assert "real money" in st["notice_en"] and st["ack_phrase_en"] == MI.ACK_PHRASE_EN
    monkeypatch.setenv(MI.SERVER_FLAG, "1")

    # 打开：要读告知、输入原话
    assert (
        c.post("/api/microlive/enable", json={"phrase": "好", "notice_version": st["notice_current"]}).status_code
        == 400
    )
    assert c.post("/api/microlive/enable", json={"phrase": MI.ACK_PHRASE, "notice_version": "old"}).status_code == 400
    r = c.post("/api/microlive/enable", json={"phrase": MI.ACK_PHRASE, "notice_version": st["notice_current"]})
    assert r.json()["status"]["ready"], r.text

    # Key：自检不过不保存
    monkeypatch.setattr(
        MI, "permission_probe", lambda ex, creds: {"can_trade": True, "can_withdraw": True, "ip_restricted": True}
    )
    r = c.post(
        "/api/microlive/connections",
        json={"exchange": "binance", "api_key": "livekey-123456", "api_secret": "sec-123456"},
    )
    assert r.status_code == 403 and "提现" in r.json()["message"]
    monkeypatch.setattr(
        MI, "permission_probe", lambda ex, creds: {"can_trade": True, "can_withdraw": False, "ip_restricted": True}
    )
    r = c.post(
        "/api/microlive/connections",
        json={"exchange": "binance", "api_key": "livekey-123456", "api_secret": "sec-123456"},
    )
    assert r.status_code == 200, r.text
    cid = r.json()["connection"]["id"]
    assert "sec-123456" not in c.get("/api/microlive").text

    # 普通启动入口不能开实盘
    r = c.post("/api/studio/paper/start", json={"slug": slug, "execution": "live", "source": "exchange"})
    assert r.status_code == 422

    base = {"slug": slug, "connection_id": cid, "symbol": "BTC/USDT", "stop_loss": 0.05}
    r = c.post("/api/microlive/prepare", json={**base, "initial_cash": 200})
    assert r.status_code == 400 and "资金上限" in r.json()["message"]
    ch = c.post("/api/microlive/prepare", json={**base, "initial_cash": 100}).json()
    assert ch["summary"]["cap"] == 100 and len(ch["code"]) == 6
    bad = "000000" if ch["code"] != "000000" else "111111"
    assert (
        c.post(
            "/api/microlive/start", json={"challenge_id": ch["challenge_id"], "code": bad, "phrase": MI.START_PHRASE}
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/microlive/start", json={"challenge_id": ch["challenge_id"], "code": ch["code"], "phrase": "好"}
        ).status_code
        == 400
    )
    r = c.post(
        "/api/microlive/start", json={"challenge_id": ch["challenge_id"], "code": ch["code"], "phrase": MI.START_PHRASE}
    )
    assert r.status_code == 200, r.text
    sid = r.json()["run_id"]
    # 同一个确认码不能再用
    assert (
        c.post(
            "/api/microlive/start",
            json={"challenge_id": ch["challenge_id"], "code": ch["code"], "phrase": MI.START_PHRASE},
        ).status_code
        == 400
    )
    with context.bind(c.headers["X-Workspace-Id"], user_id="u"):
        run = get_services().runs.get(sid)
    assert run["mode"] == "micro_live" and run["manifest"]["risk"]["stop_loss"] == 0.05

    for _ in range(4):
        clock["end"] += step
        s = c.post(f"/api/studio/paper/{sid}/tick").json()["session"]
    b = holder["broker"]
    assert s["execution"] == "live" and b.placed, s.get("events")
    spent = Decimal("500") - b.bal["USDT"]
    assert spent <= Decimal("100.1")

    # 一键停止：平仓、停止、拒绝新的启动
    r = c.post("/api/microlive/kill").json()
    assert sid in r["stopped"] and r["status"]["killed"]
    from quant_agent.store import Collections

    with context.bind(c.headers["X-Workspace-Id"], user_id="u"):
        s = get_services().store.get(Collections.PAPER_SESSIONS, sid)
    assert s["status"] == "stopped"
    assert b.bal.get("BTC", Decimal(0)) == 0
    r = c.post("/api/microlive/prepare", json={**base, "initial_cash": 100})
    assert r.status_code == 403 and "一键停止" in r.json()["message"]
    assert c.post("/api/microlive/rearm", json={"phrase": "x"}).status_code == 400
    assert c.post("/api/microlive/rearm", json={"phrase": MI.ACK_PHRASE}).json()["status"]["ready"]


def test_only_graduated_and_tier_raise_rules(env, monkeypatch):
    c, _holder, *_ = env
    monkeypatch.setenv(MI.SERVER_FLAG, "1")
    st = c.get("/api/microlive").json()["status"]
    c.post("/api/microlive/enable", json={"phrase": MI.ACK_PHRASE, "notice_version": st["notice_current"]})
    cid = c.post(
        "/api/microlive/connections",
        json={"exchange": "binance", "api_key": "livekey-123456", "api_secret": "sec-123456"},
    ).json()["connection"]["id"]
    r = c.post(
        "/api/studio/strategies", json={"name": "没毕业", "code": FLIP, "securities": ["BTC/USDT"], "timeframe": "1h"}
    )
    slug = r.json()["strategy"]["slug"]
    c.post(f"/api/studio/strategies/{slug}/approve")
    r = c.post("/api/microlive/prepare", json={"slug": slug, "connection_id": cid, "initial_cash": 50})
    assert r.status_code == 403 and "毕业" in r.json()["message"]
    g = _graduated_strategy(c, "毕业了")
    row = next(x for x in c.get("/api/microlive").json()["strategies"] if x["slug"] == g)
    assert row["cap"] == 100 and row["next_cap"] == 300 and not row["can_raise"]
    assert c.post(f"/api/microlive/tiers/{g}/raise").status_code == 400
    # 服务器急停文件
    (get_services().home / MI.KILL_FILE).write_text("stop")
    assert any("急停" in b for b in c.get("/api/microlive").json()["status"]["blockers"])


def test_recheck_wipes_secret_when_withdraw_permission_appears(env, monkeypatch):
    c, _holder, _clock, _step = env
    r = c.post(
        "/api/microlive/connections",
        json={"exchange": "binance", "api_key": "livekey-123456", "api_secret": "sec-123456"},
    )
    assert r.status_code == 200, r.text
    cid = r.json()["connection"]["id"]
    monkeypatch.setattr(
        MI, "permission_probe", lambda ex, creds: {"can_trade": True, "can_withdraw": True, "ip_restricted": True}
    )
    r = c.post(f"/api/microlive/connections/{cid}/test")
    assert r.status_code == 200, r.text
    assert r.json()["connection"]["status"] == "revoked"
    with context.bind(c.headers["X-Workspace-Id"]):
        assert MI.connections(get_services()).get(cid)["secret_ref"] is None


def test_market_banner_reflects_micro_live_switch(env, monkeypatch):
    c, _holder, _clock, _step = env
    b = c.get("/api/market/banner").json()
    assert b["live_enabled"] is False and "默认关闭" in b["mode_label"]

    monkeypatch.setenv(MI.SERVER_FLAG, "1")
    b = c.get("/api/market/banner").json()
    assert b["live_enabled"] is False and "需管理员开启" in b["mode_label"]

    st = c.get("/api/microlive").json()["status"]
    r = c.post("/api/microlive/enable", json={"phrase": MI.ACK_PHRASE, "notice_version": st["notice_current"]})
    assert r.status_code == 200, r.text
    b = c.get("/api/market/banner").json()
    assert b["live_enabled"] is True and "已开启" in b["mode_label"]
