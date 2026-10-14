"""T09 · 告警与通知。

验收标准（docs/VibeCoding任务包.md T09）：
1. 四类事件各一条测试——实时模拟风控暂停、run 失败、只读连接失效、网络判定 UNREACHABLE；
2. 通知内容不含密钥；
3. 同一事件 10 分钟内不重复发送。

本文件另覆盖：跨工作区隔离、API 请求线程不做网络 I/O（入队 + worker 投递）、
投递失败退避重试、渠道凭证加密落盘、非管理员不能配置渠道与规则。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.notify import alerts
from quant_agent.platform.db import now_ms
from quant_agent.platform.errors import AppError

#: 一条看起来像真凭证的 webhook URL（企业微信的 key 就在 query 里）。
WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SUPERSECRETKEY1234567890abcd"
WEBHOOK_SECRET = "SUPERSECRETKEY1234567890abcd"
SERVERCHAN_KEY = "SCT9876543210abcdefghijklmn"
SERVERCHAN_SECRET = "SCT9876543210abcdefghijklmn"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
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


def _ok_transport() -> httpx.MockTransport:
    """假装渠道返回成功。企业微信要求 {"errcode": 0}。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    return httpx.MockTransport(handler)


def _deliver(services, *, transport=None, workspace_id: str | None = None) -> dict:
    svc = alerts.DeliveryService(services.db, services.secrets, services.home, transport=transport)
    return svc.deliver_pending(workspace_id=workspace_id)


def _add_console_channel(services, ws: str, name: str = "控制台") -> dict:
    """建一条 console 渠道：不发网络请求，适合只验证"事件是否被路由到通知"。"""
    return alerts.create_channel(
        services.db,
        alerts.ChannelSecrets(services.secrets, services.home),
        ws,
        user_id="u1",
        kind="console",
        name=name,
    )


def _add_webhook_channel(services, ws: str, url: str = WEBHOOK_URL) -> dict:
    return alerts.create_channel(
        services.db,
        alerts.ChannelSecrets(services.secrets, services.home),
        ws,
        user_id="u1",
        kind="webhook",
        name="企业微信",
        creds={"url": url},
    )


def _add_rule(services, ws: str, event_type: str, *, channel_ids=None, **kw) -> dict:
    return alerts.upsert_rule(
        services.db,
        ws,
        user_id="u1",
        name=f"{event_type} 规则",
        event_type=event_type,
        channel_ids=channel_ids,
        **kw,
    )


def _alert_row(services, ws: str, event_type: str) -> dict:
    row = services.db.fetchone(
        "SELECT * FROM alerts WHERE workspace_id=? AND event_type=? ORDER BY id DESC LIMIT 1", (ws, event_type)
    )
    assert row is not None, f"没有为 {event_type} 生成告警"
    return row


def _all_text(db, table: str) -> str:
    rows = db.fetchall(f"SELECT * FROM {table}")
    return " ".join(str(v) for r in rows for v in r.values())


# ---------------------------------------------------------------------------
# ① 四类事件各一条测试
# ---------------------------------------------------------------------------
def test_run_failed_raises_alert_and_notifies(services, ws):
    """run 失败：事件 → 规则匹配 → 入队 → worker 投递成功。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]])

    outcome = alerts.raise_for_run_failure(
        services.db,
        workspace_id=ws,
        run_id="run-abc",
        run_kind="backtest",
        mode="backtest",
        error="数据缺口：缺少 2024-01-02 的 bar",
        error_code="DATA_INCOMPLETE",
    )
    assert outcome is not None and outcome.status == "pending", outcome
    assert outcome.deliveries == 1

    row = _alert_row(services, ws, alerts.EVENT_RUN_FAILED)
    assert row["subject"] == "run-abc"
    assert "运行失败" in row["title"]
    assert "run-abc" in row["body"]
    assert "仅为研究结果，非投资建议。" in row["body"]
    assert json.loads(row["context"])["error_code"] == "DATA_INCOMPLETE"

    out = _deliver(services, workspace_id=ws)
    assert out["sent"] == 1 and out["failed"] == 0
    assert services.db.scalar("SELECT status FROM alerts WHERE id=?", (row["id"],)) == "sent"


def test_paper_risk_pause_raises_alert_and_notifies(services, ws):
    """实时模拟风控暂停：按 RISK_DRAWDOWN_PAUSE 原因码上报。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_PAPER_RISK_PAUSE, channel_ids=[channel["id"]])

    outcome = alerts.raise_for_paper_risk_pause(
        services.db,
        workspace_id=ws,
        session_id="paper-1",
        reason_code="RISK_DRAWDOWN_PAUSE",
        reason="回撤 21.3% 超过 20%，风控暂停新开仓",
        drawdown="0.213",
        threshold="0.2",
    )
    assert outcome is not None and outcome.status == "pending", outcome

    row = _alert_row(services, ws, alerts.EVENT_PAPER_RISK_PAUSE)
    assert row["subject"] == "paper-1"
    assert "风控暂停" in row["body"]
    assert "RISK_DRAWDOWN_PAUSE" in row["body"]
    ctx = json.loads(row["context"])
    assert ctx["drawdown"] == "0.213" and ctx["threshold"] == "0.2"

    assert _deliver(services, workspace_id=ws)["sent"] == 1


