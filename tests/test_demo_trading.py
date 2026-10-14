"""交易所模拟盘执行链路：下单客户端、OMS、内核外部成交、连接管理、策略包、实时模拟端到端。

不连任何交易所：HTTP 用 httpx.MockTransport，OMS 和实时模拟用内存里的假模拟盘。
"""

from __future__ import annotations

import io
import json
import zipfile
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api import tasks
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.data.quality import TIMEFRAME_MS
from quant_agent.engine.live import LiveRunner, clock_synthetic_bars
from quant_agent.live import demo_accounts
from quant_agent.live import demo_broker as DB
from quant_agent.live.oms import DemoOMS, OmsLimits
from quant_agent.sdk import load_source
from quant_agent.sdk.runtimes import RuntimeCost

FLIP = """
def initialize(ctx):
    g.n = 0

def handle_data(ctx):
    g.n += 1
    s = ctx.securities[0]
    order_target_percent(s, 0.5 if g.n % 2 else 0.0)
"""


# ---------------------------------------------------------------------------
# 假模拟盘
# ---------------------------------------------------------------------------
class FakeBroker(DB.DemoBroker):
    exchange, env = "binance", "demo"

    def __init__(self, usdt: str = "50000", price: float = 100.0) -> None:
        self.bal = {"USDT": Decimal(usdt)}
        self.price = Decimal(str(price))
        self.orders: dict[str, DB.OrderReport] = {}
        self.placed: list[tuple[str, str, Decimal, str]] = []
        self.fail_next: str = ""  # "unknown" | "error"
        self.lost = False  # unknown 时订单是否真的没到交易所
        self.closed = False

    def rules(self, symbol: str) -> DB.Rules:
        return DB.Rules(step=Decimal("0.0001"), min_qty=Decimal("0.0001"), min_notional=Decimal("5"))

    def balances(self) -> dict[str, Decimal]:
        return dict(self.bal)

    def place_market(self, symbol, side, qty, client_order_id):
        mode, self.fail_next = self.fail_next, ""
        if mode == "error":
            raise DB.BrokerError("余额不足")
        base = symbol.split("/")[0]
        fee = (qty * self.price * Decimal("0.001")).quantize(Decimal("0.00000001"))
        rep = DB.OrderReport(client_order_id, "filled", qty, self.price, fee, "USDT", f"x{len(self.orders)}", ts=1)
        if not (mode == "unknown" and self.lost):
            self.placed.append((symbol, side, qty, client_order_id))
            sign = 1 if side == "buy" else -1
            self.bal[base] = self.bal.get(base, Decimal(0)) + sign * qty
            self.bal["USDT"] -= sign * qty * self.price + fee
            self.orders[client_order_id] = rep
        if mode == "unknown":
            raise DB.BrokerUnknown("timeout")
        return rep

    def query(self, symbol, client_order_id):
        if client_order_id in self.orders:
            return self.orders[client_order_id]
        return DB.OrderReport(client_order_id, "rejected", message="交易所没有这笔订单（未送达）")

    def close(self):
        self.closed = True


def _intent(side="buy", qty="1", price="100", coid="q1"):
    return {
        "client_order_id": coid,
        "security": "BTC/USDT",
        "side": side,
        "qty": qty,
        "ref_price": price,
        "signal_id": "s1",
        "reason": "test",
    }


# ---------------------------------------------------------------------------
# 下单客户端：只访问模拟环境，签名正确
# ---------------------------------------------------------------------------
def test_binance_demo_broker_signs_and_parses():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        q = parse_qs(urlparse(str(req.url)).query)
        if req.url.path == "/api/v3/order" and req.method == "POST":
            assert req.headers["X-MBX-APIKEY"] == "k"
            assert q["type"] == ["MARKET"] and q["newClientOrderId"] == ["q1"] and "signature" in q
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "q1",
                    "orderId": 7,
                    "status": "FILLED",
                    "executedQty": "0.5",
                    "cummulativeQuoteQty": "50.5",
                    "transactTime": 123,
                    "fills": [{"commission": "0.0005", "commissionAsset": "BTC"}],
                },
            )
        if req.url.path == "/api/v3/order":
            return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
        if req.url.path == "/api/v3/account":
            return httpx.Response(200, json={"balances": [{"asset": "USDT", "free": "10", "locked": "1"}]})
        return httpx.Response(404, json={})

    b = DB.make_broker("binance", "demo", {"api_key": "k", "api_secret": "s"}, transport=httpx.MockTransport(handler))
    rep = b.place_market("BTC/USDT", "buy", Decimal("0.5"), "q1")
    assert rep.status == "filled" and rep.avg_price == Decimal("101") and rep.fee_currency == "BTC"
    assert b.query("BTC/USDT", "q2").status == "rejected"
    assert b.balances() == {"USDT": Decimal("11")}
    assert all(r.url.host == "demo-api.binance.com" for r in seen)


