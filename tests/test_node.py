"""运行节点：失联告警、时钟校准、限频、主循环看门狗、聊天机器人（绑定、二次确认、飞书 / 企业微信回调）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.live import nodeguard
from quant_agent.live.demo_broker import BinanceDemoBroker, BrokerError
from quant_agent.notify import alerts
from quant_agent.notify import bot as B
from quant_agent.platform import watchdog
from quant_agent.platform.db import now_ms
from quant_agent.platform.runs import LEASE_MS, RUNNING
from quant_agent.store import Collections


@pytest.fixture()
def services(tmp_path):
    s = AppServices.build(store_root=str(tmp_path))
    set_services(s)
    yield s
    reset_services()


@pytest.fixture()
def client(services):
    with TestClient(create_app()) as c:
        login(c)
        yield c


@pytest.fixture()
def ws(client) -> str:
    return client.headers["X-Workspace-Id"]


def _paper_run(services, ws, *, beat_ago_ms: int, title="均线策略", execution="paper", exchange="binance"):
    run = services.runs.create(
        workspace_id=ws,
        user_id="u",
        kind="paper",
        mode="paper",
        title=title,
        manifest={"execution": execution, "exchange": exchange},
    )
    services.db.execute(
        "UPDATE runs SET state=?, lease_owner='w1', lease_expires_at=? WHERE id=?",
        (RUNNING, now_ms() - beat_ago_ms + LEASE_MS, run["id"]),
    )
    return run


# ---------------------------------------------------------------- 失联告警
def test_heartbeat_loss_alerts_within_three_minutes_and_delivers_itself(services, ws):
    ch = alerts.create_channel(
        services.db,
        alerts.ChannelSecrets(services.secrets, services.home),
        ws,
        user_id="u1",
        kind="console",
        name="控制台",
    )
    alerts.upsert_rule(
        services.db, ws, user_id="u1", name="失联", event_type=alerts.EVENT_NODE_HEARTBEAT_LOST, channel_ids=[ch["id"]]
    )
    fresh = _paper_run(services, ws, beat_ago_ms=20_000, title="正常的")
    gone = _paper_run(services, ws, beat_ago_ms=watchdog.STALE_MS + 1_000, title="失联的")
    out = watchdog.check(services)
    assert [r["id"] for r in out] == [gone["id"]]
    row = services.db.fetchone("SELECT * FROM alerts WHERE event_type=?", (alerts.EVENT_NODE_HEARTBEAT_LOST,))
    assert row and "失联的" in row["title"]
    # worker 挂了也能发出去：看门狗自己投递
    d = services.db.fetchone("SELECT status FROM alert_deliveries WHERE alert_id=?", (row["id"],))
    assert d["status"] == "sent"
    # 时间预算：失联判定 2 分钟 + 检查间隔 30 秒 < 3 分钟
    assert watchdog.STALE_MS + watchdog.CHECK_SECONDS * 1000 < 180_000
    assert fresh["id"] not in [r["id"] for r in out]
    # 同一个实验冷却期内不重复发
    watchdog.check(services)
    assert len(services.db.fetchall("SELECT id FROM alert_deliveries")) == 1


def test_recovery_budget_is_under_two_minutes():
    # 租约过期（60 秒）后由重启的 worker 认领；worker 每轮先回收过期租约再认领
    assert LEASE_MS <= 60_000
    assert LEASE_MS / 1000 + 30 < 120  # 加上容器重启与主循环一轮的余量


def test_hang_guard_exits_when_main_loop_stalls():
    hit = []
    g = watchdog.HangGuard(0.3, on_hang=lambda: hit.append(1))
    g.start()
    time.sleep(0.6)
    assert hit == [1]
    ok = []
    g2 = watchdog.HangGuard(0.3, on_hang=lambda: ok.append(1))
    g2.start()
    for _ in range(6):
        g2.tick()
        time.sleep(0.1)
    g2.stop.set()
    assert ok == []


# ---------------------------------------------------------------- 时钟
def _time_transport(skew_ms: int):
    def handler(req: httpx.Request) -> httpx.Response:
        server = int(time.time() * 1000) + skew_ms
        if "binance" in req.url.host:
            return httpx.Response(200, json={"serverTime": server})
        if "okx" in req.url.host:
            return httpx.Response(200, json={"code": "0", "data": [{"ts": str(server)}]})
        return httpx.Response(200, json={"retCode": 0, "result": {"timeNano": str(server * 1_000_000)}})

    return httpx.MockTransport(handler)


def test_clock_offset_measured_and_used_for_signing():
    clk = nodeguard.ClockSync()
    for ex in ("binance", "okx", "bybit"):
        off = clk.measure(ex, transport=_time_transport(3_000))
        assert abs(off - 3_000) < 200
    assert all(s["level"] == "warn" for s in clk.status())
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(int(parse_qs(urlparse(str(req.url)).query)["timestamp"][0]))
        return httpx.Response(200, json={"balances": []})

    old = nodeguard.CLOCK.offsets.get("binance")
    nodeguard.CLOCK.offsets["binance"] = 7_000
    try:
        BinanceDemoBroker("k", "s", transport=httpx.MockTransport(handler)).balances()
    finally:
        nodeguard.CLOCK.offsets.pop("binance", None) if old is None else nodeguard.CLOCK.offsets.update(binance=old)
    assert abs(seen[0] - (time.time() * 1000 + 7_000)) < 1_000


def test_clock_drift_alerts_when_demo_session_running(services, ws):
    _paper_run(services, ws, beat_ago_ms=1_000, execution="demo", exchange="okx")
    try:
        status = watchdog.check_clock(services, transport=_time_transport(-9_000))
        assert next(s for s in status if s["exchange"] == "okx")["level"] == "bad"
        assert services.db.fetchone("SELECT id FROM alerts WHERE event_type=?", (alerts.EVENT_CLOCK_DRIFT,))
    finally:
        nodeguard.CLOCK.offsets.pop("okx", None)


# ---------------------------------------------------------------- 限频
def test_token_bucket_waits_then_refuses_after_429():
    b = nodeguard.Bucket(2.0, burst=2)
    slept = []
    assert b.acquire(sleep=slept.append) == 0 and b.acquire(sleep=slept.append) == 0
    real_sleep = time.sleep
    assert b.acquire(sleep=lambda s: (slept.append(s), real_sleep(s))) > 0 and slept
    b.penalize(30)
    with pytest.raises(nodeguard.RateLimited):
        b.acquire(max_wait=1, sleep=lambda s: None)


def test_broker_429_pauses_bucket_and_does_not_send_more(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "20"}, json={"msg": "Too many requests"})

    monkeypatch.setitem(nodeguard._BUCKETS, "binance", nodeguard.Bucket(100))
    br = BinanceDemoBroker("k", "s", transport=httpx.MockTransport(handler))
    with pytest.raises(BrokerError, match="限频"):
        br.balances()
    with pytest.raises(BrokerError, match="本地限频"):
        br.balances()
    assert len(calls) == 1


# ---------------------------------------------------------------- 机器人
def _session_doc(services, run, *, name="均线策略", ret=0.05):
    services.store.for_workspace(run["workspace_id"]).upsert(
        Collections.PAPER_SESSIONS,
        {
            "id": run["id"],
            "strategy_name": name,
            "execution": "paper",
            "equity_points": [{"t": 1, "v": 10_000}, {"t": 2, "v": 10_000 * (1 + ret)}],
            "portfolio": {"positions": {"BTC/USDT": {"amount": "0.1"}}, "total_value": 10_500},
            "fills": [
                {"ts": 1_700_000_000_000, "side": "buy", "security": "BTC/USDT", "price": "50000", "reason": "breakout"}
            ],
            "events": [{"level": "warning", "message": "滑点偏大"}],
        },
    )


def test_bot_requires_binding_then_status_why_and_confirmed_close(client, services, ws, monkeypatch):
    run = _paper_run(services, ws, beat_ago_ms=5_000)
    _session_doc(services, run)
    bot = B.BotService(services)
    assert "还没绑定" in bot.handle("telegram", "42", "状态")
    code = client.post("/api/bot/bind-code").json()["code"]
    assert "绑定好了" in bot.handle("telegram", "42", f"绑定 {code}")
    assert "已过期" in bot.handle("telegram", "43", f"绑定 {code}")  # 一次性
    st = bot.handle("telegram", "42", "status")
    assert "1. 均线策略" in st and "+5.00%" in st and "BTC/USDT" in st
    why = bot.handle("telegram", "42", "为什么 1")
    assert "breakout" in why and "滑点偏大" in why

    called = []
    import quant_agent.api.routes_studio as RS

    monkeypatch.setattr(
        RS, "liquidate_paper", lambda sid: called.append(("close", sid)) or {"session": {"strategy_name": "均线策略"}}
    )
    ask = bot.handle("telegram", "42", "平仓 全部")
    assert "确认" in ask and not called
    real = ask.split("确认 ")[1][:4]
    assert "不对" in bot.handle("telegram", "42", "确认 0000")
    done = bot.handle("telegram", "42", f"确认 {real}")
    assert called == [("close", run["id"])] and "平仓" in done
    assert "没有待确认" in bot.handle("telegram", "42", f"确认 {real}")  # 只能用一次


def test_bot_confirmation_expires_and_reviewer_cannot_stop(client, services, ws):
    run = _paper_run(services, ws, beat_ago_ms=5_000)
    _session_doc(services, run)
    t = [1_000_000]
    bot = B.BotService(services, now=lambda: t[0])
    code = bot.new_bind_code(ws, services.db.fetchone("SELECT user_id FROM memberships LIMIT 1")["user_id"])["code"]
    bot.handle("feishu", "c1", f"绑定 {code}")
    ask = bot.handle("feishu", "c1", "停止 1")
    t[0] += B.CONFIRM_MS + 1
    assert "超过 2 分钟" in bot.handle("feishu", "c1", "确认 " + ask.split("确认 ")[1][:4])
    services.db.execute("UPDATE memberships SET role='reviewer'")
    assert "审阅者" in bot.handle("feishu", "c1", "停止 1")


def _feishu_encrypt(key: str, payload: dict) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    raw = json.dumps(payload).encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    iv = os.urandom(16)
    enc = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
    return base64.b64encode(iv + enc.update(raw) + enc.finalize()).decode()


def test_feishu_callback_verifies_token_decrypts_and_replies(client, services, ws):
    b = client.post(
        "/api/bot",
        json={
            "kind": "feishu",
            "creds": {"app_id": "a", "app_secret": "s", "verification_token": "vt", "encrypt_key": "ek"},
        },
    ).json()["bot"]
    anon = TestClient(client.app)
    hook = b["hook_path"]
    r = anon.post(hook, json={"type": "url_verification", "token": "vt", "challenge": "abc"})
    assert r.status_code == 200 and r.json() == {"challenge": "abc"}
    assert anon.post(hook, json={"type": "url_verification", "token": "bad", "challenge": "x"}).status_code == 403
    sent = []

    def handler(req):
        if req.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"tenant_access_token": "t", "expire": 7200})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"code": 0})

    fs = B.Feishu(
        {"app_id": "a", "app_secret": "s", "verification_token": "vt", "encrypt_key": "ek"},
        transport=httpx.MockTransport(handler),
    )
    ev = {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "token": "vt"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou"}},
            "message": {"chat_id": "oc1", "message_type": "text", "content": json.dumps({"text": "@机器人 帮助"})},
        },
    }
    fs.callback(B.BotService(services), {"encrypt": _feishu_encrypt("ek", ev)})
    assert sent and sent[0]["receive_id"] == "oc1" and "还没绑定" in json.loads(sent[0]["content"])["text"]


def test_wecom_url_verify_and_message_roundtrip(services):
    cfg = {
        "corp_id": "wwcorp",
        "agent_id": "1000002",
        "secret": "s",
        "token": "tok",
        "aes_key": base64.b64encode(os.urandom(32)).decode().rstrip("="),
    }
    sent = []

    def handler(req):
        if req.url.path.endswith("gettoken"):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 7200})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"errcode": 0})

    wc = B.WeCom(cfg, transport=httpx.MockTransport(handler))
    echo = wc.encrypt("hello-echo")
    assert wc.verify_url(wc._sig("1", "n", echo), "1", "n", echo) == "hello-echo"
    with pytest.raises(PermissionError):
        wc.verify_url("bad", "1", "n", echo)
    inner = (
        "<xml><FromUserName><![CDATA[zhangsan]]></FromUserName><MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[状态]]></Content></xml>"
    )
    enc = wc.encrypt(inner)
    wc.callback(
        B.BotService(services), wc._sig("2", "m", enc), "2", "m", f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
    )
    assert sent[0]["touser"] == "zhangsan" and "还没绑定" in sent[0]["text"]["content"]


def test_telegram_long_poll_replies(services):
    out = []

    def handler(req):
        if req.url.path.endswith("getUpdates"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [{"update_id": 5, "message": {"chat": {"id": 9}, "from": {"id": 1}, "text": "help"}}],
                },
            )
        out.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    tg = B.Telegram("123:abc", transport=httpx.MockTransport(handler))
    assert tg.poll(B.BotService(services)) == 1 and tg.offset == 6
    assert out[0]["chat_id"] == "9"


def test_board_and_health_endpoints(client, services, ws):
    run = _paper_run(services, ws, beat_ago_ms=5_000)
    _session_doc(services, run)
    board = client.get("/api/node/board").json()
    assert (
        board["sessions"][0]["strategy"] == "均线策略"
        and board["sessions"][0]["positions"][0]["security"] == "BTC/USDT"
    )
    h = client.get("/api/node/health").json()
    assert "workers_alive" in h and "clock" in h
    bots = client.get("/api/bot").json()
    assert set(bots["kinds"]) == {"telegram", "feishu", "wecom"}