def test_readonly_connection_invalid_raises_alert_and_notifies(services, ws):
    """只读连接失效：凭证被撤销 / 权限不再只读。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_READONLY_CONNECTION_INVALID, channel_ids=[channel["id"]])

    outcome = alerts.raise_for_readonly_connection_invalid(
        services.db,
        workspace_id=ws,
        connection_id="conn-1",
        exchange="binance",
        environment="live",
        reason="交易所拒绝了该 Key（HTTP 401）",
    )
    assert outcome is not None and outcome.status == "pending", outcome

    row = _alert_row(services, ws, alerts.EVENT_READONLY_CONNECTION_INVALID)
    assert row["subject"] == "conn-1"
    assert "只读连接失效" in row["title"]
    assert "binance" in row["body"]
    assert _deliver(services, workspace_id=ws)["sent"] == 1


def test_network_unreachable_raises_alert_and_notifies(services, ws):
    """网络判定变为 UNREACHABLE：行情接口最近 1 小时全部失败。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_NETWORK_UNREACHABLE, channel_ids=[channel["id"]])

    outcome = alerts.raise_for_network_unreachable(
        services.db,
        workspace_id=ws,
        provider="binance",
        endpoint="https://api.binance.com/api/v3/ping?key=SECRETQUERYVALUE123456",
        detail="最近 1 小时 12 次探测全部失败",
    )
    assert outcome is not None and outcome.status == "pending", outcome

    row = _alert_row(services, ws, alerts.EVENT_NETWORK_UNREACHABLE)
    assert row["subject"] == "binance"
    assert "行情接口不可达" in row["title"]
    # endpoint 只保留主机名：带 query 的完整 URL 可能含凭证
    assert json.loads(row["context"])["endpoint"] == "https://api.binance.com/…"
    assert "SECRETQUERYVALUE" not in row["body"]
    assert _deliver(services, workspace_id=ws)["sent"] == 1


# ---------------------------------------------------------------------------
# ② 10 分钟冷却去重
# ---------------------------------------------------------------------------
def test_same_event_is_not_sent_twice_within_10_minutes(services, ws):
    """同一事件 10 分钟内不重复发送：第二次只留痕（suppressed），不再入队投递。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]])
    t0 = now_ms()

    first = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-1", now=t0)
    assert first.status == "pending" and first.deliveries == 1

    second = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-1", now=t0 + 9 * 60 * 1000)
    assert second.status == "suppressed", second
    assert second.deliveries == 0, "被冷却压住的告警不应再产生投递记录"
    assert second.reason and "冷却" in second.reason

    # 留痕仍在，且指向压住它的那条
    suppressed = services.db.fetchone("SELECT * FROM alerts WHERE id=?", (second.alert_id,))
    assert suppressed["status"] == "suppressed"
    assert suppressed["suppressed_by"] == first.alert_id
    assert services.db.scalar("SELECT COUNT(*) FROM alert_deliveries WHERE alert_id=?", (second.alert_id,)) == 0

    # 10 分钟整（窗口边界）之后可以再发
    third = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-1", now=t0 + 10 * 60 * 1000)
    assert third.status == "pending", third

    # 同一规则、不同对象：各自一条冷却窗口，互不影响
    other = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-2", now=t0 + 11 * 60 * 1000)
    assert other.status == "pending", other


def test_suppressed_alerts_do_not_extend_the_cooldown_window(services, ws):
    """窗口的起点永远是"上一条真正投递过的告警"，被压住的留痕不能续期。

    否则"每 9 分钟来一次事件"会让窗口被无限续期，告警永远发不出去。
    """
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]])
    t0 = now_ms()

    assert alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0).status == "pending"
    # 窗口（10 分钟）内：只留痕
    for offset_min in (1, 5, 9):
        got = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + offset_min * 60 * 1000)
        assert got.status == "suppressed", got
    # 距首次已过 10 分钟 → 必须重新发（而不是被那三条留痕续期到 19 分钟）
    again = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + 11 * 60 * 1000)
    assert again.status == "pending", again
    # 新窗口从"这次发送"起算
    assert (
        alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + 20 * 60 * 1000).status
        == "suppressed"
    )


def test_dedupe_scope_rule_ignores_subject(services, ws):
    """dedupe_scope=rule：同一规则下所有对象共用一个冷却窗口。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]], dedupe_scope="rule")
    t0 = now_ms()

    assert alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-1", now=t0).status == "pending"
    got = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-2", now=t0 + 60_000)
    assert got.status == "suppressed", got