def test_okx_demo_broker_always_sends_simulated_header():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/api/v5/trade/order" and req.method == "POST":
            body = json.loads(req.content)
            assert body["clOrdId"] == "q1" and body["tgtCcy"] == "base_ccy"
            return httpx.Response(200, json={"code": "0", "data": [{"ordId": "9", "sCode": "0"}]})
        return httpx.Response(
            200,
            json={
                "code": "0",
                "data": [
                    {
                        "state": "filled",
                        "accFillSz": "0.5",
                        "avgPx": "100",
                        "fee": "-0.05",
                        "feeCcy": "USDT",
                        "ordId": "9",
                    }
                ],
            },
        )

    b = DB.make_broker(
        "okx", "demo", {"api_key": "k", "api_secret": "s", "passphrase": "p"}, transport=httpx.MockTransport(handler)
    )
    assert b.place_market("BTC/USDT", "buy", Decimal("0.5"), "q1").status == "new"
    rep = b.query("BTC/USDT", "q1")
    assert rep.status == "filled" and rep.fee == Decimal("0.05")
    assert all(r.headers["x-simulated-trading"] == "1" for r in seen)


def test_no_live_environment_exists():
    with pytest.raises(ValueError):
        DB.make_broker("binance", "live", {"api_key": "okx-key-123", "api_secret": "secret-123"})
    with pytest.raises(ValueError):
        DB.make_broker("okx", "live", {"api_key": "okx-key-123", "api_secret": "secret-123"})
    assert {meta["base"] for meta in DB.ENVIRONMENTS.values()} == {
        "https://demo-api.binance.com",
        "https://testnet.binance.vision",
        "https://www.okx.com",
        "https://api-demo.bybit.com",
        "https://api-testnet.bybit.com",
    }


# ---------------------------------------------------------------------------
# OMS
# ---------------------------------------------------------------------------
def test_oms_fills_and_records_slippage():
    b = FakeBroker(price=101)
    oms = DemoOMS(b, OmsLimits(), poll_seconds=0)
    rep = oms.submit(_intent())
    assert rep["status"] == "filled" and rep["avg_price"] == "101"
    assert oms.state.orders[-1]["slippage_bps"] == 100.0  # 买贵了 1%
    assert oms.state.stats["sent"] == 1


def test_oms_pretrade_limits_and_halt():
    b = FakeBroker()
    oms = DemoOMS(b, OmsLimits(max_order_notional=500, max_orders_per_minute=2), poll_seconds=0)
    assert oms.submit(_intent(qty="10"))["status"] == "rejected"  # 1000 > 500
    assert not b.placed
    oms.halt("测试熔断")
    assert "熔断" in oms.submit(_intent(coid="q2"))["message"]  # 熔断只拦买单
    b.bal["BTC"] = Decimal("5")
    assert oms.submit(_intent(side="sell", coid="q3"))["status"] == "filled"
    oms.resume()
    assert oms.submit(_intent(coid="q4"))["status"] == "filled"
    assert "上限" in oms.submit(_intent(coid="q5"))["message"]  # 每分钟 2 笔


def test_oms_unknown_queries_instead_of_resending():
    b = FakeBroker()
    oms = DemoOMS(b, OmsLimits(), poll_seconds=0)
    b.fail_next = "unknown"  # 超时，但订单其实到了交易所
    rep = oms.submit(_intent(coid="qa"))
    assert rep["status"] == "filled" and len(b.placed) == 1
    b.fail_next, b.lost = "unknown", True  # 超时且订单没到：查不到 → 作废，不重下
    rep = oms.submit(_intent(coid="qb"))
    assert rep["status"] == "rejected" and len(b.placed) == 1


