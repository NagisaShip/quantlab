"""真实记录：CSV 导入、公式注入防护、只读账户连接、归因对照、测试网标识、目标仓位信封。"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.live import accounts as acc
from quant_agent.live.envelope import EnvelopeValidator
from quant_agent.live.imports import ImportError_, parse_csv

STRAT = """
def initialize(ctx):
    run_daily(step, time="close")

def step(ctx):
    s = ctx.securities[0]
    if ctx.bar_index % 40 == 1:
        order_target_percent(s, 0.5)
    elif ctx.bar_index % 40 == 21:
        order_target_percent(s, 0)
"""
FILLS_CSV = (
    "Date(UTC),Pair,Side,Price,Executed,Fee,Fee Coin,Order Id\n"
    "2026-01-02 08:00:00,BTCUSDT,BUY,100.5,0.01,0.001005,USDT,o1\n"
    "2026-01-02 09:00:00,BTCUSDT,SELL,101.25,0.01,0.00101,USDT,o2\n"
)


@pytest.fixture()
def app(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()
    acc.client_factory = acc.default_client_factory


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        login(c)
        yield c


# ---------------------------------------------------------------- 解析
def test_parse_fills_with_alias_mapping_and_decimal():
    r = parse_csv("fills", FILLS_CSV)
    assert r.mapping["qty"] == "Executed" and r.mapping["instrument"] == "Pair"
    assert str(r.rows[0]["price"]) == "100.5" and r.rows[0]["instrument"] == "BTC/USDT" and r.rows[0]["side"] == "buy"
    assert r.rows[0]["ts"] == 1767340800000


def test_formula_injection_rejected():
    bad = FILLS_CSV + '2026-01-02 10:00:00,BTCUSDT,BUY,100,1,0,USDT,"=HYPERLINK(""http://x"")"\n'
    with pytest.raises(ImportError_) as ei:
        parse_csv("fills", bad)
    assert any("公式注入" in e["message"] for e in ei.value.errors)
    # 负数金额不是公式
    flows = "time,coin,amount\n2026-01-01,USDT,1000\n2026-01-05,USDT,-200.5\n"
    assert str(parse_csv("flows", flows).rows[1]["amount"]) == "-200.5"


def test_all_or_nothing_with_row_errors():
    bad = FILLS_CSV + "2026-01-03 00:00:00,BTCUSDT,HOLD,100,1,0,USDT,o3\n"
    with pytest.raises(ImportError_) as ei:
        parse_csv("fills", bad)
    assert ei.value.errors[0]["row"] == 4 and "方向" in ei.value.errors[0]["message"]


def test_csv_export_neutralizes_formulas():
    from quant_agent.platform.csvsafe import to_csv

    out = to_csv([{"a": "=1+1", "b": "-3", "c": "@SUM(A1)"}], ["a", "b", "c"])
    assert "'=1+1" in out and ",-3," in out and "'@SUM" in out


# ---------------------------------------------------------------- 导入与对照
def test_import_status_and_no_fake_live(client):
    st = client.get("/api/live/status").json()
    assert st["has_live_data"] is False
    r = client.post(
        "/api/imports", json={"kind": "fills", "filename": "binance.csv", "content": FILLS_CSV, "exchange": "binance"}
    )
    assert r.status_code == 200, r.text
    imp = r.json()["import"]
    assert imp["rows"] == 2 and imp["report"]["saved"] == 2
    st = client.get("/api/live/status").json()
    assert st["has_live_data"] is True and st["live_fills"] == 2
    # 同一份文件不能重复导入
    r2 = client.post("/api/imports", json={"kind": "fills", "filename": "again.csv", "content": FILLS_CSV})
    assert r2.status_code == 409
    detail = client.get(f"/api/imports/{imp['id']}").json()
    assert detail["rows"][0]["price"] == "100.5"
    r3 = client.post("/api/imports", json={"kind": "fills", "content": "a,b\n1,2\n"})
    assert r3.status_code == 400 and r3.json()["code"] == "INVALID_PARAMETER"
    client.delete(f"/api/imports/{imp['id']}")
    assert client.get("/api/live/status").json()["has_live_data"] is False


def test_comparison_attribution_with_confidence(client):
    bt = client.post(
        "/api/research/backtests",
        json={"code": STRAT, "source": "synthetic", "bars_limit": 400, "warmup_bars": 10, "persist": False},
    ).json()
    sim_run = bt["run_id"]
    sims = client.get(f"/api/runs/{sim_run}/fills").json()["items"]
    assert len(sims) >= 3
    s0, s1 = sims[0], sims[1]
    rows = [
        # 1) 用 client_order_id 精确对应
        f"{s0['ts']},BTC/USDT,{s0['side']},{s0['price']},{s0['amount']},0.01,USDT,{s0['fill_id']},",
        # 2) 无标签，时间晚 10 分钟、价格略差：启发式
        f"{s1['ts'] + 600_000},BTC/USDT,{s1['side']},{float(s1['price']) * 1.001:.4f},{s1['amount']},0.01,USDT,,",
        # 3) 手工交易：无法归因
        f"{s0['ts'] + 5 * 86_400_000},ETH/USDT,buy,2000,0.1,0.2,USDT,,",
    ]
    csv_text = "ts,instrument,side,price,qty,fee,fee_currency,client_order_id,order_id\n" + "\n".join(rows) + "\n"
    imp = client.post("/api/imports", json={"kind": "fills", "content": csv_text}).json()["import"]
    r = client.post("/api/comparisons", json={"sim_run_id": sim_run, "live_run_ids": [imp["run_id"]]})
    assert r.status_code == 200, r.text
    cmp = r.json()["comparison"]
    sm = cmp["summary"]
    assert sm["attributed"] == 2 and sm["unattributed"] == 1
    assert sm["by_method"] == {"client_order_id": 1, "heuristic": 1}
    heur = next(p for p in cmp["pairs"] if p["method"] == "heuristic")
    assert 0.5 <= heur["confidence"] < 1
    assert cmp["unattributed"][0]["instrument"] == "ETH/USDT"
    assert any("无法归因" in n for n in cmp["notes"])
    assert client.get(f"/api/comparisons/{cmp['id']}").json()["comparison"]["summary"]["attributed"] == 2


def test_testnet_import_is_labelled_test(client):
    bt = client.post(
        "/api/research/backtests",
        json={"code": STRAT, "source": "synthetic", "bars_limit": 300, "warmup_bars": 10, "persist": False},
    ).json()
    imp = client.post("/api/imports", json={"kind": "fills", "content": FILLS_CSV, "environment": "testnet"}).json()
    assert client.get("/api/live/status").json()["has_live_data"] is False  # 测试成交不算实盘
    cmp = client.post(
        "/api/comparisons", json={"sim_run_id": bt["run_id"], "live_run_ids": [imp["import"]["run_id"]]}
    ).json()["comparison"]
    assert cmp["labels"]["test"] is True and "测试" in cmp["notes"][0]


def test_imports_are_workspace_isolated(app, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(app) as a, TestClient(app) as b:
        login(a, email="a@example.com", password="Alice123!")
        login(b, email="b@example.com", password="Bobby123!")
        imp = a.post("/api/imports", json={"kind": "fills", "content": FILLS_CSV}).json()["import"]
        assert b.get(f"/api/imports/{imp['id']}").status_code == 404
        assert b.get("/api/live/status").json()["has_live_data"] is False


# ---------------------------------------------------------------- 只读账户
class FakeClient:
    def __init__(self, perms):
        self.perms = perms

    def permissions(self):
        return acc._decide(
            "live",
            self.perms.get("trade", False),
            self.perms.get("withdraw", False),
            self.perms.get("ip", True),
            "123***89",
            10,
            [],
        )

    def balances(self):
        return [{"currency": "USDT", "free": "1000.5", "locked": "0"}]

    def fills(self, symbols, since):
        return [
            {
                "ts": 1767340800000,
                "instrument": "BTC/USDT",
                "side": "buy",
                "qty": "0.01",
                "price": "100",
                "fee": "0.001",
                "fee_currency": "USDT",
                "order_id": "9",
                "fill_id": "77",
                "client_order_id": None,
            }
        ]


def test_trade_enabled_key_is_rejected_and_not_stored(client, tmp_path):
    acc.client_factory = lambda ex, creds, env: FakeClient({"trade": True})
    r = client.post(
        "/api/accounts/connections",
        json={"exchange": "binance", "api_key": "AKIA-trade-key-1234", "api_secret": "secret-value-xyz"},
    )
    assert r.status_code == 403 and "交易权限" in r.json()["message"]
    assert client.get("/api/accounts/connections").json()["items"] == []
    assert not list((get_services().home / "account-secrets").glob("*.enc"))


def test_readonly_connection_lifecycle(client):
    acc.client_factory = lambda ex, creds, env: FakeClient({})
    r = client.post(
        "/api/accounts/connections",
        json={
            "exchange": "binance",
            "label": "主账户",
            "api_key": "AKIA-readonly-key-9999",
            "api_secret": "s3cret-value-abc",
        },
    )
    assert r.status_code == 200, r.text
    conn = r.json()["connection"]
    assert conn["key_mask"] == "AKIA…9999" and conn["status"] == "active" and conn["connection_id"]
    assert "s3cret" not in json.dumps(r.json()) and "secret_ref" not in conn
    files = list((get_services().home / "account-secrets").glob("*.enc"))
    assert len(files) == 1 and "s3cret" not in files[0].read_text()
    # 同步
    s = client.post(f"/api/accounts/connections/{conn['id']}/sync", json={"symbols": ["BTC/USDT"]}).json()
    assert s["run"]["state"] == "COMPLETED" and s["run"]["mode"] == "readonly_live"
    st = client.get("/api/live/status").json()
    assert st["has_live_data"] and st["balances"][0]["free"].startswith("1000.5")
    # 用户事后在交易所开启了交易权限 → 重新检测时拒绝并删除密文
    acc.client_factory = lambda ex, creds, env: FakeClient({"trade": True})
    t = client.post(f"/api/accounts/connections/{conn['id']}/test").json()["connection"]
    assert t["status"] == "rejected" and not list((get_services().home / "account-secrets").glob("*.enc"))
    # 撤销
    rv = client.delete(f"/api/accounts/connections/{conn['id']}").json()
    assert rv["connection"]["status"] == "revoked" and "交易所侧" in rv["note"]
    audit = {i["action"] for i in client.get("/api/workspace/audit").json()["items"]}
    assert {"account.connect", "account.test", "account.revoke"} <= audit
    assert "s3cret" not in json.dumps(client.get("/api/workspace/audit").json())


def test_binance_readonly_client_signs_and_detects_permissions():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/api/v3/time":
            import time

            return httpx.Response(200, json={"serverTime": int(time.time() * 1000)})
        assert "signature" in req.url.params and req.headers["X-MBX-APIKEY"] == "k" * 16
        if req.url.path == "/api/v3/account":
            return httpx.Response(200, json={"uid": 12345678, "canTrade": True, "balances": []})
        if req.url.path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(
                200,
                json={
                    "ipRestrict": True,
                    "enableReading": True,
                    "enableWithdrawals": False,
                    "enableSpotAndMarginTrading": False,
                },
            )
        return httpx.Response(404)

    c = acc.BinanceReadOnly("k" * 16, "s" * 16, transport=httpx.MockTransport(handler))
    rep = c.permissions()
    assert rep.ok and rep.ip_restricted and not rep.can_trade and rep.account_id == "123***78"
    assert all(r.method == "GET" for r in seen)


def test_readonly_clients_have_no_order_path():
    for cls in (acc.BinanceReadOnly, acc.OKXReadOnly):
        names = {n.lower() for n in dir(cls)}
        assert not any(w in n for n in names for w in ("order", "withdraw", "transfer", "cancel"))
    from quant_agent.api.routes_auth import me  # noqa: F401  (live_enabled 恒为 False)


# ---------------------------------------------------------------- 信封
def test_envelope_validator_rejects_expired_duplicate_out_of_order():
    v = EnvelopeValidator(max_weight=0.5)
    base = {
        "schema_version": 1,
        "signal_id": "sig-00000001",
        "source_strategy_version": "sv-1",
        "instrument_id": "BTC/USDT",
        "target_weight": 0.4,
        "created_at": 1000,
        "expires_at": 5000,
    }
    assert v.check(base, now=2000) == (True, "通过")
    assert "重复" in v.check(base, now=2000)[1]
    assert "乱序" in v.check({**base, "signal_id": "sig-00000002", "created_at": 900}, now=2000)[1]
    assert "过期" in v.check({**base, "signal_id": "sig-00000003", "created_at": 1100}, now=6000)[1]
    assert (
        "风险上限"
        in v.check({**base, "signal_id": "sig-00000004", "created_at": 1200, "target_weight": 0.9}, now=2000)[1]
    )


def test_run_envelopes_endpoint(client):
    bt = client.post(
        "/api/research/backtests",
        json={"code": STRAT, "source": "synthetic", "bars_limit": 300, "warmup_bars": 10, "persist": False},
    ).json()
    envs = client.get(f"/api/runs/{bt['run_id']}/envelopes").json()["items"]
    assert envs and all(0 <= e["target_weight"] <= 1 and e["expires_at"] > e["created_at"] for e in envs)


# ---------------------------------------------------------------- 网络实测与事件审计
def test_network_status_pending_until_72h(client):
    from quant_agent.platform import netprobe
    from quant_agent.platform.db import now_ms

    db = get_services().db
    st = client.get("/api/market/network").json()
    assert st["overall"] == "PENDING" and st["label"] == "网络可用性待验证"
    now = now_ms()
    for i in range(80 * 6):  # 80 小时、每 10 分钟一次、全部成功
        db.execute(
            "INSERT INTO network_probes (ts, provider, endpoint, ok, latency_ms, stage) VALUES (?,?,?,?,?,?)",
            (now - i * 600_000, "binance", "x", 1, 50, "rest"),
        )
    assert netprobe.status(db)["providers"][0]["verdict"] == "AVAILABLE"
    netprobe.record(db, "okx", "y", False, None, error="ConnectError")
    s = netprobe.status(db)
    assert {p["provider"]: p["verdict"] for p in s["providers"]} == {"binance": "AVAILABLE", "okx": "UNREACHABLE"}


def test_bars_carry_event_audit_fields(client):
    r = client.get("/api/market/bars?source=synthetic&limit=20&audit=true").json()
    a = r["bars"][-1]["audit"]
    assert a["event_ts"] == r["bars"][-1]["t"] + 3_600_000
    assert a["receive_ts"] > 0 and a["sequence"] > 0 and a["source"] == "synthetic"
    assert a["instrument_id"] == "synthetic:BTC/USDT" and a["raw_ref"].startswith("synthetic:")
    seqs = [b["audit"]["sequence"] for b in r["bars"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert r["last_event_ts"] == a["event_ts"]


def test_market_csv_formula_injection_rejected(client):
    csv_text = "timestamp,open,high,low,close,volume\n1700000000000,1,2,0.5,1.5,10\n1700003600000,=1+1,2,0.5,1.5,10\n"
    r = client.post("/api/arena/datasets/upload", json={"content": csv_text, "symbol": "BTC/USDT", "timeframe": "1h"})
    assert r.status_code == 400 and "公式" in r.json()["message"]