def test_cooldown_is_configurable_per_rule(services, ws):
    """冷却窗口由规则决定（默认 600 秒 = 10 分钟）。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]], cooldown_seconds=60)
    t0 = now_ms()
    assert alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0).status == "pending"
    assert (
        alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + 59_000).status
        == "suppressed"
    )
    assert (
        alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + 61_000).status == "pending"
    )


# ---------------------------------------------------------------------------
# ③ 通知内容不含密钥
# ---------------------------------------------------------------------------
def test_webhook_credentials_never_leak(client, services, ws):
    """渠道凭证：接口只回显掩码、数据库与磁盘都没有明文。"""
    r = client.post(
        "/api/notify/channels",
        json={"kind": "webhook", "name": "企业微信", "credentials": {"url": WEBHOOK_URL}},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert WEBHOOK_SECRET not in body
    assert r.json()["channel"]["masked"] == "https://qyapi.weixin.qq.com/…"
    assert "secret_ref" not in r.json()["channel"], "接口不得暴露密文引用"

    # 列表接口同样干净
    listing = client.get("/api/notify/channels")
    assert WEBHOOK_SECRET not in listing.text

    # 数据库里没有任何明文
    assert WEBHOOK_SECRET not in _all_text(services.db, "notify_channels")
    assert WEBHOOK_SECRET not in _all_text(services.db, "alert_rules")
    assert WEBHOOK_SECRET not in _all_text(services.db, "alerts")
    assert WEBHOOK_SECRET not in _all_text(services.db, "alert_deliveries")

    # 磁盘上的凭证文件是密文
    ref = services.db.scalar("SELECT secret_ref FROM notify_channels LIMIT 1")
    enc_file = Path(services.home) / "notify-secrets" / f"{ref}.enc"
    assert enc_file.exists()
    assert WEBHOOK_SECRET not in enc_file.read_text(encoding="utf-8")


def test_alert_body_and_delivery_errors_are_scrubbed(services, ws, monkeypatch):
    """告警正文与投递错误里不得出现凭证。

    投递失败的异常文本经常带目标 URL（httpx 的连接错误就是这样），
    它会进 `alert_deliveries.last_error` 并被界面展示，所以必须脱敏。
    """
    channel = _add_webhook_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]])

    class Exploding:
        def send(self, note):
            raise RuntimeError(f"连接失败：{WEBHOOK_URL}")

    monkeypatch.setattr(alerts, "build_notifier", lambda *a, **k: Exploding())

    outcome = alerts.raise_alert(
        services.db,
        ws,
        alerts.EVENT_RUN_FAILED,
        subject="run-1",
        context_data={"note": f"上游地址 {WEBHOOK_URL}"},
        now=now_ms(),
    )
    assert outcome.status == "pending"
    row = services.db.fetchone("SELECT * FROM alerts WHERE id=?", (outcome.alert_id,))
    assert WEBHOOK_SECRET not in row["body"]
    assert WEBHOOK_SECRET not in row["context"]
    assert "https://qyapi.weixin.qq.com/…" in row["body"]

    out = _deliver(services, workspace_id=ws)
    assert out["retry"] == 1
    err = services.db.scalar("SELECT last_error FROM alert_deliveries WHERE alert_id=?", (outcome.alert_id,))
    assert WEBHOOK_SECRET not in err
    assert "https://qyapi.weixin.qq.com/…" in err


def test_serverchan_key_is_masked(services, ws):
    """Server 酱的 SendKey 直接嵌在 URL path 里，掩码必须保留首尾。"""
    chan = alerts.create_channel(
        services.db,
        alerts.ChannelSecrets(services.secrets, services.home),
        ws,
        user_id="u1",
        kind="serverchan",
        name="Server 酱",
        creds={"send_key": SERVERCHAN_KEY},
    )
    assert chan["masked"] == f"{SERVERCHAN_KEY[:2]}******{SERVERCHAN_KEY[-2:]}"
    assert SERVERCHAN_SECRET not in json.dumps(chan, ensure_ascii=False)
    assert SERVERCHAN_SECRET not in _all_text(services.db, "notify_channels")


def test_scrub_helpers_are_conservative():
    """脱敏函数自身的单元测试：URL、key=value、高熵串都要处理。"""
    assert alerts.mask_url(WEBHOOK_URL) == "https://qyapi.weixin.qq.com/…"
    assert alerts.mask_url("") == ""
    assert alerts.mask_token("ab") == "**"
    assert alerts.mask_token("abcdefghijklmnop") == "ab******op"

    text = alerts.scrub_text(f"目标 {WEBHOOK_URL} 已失效，key={WEBHOOK_SECRET}")
    assert WEBHOOK_SECRET not in text
    assert "qyapi.weixin.qq.com/…" in text

    nested = alerts.scrub({"api_key": "PLAINKEY", "keep": "ok", "list": [{"token": "T"}]})
    assert nested["api_key"] == "***"
    assert nested["keep"] == "ok"
    assert nested["list"][0]["token"] == "***"

    assert alerts.contains_secret(f"x{WEBHOOK_SECRET}y", [WEBHOOK_SECRET]) is True
    assert alerts.contains_secret("clean", [WEBHOOK_SECRET]) is False


def test_audit_details_contain_only_the_mask(client, services, ws):
    """审计 details 里也只能是掩码。"""
    client.post(
        "/api/notify/channels",
        json={"kind": "webhook", "name": "钩子", "credentials": {"url": WEBHOOK_URL}},
    )
    rows = services.db.fetchall("SELECT details FROM audit_log WHERE action='notify.channel_create'")
    assert rows, "创建渠道必须留审计"
    assert WEBHOOK_SECRET not in " ".join(r["details"] for r in rows)
    assert "qyapi.weixin.qq.com/…" in rows[0]["details"]


# ---------------------------------------------------------------------------
# ④ 跨工作区隔离
# ---------------------------------------------------------------------------
def test_cross_workspace_alerts_are_invisible(services, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(create_app()) as alice, TestClient(create_app()) as bob:
        a = login(alice, email="alice@example.com", password="Alice123!", name="Alice")
        b = login(bob, email="bob@example.com", password="Bobby123!", name="Bob")
        aws, bws = a["workspace"]["id"], b["workspace"]["id"]
        assert aws != bws

        chan = alice.post("/api/notify/channels", json={"kind": "console", "name": "A 的控制台"}).json()["channel"]
        rule = alice.post(
            "/api/alert-rules",
            json={"name": "A 的规则", "event_type": alerts.EVENT_RUN_FAILED, "channel_ids": [chan["id"]]},
        ).json()["rule"]
        fired = alice.post("/api/alerts/test", json={"event_type": alerts.EVENT_RUN_FAILED, "subject": "run-A"})
        assert fired.status_code == 200, fired.text
        alert_id = fired.json()["outcome"]["alert_id"]
        assert alert_id

        # Bob 什么都看不到
        assert bob.get("/api/alerts").json()["alerts"] == []
        assert bob.get("/api/notify/channels").json()["channels"] == []
        assert bob.get("/api/alert-rules").json()["rules"] == []
        assert bob.get("/api/alerts/status").json()["unread"] == 0

        # 直接按 id 操作也不行（不区分"不存在"和"不是你的"）
        assert bob.delete(f"/api/notify/channels/{chan['id']}").status_code == 404
        assert bob.delete(f"/api/alert-rules/{rule['id']}").status_code == 404
        assert bob.post("/api/alerts/read", json={"alert_ids": [alert_id]}).json()["marked"] == 0

        # Alice 自己看得到
        assert len(alice.get("/api/alerts").json()["alerts"]) == 1

        # 冷却窗口也按工作区隔离：B 里没有规则，事件只留痕不投递
        outcome = alerts.raise_alert(services.db, bws, alerts.EVENT_RUN_FAILED, subject="run-B")
        assert outcome.status == "skipped"
        assert services.db.scalar("SELECT COUNT(*) FROM alert_deliveries WHERE workspace_id=?", (bws,)) == 0
        assert services.db.scalar("SELECT COUNT(*) FROM alerts WHERE workspace_id=?", (aws,)) == 1


def test_cooldown_does_not_leak_across_workspaces(services):
    """两个工作区各自的规则互相不影响冷却。"""
    chan_a = _add_console_channel(services, "ws-a")
    chan_b = _add_console_channel(services, "ws-b")
    _add_rule(services, "ws-a", alerts.EVENT_RUN_FAILED, channel_ids=[chan_a["id"]])
    _add_rule(services, "ws-b", alerts.EVENT_RUN_FAILED, channel_ids=[chan_b["id"]])
    t0 = now_ms()
    assert alerts.raise_alert(services.db, "ws-a", alerts.EVENT_RUN_FAILED, subject="s", now=t0).status == "pending"
    assert alerts.raise_alert(services.db, "ws-b", alerts.EVENT_RUN_FAILED, subject="s", now=t0).status == "pending"


def test_member_cannot_configure_channels_or_rules(services, monkeypatch):
    """渠道与规则能把工作区数据推到外部，限管理员。成员仍可查看。"""
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(create_app()) as admin:
        login(admin)
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        with TestClient(create_app()) as member:
            login(member, email="m@example.com", password="Member12!", name="成员", invite_code=code)

            r = member.post("/api/notify/channels", json={"kind": "console", "name": "x"})
            assert r.status_code == 403 and r.json()["code"] == "PERMISSION_DENIED"
            r = member.post("/api/alert-rules", json={"name": "x", "event_type": alerts.EVENT_RUN_FAILED})
            assert r.status_code == 403 and r.json()["code"] == "PERMISSION_DENIED"
            r = member.post("/api/alerts/test", json={"event_type": alerts.EVENT_RUN_FAILED})
            assert r.status_code == 403 and r.json()["code"] == "PERMISSION_DENIED"

            assert member.get("/api/notify/channels").status_code == 200
            assert member.get("/api/alert-rules").status_code == 200
            assert member.get("/api/alerts").status_code == 200


# ---------------------------------------------------------------------------
# ⑤ API 请求线程不做网络 I/O（入队 + worker 投递）
# ---------------------------------------------------------------------------
def test_api_enqueues_without_sending_in_the_request_thread(client, services, ws, monkeypatch):
    """发通知是网络 I/O，绝不能阻塞请求线程。

    做法：把 Notifier 换成一个"发送要 2 秒"的替身，然后打 `/api/alerts/test`。
    请求必须立刻返回、并且一次都没调用过 Notifier；真正的发送发生在 worker 侧
    （这里显式调用 `DeliveryService.deliver_pending`，它就是 worker 的周期钩子）。
    """
    calls: list[str] = []

    class SlowNotifier:
        def send(self, note):
            time.sleep(2.0)
            calls.append(note.title)
            return True

    monkeypatch.setattr(alerts, "build_notifier", lambda *a, **k: SlowNotifier())

    chan = client.post("/api/notify/channels", json={"kind": "console", "name": "慢通道"}).json()["channel"]
    client.post(
        "/api/alert-rules",
        json={"name": "全部失败", "event_type": alerts.EVENT_RUN_FAILED, "channel_ids": [chan["id"]]},
    )

    t0 = time.perf_counter()
    r = client.post("/api/alerts/test", json={"event_type": alerts.EVENT_RUN_FAILED, "subject": "run-slow"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    assert elapsed < 1.0, f"请求线程里发生了阻塞（{elapsed:.2f}s）"
    assert calls == [], "请求线程里不得真正发送通知"
    assert r.json()["outcome"]["status"] == "pending"
    assert r.json()["outcome"]["deliveries"] == 1

    # worker 侧投递
    out = _deliver(services, workspace_id=ws)
    assert out["sent"] == 1
    assert len(calls) == 1


def test_delivery_retries_then_fails_with_backoff(services, ws):
    """投递失败按指数退避重试，超过上限后标记 failed。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[channel["id"]])

    outcome = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-1")
    assert outcome.status == "pending"

    svc = alerts.DeliveryService(services.db, services.secrets, services.home)
    # 渠道永远失败（不真的建连），专注于验证退避与状态收敛
    svc._send_one = lambda row: (False, "渠道返回失败")  # type: ignore[method-assign]

    t0 = now_ms()
    first = svc.deliver_pending(workspace_id=ws, now=t0)
    assert first == {"sent": 0, "retry": 1, "failed": 0, "claimed": 1}
    row = services.db.fetchone("SELECT * FROM alert_deliveries WHERE alert_id=?", (outcome.alert_id,))
    assert row["attempts"] == 1 and row["status"] == "pending"
    assert row["next_attempt_at"] == t0 + alerts.BACKOFF_BASE_MS
    # 未到下次尝试时间：不重复发送
    assert svc.deliver_pending(workspace_id=ws, now=t0 + 1000)["claimed"] == 0

    second = svc.deliver_pending(workspace_id=ws, now=t0 + alerts.BACKOFF_BASE_MS)
    assert second["retry"] == 1
    row = services.db.fetchone("SELECT * FROM alert_deliveries WHERE alert_id=?", (outcome.alert_id,))
    assert row["attempts"] == 2 and row["next_attempt_at"] == t0 + alerts.BACKOFF_BASE_MS + 2 * alerts.BACKOFF_BASE_MS

    third = svc.deliver_pending(workspace_id=ws, now=t0 + 10 * alerts.BACKOFF_BASE_MS)
    assert third["failed"] == 1
    row = services.db.fetchone("SELECT * FROM alert_deliveries WHERE alert_id=?", (outcome.alert_id,))
    assert row["status"] == "failed" and row["attempts"] == 3
    assert services.db.scalar("SELECT status FROM alerts WHERE id=?", (outcome.alert_id,)) == "failed"