def test_oms_daily_loss_halt_and_reconcile():
    b = FakeBroker()
    oms = DemoOMS(b, OmsLimits(max_daily_loss=0.05), initial_cash=10_000, poll_seconds=0)
    oms.snapshot_baseline()
    assert oms.observe_equity(10_000) is None
    assert "熔断" in oms.observe_equity(9_400)
    assert oms.state.halted
    rep = oms.submit({**_intent(), "qty": "1"})
    assert rep["status"] == "rejected"
    oms.resume()
    oms.submit(_intent(coid="qr"))
    # 账本：花了 100 + 0.1 手续费，持有 1 BTC
    rec = oms.reconcile(Decimal("9899.9"), {"BTC/USDT": Decimal("1")}, quote="USDT")
    assert rec["ok"], rec
    b.bal["BTC"] += Decimal("0.5")  # 有人在交易所手动买了
    assert not oms.reconcile(Decimal("9899.9"), {"BTC/USDT": Decimal("1")}, quote="USDT")["ok"]


# ---------------------------------------------------------------------------
# 运行内核：外部成交模式
# ---------------------------------------------------------------------------
def _hist(n=120, start=1_700_000_000_000, tf="1h"):
    step = TIMEFRAME_MS[tf]
    return clock_synthetic_bars("BTC/USDT", tf, start, start + n * step)


def test_runtime_external_mode_books_exchange_reports():
    tf = "1h"
    bars = _hist()
    warm, rest = bars[:100], bars[100:]
    live = LiveRunner()
    module = load_source(FLIP, name="flip")
    live.start(
        "s1",
        module,
        {"BTC/USDT": warm},
        timeframe=tf,
        initial_cash=10_000,
        cost=RuntimeCost(taker_fee=0.001),
        started_at=0,
        fill_mode="external",
    )
    # 交易所模拟盘：收盘决策后立刻送单，参考价 = 这根收盘价（不等下一根开盘）
    out = live.step("s1", {"BTC/USDT": rest[:1]})
    assert out["fills"] == [] and len(out["outbox"]) == 1 and out["awaiting"] == 1
    intent = out["outbox"][0]
    assert Decimal(intent["market_price"]) == Decimal(str(rest[0].close))
    assert intent["side"] == "buy" and intent["client_order_id"].startswith("q")
    # 同一笔信号的客户端订单号是确定的：重启后查单用
    assert live.awaiting("s1")[0]["client_order_id"] == intent["client_order_id"]
    b = FakeBroker(price=float(intent["ref_price"]))
    oms = DemoOMS(b, OmsLimits(max_order_notional=0), poll_seconds=0)
    reports = oms.process(out["outbox"], [])
    res = live.apply_reports("s1", reports)
    assert len(res["fills"]) == 1 and res["awaiting"] == 0
    held = Decimal(res["ledger"]["holdings"]["BTC/USDT"])
    assert held == Decimal(intent["qty"]).quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
    # 重复回报只记一次
    assert live.apply_reports("s1", reports)["fills"] == []
    # 拒单：不记账，记一条 EXCHANGE_REJECTED
    out = live.step("s1", {"BTC/USDT": rest[1:2]})
    sell = out["outbox"][0]
    res = live.apply_reports(
        "s1",
        [{"client_order_id": sell["client_order_id"], "status": "rejected", "filled_qty": "0", "message": "测试拒单"}],
    )
    assert res["fills"] == [] and res["rejected"][0]["code"] == "EXCHANGE_REJECTED"


# ---------------------------------------------------------------------------
# API：连接管理、策略包、实时模拟端到端
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def fake():
    return {"broker": None}


@pytest.fixture(scope="module")
def client(tmp_path_factory, fake):
    root = tmp_path_factory.mktemp("demo-store")
    services = AppServices.build(store_root=str(root))
    set_services(services)
    mp = pytest.MonkeyPatch()

    def factory(exchange, env, creds):
        if creds["api_key"] == "bad-key-000":
            raise_broker = FakeBroker()
            raise_broker.balances = lambda: (_ for _ in ()).throw(DB.BrokerError("Invalid API-key"))
            return raise_broker
        fake["broker"] = fake["broker"] or FakeBroker()
        return fake["broker"]

    mp.setattr(demo_accounts, "broker_factory", factory)
    with TestClient(create_app()) as c:
        login(c)
        yield c
    mp.undo()
    reset_services()


