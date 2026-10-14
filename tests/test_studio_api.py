"""策略工作室 API 测试：文件化策略、规则编译、回测、实时模拟、实盘对照。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services

SIMPLE = """
def initialize(ctx):
    set_benchmark(ctx.securities[0])
    g.fast, g.slow = 12, 48
    run_daily(rebalance, time="close")

def rebalance(ctx):
    s = ctx.securities[0]
    closes = history(s, g.slow, "close")
    if len(closes) < g.slow:
        return
    fast = sum(closes[-g.fast:]) / g.fast
    slow = sum(closes) / g.slow
    order_target_percent(s, 0.8 if fast > slow else 0.0)
"""


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("studio-store")
    services = AppServices.build(store_root=str(root))
    set_services(services)
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


# ---------------- 策略文件 ----------------
def test_save_list_read_delete_strategy(client):
    r = client.get("/api/studio/strategies")
    assert r.status_code == 200
    body = r.json()
    assert "strategies" in body and body["layout"]
    assert any("strategy.py" in line for line in body["layout"])

    r = client.post(
        "/api/studio/strategies",
        json={"name": "我的趋势策略", "code": SIMPLE, "team": "趋势研究组", "symbol": "BTC/USDT"},
    )
    assert r.status_code == 200, r.text
    slug = r.json()["strategy"]["slug"]
    assert r.json()["strategy"]["version"]

    r = client.get(f"/api/studio/strategies/{slug}")
    detail = r.json()
    assert detail["strategy"]["code"] == SIMPLE
    assert detail["issues"] == []
    assert detail["versions"] == []

    # 保存新内容 → 产生历史版本
    r = client.post(
        "/api/studio/strategies",
        json={"name": "我的趋势策略", "code": SIMPLE + "\n# v2\n", "slug": slug},
    )
    assert r.status_code == 200
    r = client.get(f"/api/studio/strategies/{slug}")
    assert len(r.json()["versions"]) == 1

    r = client.get(f"/api/studio/strategies/{slug}/export")
    assert r.status_code == 200
    assert "团队：" in r.text and "def initialize" in r.text

    r = client.delete(f"/api/studio/strategies/{slug}")
    assert r.json()["ok"] is True
    assert client.get(f"/api/studio/strategies/{slug}").status_code == 404


def test_static_check_blocks_dangerous_code(client):
    r = client.post(
        "/api/studio/strategies",
        json={"name": "坏策略", "code": "import requests\ndef initialize(ctx): pass\n"},
    )
    assert r.status_code == 400
    assert "不允许导入" in r.json()["message"]


def test_check_endpoint_reports_functions(client):
    r = client.post("/api/studio/strategies", json={"name": "检查用", "code": SIMPLE})
    slug = r.json()["strategy"]["slug"]
    r = client.post(f"/api/studio/strategies/{slug}/check")
    body = r.json()
    assert body["ok"] is True
    assert "initialize" in body["functions"] and "handle_data" in body["functions"]
    client.delete(f"/api/studio/strategies/{slug}")


def test_check_endpoint_reports_syntax_error(client):
    r = client.post(
        "/api/studio/strategies",
        json={"name": "语法错", "code": "def initialize(ctx:\n    pass\n", "allow_issues": True},
    )
    assert r.status_code == 200
    slug = r.json()["strategy"]["slug"]
    r = client.post(f"/api/studio/strategies/{slug}/check")
    assert r.json()["ok"] is False
    assert "语法错误" in r.json()["issues"][0]
    client.delete(f"/api/studio/strategies/{slug}")


# ---------------- 规则 → Python ----------------
def test_rule_compiles_to_python_and_saves(client):
    r = client.post("/api/studio/rules/preview-python", json={"rule": {}})
    assert r.status_code == 200
    assert "def initialize" in r.text
    assert "sma_fast > sma_slow" in r.text

    r = client.post(
        "/api/studio/strategies/from-rule",
        json={"rule": {"name": "编译规则", "team": "第四研究组"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy"]["kind"] == "rule"
    assert body["rule_version"]
    assert "def handle_bar" in body["strategy"]["code"]
    client.delete(f"/api/studio/strategies/{body['strategy']['slug']}")


def test_rule_compile_rejects_bad_rule(client):
    r = client.post("/api/studio/rules/preview-python", json={"rule": {"buy": "eval('1')"}})
    assert r.status_code == 400


def test_rule_compiled_strategy_actually_backtests(client):
    r = client.post("/api/studio/strategies/from-rule", json={"rule": {"name": "回测规则"}})
    slug = r.json()["strategy"]["slug"]
    r = client.post(
        "/api/studio/backtest",
        json={"slug": slug, "symbol": "BTC/USDT", "timeframe": "1h", "bars_limit": 900, "warmup_bars": 100},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fills"], "编译出的策略必须能成交"
    assert body["metrics"]["observations"] > 0
    assert body["dataset"]["source"] in ("exchange", "synthetic")
    client.delete(f"/api/studio/strategies/{slug}")


# ---------------- 回测 ----------------
def test_backtest_with_inline_code(client):
    r = client.post(
        "/api/studio/backtest",
        json={
            "code": SIMPLE,
            "name": "内联策略",
            "bars_limit": 600,
            "warmup_bars": 100,
            "rebalance_tolerance": 0.0,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy_name"] == "内联策略"
    assert body["cost_model"]["taker_fee"] == 0.001
    assert len(body["equity_curve"]) == len(body["timestamps"])


def test_backtest_rejects_dangerous_code(client):
    r = client.post("/api/studio/backtest", json={"code": "import socket\ndef initialize(ctx): pass"})
    assert r.status_code == 400


def test_backtest_requires_slug_or_code(client):
    r = client.post("/api/studio/backtest", json={})
    assert r.status_code == 400


def test_backtest_surfaces_strategy_runtime_errors(client):
    """策略自身报错要作为可读信息返回，而不是 500。"""
    r = client.post(
        "/api/studio/backtest",
        json={
            "code": "def initialize(ctx):\n    raise RuntimeError('策略初始化失败')\n",
            "bars_limit": 300,
        },
    )
    assert r.status_code == 400
    assert "策略初始化失败" in r.json()["message"]


# ---------------- 实时模拟 ----------------
def _approved_strategy(client, name="实时用策略") -> str:
    r = client.post("/api/studio/strategies", json={"name": name, "code": SIMPLE})
    slug = r.json()["strategy"]["slug"]
    client.post(f"/api/studio/strategies/{slug}/approve")
    return slug


def test_paper_start_requires_approval(client):
    r = client.post("/api/studio/strategies", json={"name": "未批准策略", "code": SIMPLE})
    slug = r.json()["strategy"]["slug"]
    r = client.post("/api/studio/paper/start", json={"slug": slug})
    assert r.status_code == 400
    assert "尚未人工批准" in r.json()["message"]
    client.delete(f"/api/studio/strategies/{slug}")


def test_paper_lifecycle(client):
    slug = _approved_strategy(client, "实时生命周期")
    r = client.post("/api/studio/paper/start", json={"slug": slug, "initial_cash": 10000, "source": "demo"})
    assert r.status_code == 200, r.text
    session = r.json()["session"]
    assert session["status"] == "running"
    assert session["strategy_version"]
    sid = session["id"]

    r = client.post(f"/api/studio/paper/{sid}/tick")
    assert r.status_code == 200
    assert r.json()["session"]["last_bar_ts"] > 0

    r = client.get(f"/api/studio/paper/{sid}/report")
    assert r.status_code == 200
    report = r.json()
    assert "windows" in report and "7" in report["windows"]["windows"]
    assert any("不能替代" in c for c in report["conventions"])
    assert {c["days"] for c in report["session"]["checkpoints"]} == {7, 30}

    r = client.get("/api/studio/paper")
    assert any(s["id"] == sid for s in r.json()["sessions"])

    # worker 崩溃：租约过期 → RECOVERING；下一次推进从检查点恢复，已处理的成交不会重复
    fills_before = len(client.get(f"/api/runs/{sid}/fills").json()["items"])
    r = client.post(f"/api/studio/paper/{sid}/recover")
    assert r.json()["session"]["status"] == "recovering"
    r = client.post(f"/api/studio/paper/{sid}/tick")
    s2 = r.json()["session"]
    assert s2["status"] == "running" and s2["attempts"] == 2
    assert any("检查点恢复" in e["message"] for e in s2["events"])
    assert len(client.get(f"/api/runs/{sid}/fills").json()["items"]) == fills_before

    # 暂停 / 恢复
    assert client.post(f"/api/studio/paper/{sid}/pause").json()["session"]["status"] == "paused"
    assert client.get(f"/api/runs/{sid}").json()["run"]["state"] == "PAUSED"
    assert client.post(f"/api/studio/paper/{sid}/resume").json()["session"]["status"] == "running"

    # 运行中不能删除
    assert client.delete(f"/api/studio/paper/{sid}").status_code == 409

    r = client.post(f"/api/studio/paper/{sid}/stop")
    assert r.json()["session"]["status"] == "stopped"
    assert client.get(f"/api/runs/{sid}").json()["run"]["state"] == "CANCELED"
    events = client.get(f"/api/runs/{sid}/events").json()["items"]
    states = [e["data"]["state"] for e in events if e["type"] == "state"]
    assert states[0] == "QUEUED" and "RECOVERING" in states and states[-1] == "CANCELED"

    assert client.delete(f"/api/studio/paper/{sid}").json()["ok"] is True
    client.delete(f"/api/studio/strategies/{slug}")


def test_paper_liquidate_and_stop(client):
    slug = _approved_strategy(client, "平仓结束")
    sid = client.post("/api/studio/paper/start", json={"slug": slug, "source": "demo"}).json()["session"]["id"]
    r = client.post(f"/api/studio/paper/{sid}/liquidate")
    assert r.status_code == 200
    s = r.json()["session"]
    assert s["status"] == "stopped" and not s["portfolio"]["positions"]
    assert client.get(f"/api/runs/{sid}").json()["run"]["state"] == "COMPLETED"
    # 已结束的运行不再接受命令
    r = client.post(f"/api/runs/{sid}/commands", json={"command": "pause"})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


def test_paper_start_rejects_unknown_strategy(client):
    assert client.post("/api/studio/paper/start", json={"slug": "nope"}).status_code == 404


# ---------------- 实盘对照 ----------------
def test_reconcile_demo_removed_no_fake_live_data(client):
    """红线：没有真实成交时不允许用演示 / 模拟数据冒充实盘。"""
    assert client.get("/api/studio/reconcile/demo").status_code == 404
    st = client.get("/api/live/status").json()
    assert st["has_live_data"] is False and "未接入真实成交" in st["message"] and st["live_enabled"] is False


def test_reconcile_with_live_csv(client):
    r = client.post("/api/studio/strategies/from-rule", json={"rule": {"name": "对照用"}})
    slug = r.json()["strategy"]["slug"]

    csv_text = (
        "timestamp,security,side,amount,price,fee,client_order_id\n1700000000000,BTC/USDT,buy,0.001,62000,0.062,c-0\n"
    )
    r = client.post(
        "/api/studio/reconcile",
        json={"slug": slug, "live_csv": csv_text, "bars_limit": 600, "warmup_bars": 100},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["demo"] is False
    assert body["live_fills"] == 1
    assert any("分别核算" in n for n in body["notes"])
    client.delete(f"/api/studio/strategies/{slug}")


def test_reconcile_rejects_bad_csv(client):
    r = client.post("/api/studio/reconcile", json={"simulated_fills": [], "live_csv": "a,b\n1,2\n"})
    assert r.status_code == 400
    assert "缺少必需列" in r.json()["message"]


def test_reconcile_requires_a_source(client):
    r = client.post("/api/studio/reconcile", json={})
    assert r.status_code == 400


# ---------------- 交易票据与能力矩阵 ----------------
def test_order_refuses_live_trading(client):
    r = client.post(
        "/api/studio/order",
        json={"symbol": "BTC/USDT", "side": "buy", "qty": 0.001, "client_order_id": "c-1", "simulated": False},
    )
    assert r.status_code == 400
    assert "默认不启用实盘下单" in r.json()["message"]


def test_order_surfaces_missing_credentials(client):
    r = client.post(
        "/api/studio/order",
        json={
            "exchange": "binance",
            "symbol": "BTC/USDT",
            "side": "buy",
            "qty": 0.001,
            "client_order_id": "c-1",
            "simulated": True,
        },
    )
    assert r.status_code == 400
    assert "API Key" in r.json()["message"] or "Secret" in r.json()["message"]


def test_exchange_capabilities_are_exposed(client):
    r = client.get("/api/studio/exchanges/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert set(body["exchanges"]) == {"binance", "okx", "bybit"}
    for rows in body["exchanges"].values():
        keys = {c["key"]: c for c in rows}
        assert keys["withdraw"]["supported"] is False
        assert keys["public_market_data"]["supported"] is True