def test_successful_delivery_marks_alert_sent(services, ws):
    """多渠道路由：两个渠道都成功 → 告警汇总状态为 sent。"""
    a = _add_console_channel(services, ws, name="控制台")
    b = alerts.create_channel(
        services.db,
        alerts.ChannelSecrets(services.secrets, services.home),
        ws,
        user_id="u1",
        kind="webhook",
        name="企业微信",
        creds={"url": WEBHOOK_URL},
    )
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[a["id"], b["id"]])

    outcome = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="run-1")
    assert outcome.deliveries == 2
    out = _deliver(services, transport=_ok_transport(), workspace_id=ws)
    assert out["sent"] == 2
    assert services.db.scalar("SELECT status FROM alerts WHERE id=?", (outcome.alert_id,)) == "sent"
    assert alerts.alert_deliveries_summary(services.db, ws)["sent"] == 2


def test_unreachable_verdict_drives_the_alert_from_real_probe_data(services, ws):
    """端到端：真实探测数据 → netprobe 判定 UNREACHABLE → 告警。

    这条测试补上"判定"这一环：前面那条只验证"上报函数能用"，
    这里验证 `report_unreachable_network` 真的会读 `network_probes` 的判定结果，
    并且只对订阅了该事件的工作区上报。

    判定规则（platform/netprobe.py）：最近 1 小时有样本且全部失败 → UNREACHABLE。
    """
    from quant_agent.platform import netprobe

    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, alerts.EVENT_NETWORK_UNREACHABLE, channel_ids=[channel["id"]])

    # 没有任何探测记录 → 判定为空，不应产生告警
    assert alerts.unreachable_providers(services.db) == []
    assert alerts.report_unreachable_network(services.db) == []

    # 写入两条失败探测（最近 1 小时内），另一个接口成功
    netprobe.record(services.db, "binance", "api.binance.com", False, 120, "rest", "ConnectTimeout")
    netprobe.record(services.db, "binance", "api.binance.com", False, 130, "rest", "ConnectTimeout")
    netprobe.record(services.db, "okx", "www.okx.com", True, 90, "rest", None)

    bad = alerts.unreachable_providers(services.db)
    assert [p["provider"] for p in bad] == ["binance"]

    outcomes = alerts.report_unreachable_network(services.db)
    assert len(outcomes) == 1 and outcomes[0].status == "pending", outcomes

    row = _alert_row(services, ws, alerts.EVENT_NETWORK_UNREACHABLE)
    assert row["subject"] == "binance"
    assert "ConnectTimeout" in row["body"]
    assert "okx" not in row["body"]

    # 第二次调用：同一接口在冷却窗口内 → 只留痕
    again = alerts.report_unreachable_network(services.db)
    assert [o.status for o in again] == ["suppressed"], again

    # 没有订阅者的工作区不会被写进告警
    assert services.db.scalar("SELECT COUNT(*) FROM alerts WHERE workspace_id<>?", (ws,)) == 0