def test_demo_connection_lifecycle(client, tmp_path):
    r = client.get("/api/demo/environments")
    assert {(e["exchange"], e["env"]) for e in r.json()["environments"]} >= {("binance", "demo"), ("okx", "demo")}
    r = client.post(
        "/api/demo/connections",
        json={"exchange": "okx", "env": "demo", "api_key": "okx-key-123", "api_secret": "secret-123"},
    )
    assert r.status_code == 400 and "passphrase" in r.json()["message"]
    r = client.post(
        "/api/demo/connections",
        json={"exchange": "binance", "env": "demo", "api_key": "bad-key-000", "api_secret": "secret-123"},
    )
    assert r.status_code == 403 and "模拟盘" in r.json()["message"]
    r = client.post(
        "/api/demo/connections",
        json={"exchange": "binance", "env": "demo", "api_key": "demokey-123456", "api_secret": "secret-xyz"},
    )
    assert r.status_code == 200, r.text
    conn = r.json()["connection"]
    assert conn["status"] == "active" and conn["key_mask"] == "demo…3456"
    assert "secret-xyz" not in r.text and "secret_ref" not in conn
    listed = client.get("/api/demo/connections").text
    assert "secret-xyz" not in listed and "demokey-123456" not in listed
    assert client.post(f"/api/demo/connections/{conn['id']}/test").json()["connection"]["status"] == "active"
    r = client.delete(f"/api/demo/connections/{conn['id']}")
    assert r.json()["connection"]["status"] == "revoked"
    assert client.post(f"/api/demo/connections/{conn['id']}/test").json()["connection"]["status"] == "revoked"


def _strategy(client, name, code=FLIP, approve=True):
    r = client.post(
        "/api/studio/strategies", json={"name": name, "code": code, "securities": ["BTC/USDT"], "timeframe": "1h"}
    )
    assert r.status_code == 200, r.text
    slug = r.json()["strategy"]["slug"]
    if approve:
        client.post(f"/api/studio/strategies/{slug}/approve")
    return slug


def _retamper(data: bytes) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            body = src.read(info.filename)
            if info.filename == "strategy.py":
                body = body.replace(b"0.5 if", b"0.4 if")
            z.writestr(info.filename, body)
    return out.getvalue()


def test_package_export_inspect_import(client):
    slug = _strategy(client, "打包策略")
    assert client.get(f"/api/studio/strategies/{slug}/spec").json()["draft"] is True
    client.put(f"/api/studio/strategies/{slug}/spec", json={"spec": "# 打包策略\n\n交替买卖。"})
    r = client.get(f"/api/studio/strategies/{slug}/package")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    data = r.content
    rep = client.post("/api/studio/packages/inspect", content=data).json()["report"]
    assert rep["importable"] and rep["golden_match"] and rep["demo_ready"]
    r = client.post("/api/studio/packages/import?name=导入的策略", content=data)
    assert r.status_code == 200, r.text
    imported = r.json()["strategy"]
    assert imported["slug"] != slug and imported["human_approved"] is False
    assert imported["package"]["demo_ready"] is True
    assert "交替买卖" in client.get(f"/api/studio/strategies/{imported['slug']}/spec").json()["spec"]
    # 改过代码的包：能导入，但不能上模拟盘
    rep = client.post("/api/studio/packages/inspect", content=_retamper(data)).json()["report"]
    assert rep["importable"] and not rep["code_match"] and not rep["demo_ready"]
    # 不是 zip
    assert client.post("/api/studio/packages/inspect", content=b"hello").status_code == 400