def test_network_alert_not_raised_without_subscribers(services):
    """部署级事件只发给订阅了它的工作区，不给无关工作区刷告警行。"""
    from quant_agent.platform import netprobe

    netprobe.record(services.db, "binance", "api.binance.com", False, 100, "rest", "boom")
    assert alerts.report_unreachable_network(services.db) == []
    assert services.db.scalar("SELECT COUNT(*) FROM alerts") == 0


# ---------------------------------------------------------------------------
# ⑥ 事件入口的其它契约
# ---------------------------------------------------------------------------
def test_unknown_event_type_is_rejected():
    with pytest.raises(AppError) as exc:
        alerts.event_spec("not_a_real_event")
    assert exc.value.code == "INVALID_PARAMETER"


def test_raise_alert_requires_workspace(services):
    with pytest.raises(AppError) as exc:
        alerts.raise_alert(services.db, "", alerts.EVENT_RUN_FAILED)
    assert exc.value.code == "INVALID_PARAMETER"


def test_raise_alert_safe_never_raises(services, monkeypatch):
    """告警是旁路：上报失败绝不能把 run / 同步弄失败。"""
    monkeypatch.setattr(alerts, "raise_alert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert alerts.raise_alert_safe(services.db, "ws-1", alerts.EVENT_RUN_FAILED, subject="x") is None


def test_event_without_rules_is_recorded_but_not_delivered(services, ws):
    """没配规则 ≠ 没发生：事件仍然留痕，便于界面回答"到底触发过没有"。"""
    outcome = alerts.raise_alert(services.db, ws, alerts.EVENT_NETWORK_UNREACHABLE, subject="binance")
    assert outcome.status == "skipped"
    assert outcome.deliveries == 0
    row = services.db.fetchone("SELECT * FROM alerts WHERE id=?", (outcome.alert_id,))
    assert row["event_type"] == alerts.EVENT_NETWORK_UNREACHABLE
    assert row["status"] == "skipped"
    assert row["rule_id"] is None


def test_wildcard_rule_subscribes_to_every_event(services, ws):
    """event_type='*' 是"订阅全部事件"的通配规则。"""
    channel = _add_console_channel(services, ws)
    _add_rule(services, ws, "*", channel_ids=[channel["id"]])
    for kind in (alerts.EVENT_RUN_FAILED, alerts.EVENT_NETWORK_UNREACHABLE, alerts.EVENT_PAPER_RISK_PAUSE):
        got = alerts.raise_alert(services.db, ws, kind, subject=f"s-{kind}", now=now_ms())
        assert got.status == "pending", (kind, got)
    assert len(alerts.workspaces_with_rules(services.db, alerts.EVENT_RUN_FAILED)) == 1


def test_channel_without_credentials_is_rejected(services, ws):
    with pytest.raises(AppError) as exc:
        alerts.create_channel(
            services.db,
            alerts.ChannelSecrets(services.secrets, services.home),
            ws,
            user_id="u1",
            kind="webhook",
            name="空钩子",
            creds={},
        )
    assert exc.value.code == "DATA_INCOMPLETE"


def test_multiple_rules_one_suppressed_one_pending(services, ws):
    """一条事件命中多条规则时，汇总状态以"有没有真的入队"为准。

    通配规则被冷却压住，不能把另一条真正入队的规则也标成"没发"——
    否则界面会显示"已抑制"，而实际上通知已经排队了。
    """
    chan_a = _add_console_channel(services, ws, name="A")
    chan_b = _add_console_channel(services, ws, name="B")
    # 通配规则冷却 30 分钟，专项规则冷却 10 分钟：让两者在不同时刻分别过期
    _add_rule(services, ws, "*", channel_ids=[chan_a["id"]], cooldown_seconds=1800)
    _add_rule(services, ws, alerts.EVENT_RUN_FAILED, channel_ids=[chan_b["id"]], cooldown_seconds=600)
    t0 = now_ms()

    first = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0)
    assert first.status == "pending" and first.deliveries == 2
    assert len(first.rules) == 2

    # 两条规则都在窗口内 → 整体 suppressed
    second = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + 60_000)
    assert second.status == "suppressed" and second.deliveries == 0

    # 11 分钟后：专项规则（600s）已过期 → 入队；通配规则（1800s）仍被压住
    before = int(services.db.scalar("SELECT COALESCE(MAX(id), 0) FROM alerts") or 0)
    third = alerts.raise_alert(services.db, ws, alerts.EVENT_RUN_FAILED, subject="r", now=t0 + 11 * 60 * 1000)
    assert third.status == "pending", third
    assert third.deliveries == 1, "只有过期的那条规则应该入队"
    # 第三次触发新增的两行：一条入队（专项规则），一条留痕（通配规则仍被压住）
    fresh = services.db.fetchall("SELECT status FROM alerts WHERE id>? ORDER BY id", (before,))
    assert sorted(r["status"] for r in fresh) == ["pending", "suppressed"], fresh


def test_rule_referencing_foreign_channel_is_rejected(services):
    """规则不得引用别的工作区的渠道 id。"""
    other = _add_console_channel(services, "ws-other")
    with pytest.raises(AppError) as exc:
        _add_rule(services, "ws-mine", alerts.EVENT_RUN_FAILED, channel_ids=[other["id"]])
    assert exc.value.code == "INVALID_PARAMETER"


def test_invalid_cooldown_is_rejected(services, ws):
    with pytest.raises(AppError) as exc:
        _add_rule(services, ws, alerts.EVENT_RUN_FAILED, cooldown_seconds=99_999_999)
    assert exc.value.code == "INVALID_PARAMETER"


def test_deleting_a_channel_erases_its_secret_file(services, ws):
    """撤销渠道必须把密文一起擦掉：留着密文等于没撤销。"""
    chan = _add_webhook_channel(services, ws)
    ref = services.db.scalar("SELECT secret_ref FROM notify_channels WHERE id=?", (chan["id"],))
    path = Path(services.home) / "notify-secrets" / f"{ref}.enc"
    assert path.exists()

    assert alerts.delete_channel(services.db, alerts.ChannelSecrets(services.secrets, services.home), ws, chan["id"])
    assert not path.exists()
    assert services.db.fetchone("SELECT * FROM notify_channels WHERE id=?", (chan["id"],)) is None