def test_demo_paper_end_to_end(client, fake, monkeypatch):
    """真实行情（这里用确定性行情代替）+ 假模拟盘：下单、回报记账、熔断、一键停止。"""
    tf, step = "1h", TIMEFRAME_MS["1h"]
    clock = {"end": 1_700_000_000_000 + 400 * step}

    def bars(services, session, count):
        out = {}
        for sec in session.securities or [session.symbol]:
            got = clock_synthetic_bars(sec, tf, clock["end"] - count * step, clock["end"])
            out[sec] = got
            if fake["broker"] is not None and got:
                fake["broker"].price = Decimal(str(got[-1].close))
        return out

    monkeypatch.setattr(tasks, "latest_bars", bars)
    from quant_agent.api.deps import get_services

    monkeypatch.setattr(get_services().market, "ticker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    fake["broker"] = FakeBroker(usdt="20000")
    cid = client.post(
        "/api/demo/connections",
        json={"exchange": "binance", "env": "demo", "api_key": "demokey-abcdef", "api_secret": "secret-123"},
    ).json()["connection"]["id"]
    slug = _strategy(client, "模拟盘策略")
    base = {"slug": slug, "source": "exchange", "exchange": "binance", "initial_cash": 10_000}
    # 启动前检查
    r = client.post("/api/studio/paper/start", json={**base, "execution": "demo"})
    assert r.status_code == 400 and "模拟盘连接" in r.json()["message"]
    r = client.post(
        "/api/studio/paper/start", json={**base, "exchange": "okx", "execution": "demo", "demo_connection_id": cid}
    )
    assert r.status_code == 400 and "同一家交易所" in r.json()["message"]
    r = client.post(
        "/api/studio/paper/start",
        json={**base, "execution": "demo", "demo_connection_id": cid, "max_order_notional": 100_000},
    )
    assert r.status_code == 200, r.text
    s = r.json()["session"]
    sid = s["id"]
    assert s["execution"] == "demo" and s["demo"]["connection"]["key_mask"] == "demo…cdef"
    for _ in range(3):
        clock["end"] += step
        s = client.post(f"/api/studio/paper/{sid}/tick").json()["session"]
    b = fake["broker"]
    assert len(b.placed) >= 2, s.get("events")
    assert s["demo"]["stats"]["sent"] == len(b.placed)
    assert all(o["status"] == "filled" for o in s["demo"]["orders"])
    assert len(s["fills"]) == len(b.placed)
    # 内核账本和假模拟盘账户一致
    base_bal = Decimal("20000")
    cash = Decimal(str(s["portfolio"]["available_cash"]))
    assert abs((b.bal["USDT"] - base_bal) - (cash - Decimal(10_000))) < Decimal("0.01")

    term = client.get(f"/api/studio/paper/{sid}/terminal").json()
    assert term["bars"] and term["orders"] and term["security"] == "BTC/USDT"
    assert term["live_endpoint"].startswith("/api/demo/live")

    # 一键停止：熔断开仓 + 平仓单送模拟盘
    s = client.post(f"/api/studio/paper/{sid}/liquidate").json()["session"]
    assert s["status"] == "stopped" and s["demo"]["halted"]
    assert b.bal.get("BTC", Decimal(0)) == 0 or not s["portfolio"]["positions"]
    # 连接的密钥从未出现在会话里
    assert "demokey-abcdef" not in json.dumps(s, ensure_ascii=False)


def test_demo_start_refuses_tampered_import(client, fake):
    slug = _strategy(client, "要改的策略")
    data = _retamper(client.get(f"/api/studio/strategies/{slug}/package").content)
    imp = client.post("/api/studio/packages/import", content=data).json()["strategy"]["slug"]
    client.post(f"/api/studio/strategies/{imp}/approve")
    fake["broker"] = FakeBroker()
    cid = client.post(
        "/api/demo/connections",
        json={"exchange": "binance", "env": "demo", "api_key": "k-1234567890", "api_secret": "secret-123"},
    ).json()["connection"]["id"]
    r = client.post(
        "/api/studio/paper/start",
        json={"slug": imp, "source": "exchange", "exchange": "binance", "execution": "demo", "demo_connection_id": cid},
    )
    assert r.status_code == 400 and "标准答案" in r.json()["message"]


def test_qrun_demo_with_fake_exchange(tmp_path, capsys):
    """qrun demo：同一个内核 + OMS，只是换成本机进程和命令行。"""
    import argparse

    from quant_agent import qrun
    from quant_agent.live.demo_cli import run_demo
    from quant_agent.strategies import package as P

    data, _ = P.build(
        name="命令行",
        slug="cli",
        kind="code",
        source=FLIP,
        params={},
        securities=["BTC/USDT"],
        timeframe="1h",
        author="t",
        team="t",
        version="v1",
        notes="",
    )
    pkg = tmp_path / "cli.qstrat"
    pkg.write_bytes(data)
    bars = _hist(320)
    b = FakeBroker()

    class Feed:
        i = 300

        def describe(self):
            return "测试行情"

        def closed_bars(self, wait):
            if self.i >= len(bars):
                raise KeyboardInterrupt
            self.i += 1
            b.price = Decimal(str(bars[self.i - 1].open))
            return {"BTC/USDT": bars[self.i - 3 : self.i]}

        def marks(self):
            return {}

        def close(self):
            pass

    args = argparse.Namespace(
        package=str(pkg),
        exchange="binance",
        env="demo",
        cash=10_000.0,
        fee=0.001,
        slippage_bps=5.0,
        minutes=1,
        poll=0.01,
        max_order=100_000.0,
    )
    code = run_demo(
        args, qrun._load, qrun._module, lambda s, tf, n, ex: {"BTC/USDT": bars[:300]}, lambda *a: Feed(), broker=b
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert len(b.placed) >= 5 and "对账一致" in out and b.closed