def test_delivery_worker_only_touches_its_own_workspace(services):
    """worker 带 workspace_id 投递时不会碰到别的队列。"""
    for name in ("ws-1", "ws-2"):
        chan = _add_console_channel(services, name)
        _add_rule(services, name, alerts.EVENT_RUN_FAILED, channel_ids=[chan["id"]])
        alerts.raise_alert(services.db, name, alerts.EVENT_RUN_FAILED, subject=f"run-{name}")

    out = _deliver(services, workspace_id="ws-1")
    assert out["sent"] == 1
    assert alerts.pending_deliveries(services.db, "ws-2") == 1


def test_mark_read_and_status_endpoints(client, services, ws):
    client.post("/api/notify/channels", json={"kind": "console", "name": "控制台"})
    client.post(
        "/api/alert-rules",
        json={"name": "全部失败", "event_type": alerts.EVENT_RUN_FAILED, "channel_ids": []},
    )
    r = client.post("/api/alerts/test", json={"event_type": alerts.EVENT_RUN_FAILED, "subject": "run-1"})
    alert_id = r.json()["outcome"]["alert_id"]

    listing = client.get("/api/alerts").json()
    assert listing["unread"] == 1
    assert listing["alerts"][0]["event_label"] == "运行失败"
    assert listing["deliveries"]["pending"] == 1
    assert any(x["event_type"] == alerts.EVENT_RUN_FAILED for x in listing["event_types"])

    status = client.get("/api/alerts/status").json()
    assert status["unread"] == 1 and status["pending"] == 1
    assert status["channels"] == 1 and status["rules"] == 1

    assert client.post("/api/alerts/read", json={"alert_ids": [alert_id]}).json()["marked"] == 1
    assert client.get("/api/alerts?unread_only=true").json()["alerts"] == []

    assert client.post("/api/alerts/read", json={"alert_ids": []}).status_code == 400
    assert client.post("/api/alerts/read", json={"alert_ids": ["x"]}).status_code == 400


def test_preview_endpoint_does_not_persist(client, services, ws):
    r = client.post(
        "/api/alerts/preview",
        json={"event_type": alerts.EVENT_NETWORK_UNREACHABLE, "subject": "binance", "context": {"probe": "failed"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_label"] == "行情接口不可达"
    assert "binance" in body["title"]
    assert "仅为研究结果，非投资建议。" in body["body"]
    assert services.db.scalar("SELECT COUNT(*) FROM alerts") == 0


def test_alerts_endpoints_have_chinese_summaries(client):
    """新端点必须在 api/summaries.py 里有中文摘要（/docs 是交付物的一部分）。"""
    paths = client.get("/openapi.json").json()["paths"]
    checked = 0
    for path, ops in paths.items():
        if "alert" not in path and "notify" not in path:
            continue
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete"):
                continue
            checked += 1
            assert any("一" <= ch <= "鿿" for ch in op.get("summary", "")), f"{method} {path} 缺中文摘要"
    assert checked >= 11, f"告警相关端点只找到 {checked} 个，新增端点可能没注册"
