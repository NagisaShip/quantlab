"""API 端到端测试：用 FastAPI TestClient 打真实路由（不含真实 LLM 调用）。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    services = AppServices.build(store_root=str(root))
    set_services(services)
    app = create_app()
    with TestClient(app) as c:
        login(c)
        yield c
    reset_services()


# ---------------- 配置 ----------------
def test_config_endpoints(client):
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert "providers" in body and "routing" in body
    # 密钥必须脱敏
    for p in body["providers"]:
        assert "*" in p["key_masked"] or p["key_masked"] == ""

    r = client.get("/api/config/models")
    assert r.status_code == 200
    assert r.json()["models"] == []  # 不内置服务商

    r = client.get("/api/config/presets")
    presets = r.json()["presets"]
    assert any(p["name"] == "deepseek" for p in presets)
    assert any(p["name"] == "local" for p in presets)


# ---------------- 行情 ----------------
def test_market_endpoints(client):
    r = client.get("/api/market/symbols")
    assert r.status_code == 200
    assert "BTC/USDT" in r.json()["symbols"]

    r = client.get("/api/market/bars", params={"symbol": "BTC/USDT", "timeframe": "1h", "limit": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["bars"])
    assert 50 <= body["count"] <= 200
    assert set(body["bars"][0]) >= {"t", "o", "h", "l", "c", "v", "closed"}

    r = client.get("/api/market/quality", params={"symbol": "BTC/USDT", "limit": 300})
    assert r.status_code == 200
    q = r.json()
    assert q["bars"] >= 50 and q["duplicates"] == 0

    r = client.get("/api/market/summary")
    assert r.status_code == 200
    assert "last_price" in r.json()


# ---------------- 策略与回测 ----------------
def test_templates_and_guidances(client):
    r = client.get("/api/templates")
    templates = r.json()["templates"]
    names = {t["name"] for t in templates}
    assert {"ma_trend", "mean_reversion", "grid_bounded", "dca_rebalance", "buy_hold", "cash"} <= names
    for t in templates:
        assert "param_schema" in t and "defaults" in t

    r = client.get("/api/guidances")
    gs = r.json()["guidances"]
    assert {g["id"] for g in gs} >= {"trend", "mean_reversion", "grid", "dca", "baseline"}
    for g in gs:
        assert g["questions"]

    r = client.post("/api/guidances/render", json={"guidance_id": "trend", "answers": {}})
    assert r.status_code == 200
    assert "BTC/USDT" in r.json()["prompt"]


def test_backtest_run_and_metrics(client):
    r = client.post(
        "/api/backtests",
        json={
            "strategy": "ma_trend",
            "params": {"fast": 10, "slow": 30},
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "initial_cash": 10000,
            "bars_limit": 800,
            "compare_scenarios": True,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    run = body["run"]
    assert run["strategy"] == "ma_trend"
    assert len(run["equity_curve"]) == len(run["timestamps"])
    assert len(run["equity_curve"]) == len(run["drawdown"])
    m = run["metrics"]
    assert m["observations"] > 0
    assert 0.0 <= m["max_drawdown"] <= 1.0
    assert "data_source" in run or run["notes"]
    assert set(body["scenarios"]) == {"baseline", "cost_1.5x", "cost_2x", "low_liquidity"}
    # 成本越高，收益不应更好
    assert body["scenarios"]["cost_2x"]["total_return"] <= body["scenarios"]["baseline"]["total_return"]

    run_id = run["id"]
    r = client.get(f"/api/template-runs/{run_id}")
    assert r.status_code == 200
    r = client.get("/api/template-runs")
    assert any(x["id"] == run_id for x in r.json()["runs"])


def test_backtest_unknown_strategy(client):
    r = client.post("/api/backtests", json={"strategy": "nope"})
    assert r.status_code == 400


def test_strategy_version_lifecycle(client):
    r = client.post(
        "/api/strategies",
        json={"name": "我的均线策略", "template": "ma_trend", "params": {"fast": 5}},
    )
    assert r.status_code == 200
    strat = r.json()["strategy"]
    assert strat["params"]["fast"] == 5
    assert strat["params"]["slow"] == 30  # 默认值被合并进来
    assert strat["status"] == "draft"

    r = client.post(f"/api/strategies/{strat['id']}/approve")
    assert r.json()["strategy"]["human_approved"] is True

    r = client.delete(f"/api/strategies/{strat['id']}")
    assert r.json()["ok"] is True


# ---------------- Agent 角色 ----------------
def test_roles_crud(client):
    r = client.get("/api/agents/roles")
    body = r.json()
    roles = body["roles"]
    assert len(roles) >= 9
    assert any(x["id"] == "market_analyst" and x["builtin"] for x in roles)
    assert "market_summary" in body["tools"]

    r = client.post(
        "/api/agents/roles",
        json={"name": "测试角色", "system_prompt": "你是测试角色", "tags": ["测试"]},
    )
    role = r.json()["role"]
    assert role["builtin"] is False

    r = client.put(f"/api/agents/roles/{role['id']}", json={"name": "改名角色", "system_prompt": "x"})
    assert r.json()["role"]["name"] == "改名角色"

    r = client.post(f"/api/agents/roles/duplicate/{role['id']}")
    dup = r.json()["role"]
    assert dup["id"] != role["id"] and "副本" in dup["name"]

    r = client.delete(f"/api/agents/roles/{role['id']}")
    assert r.json()["ok"] is True
    client.delete(f"/api/agents/roles/{dup['id']}")

    # 内置角色不可删除
    r = client.delete("/api/agents/roles/market_analyst")
    assert r.status_code == 400


def test_role_ai_draft_falls_back_without_llm(client):
    r = client.post("/api/agents/roles/draft", json={"requirement": "负责检查滑点是否被低估"})
    assert r.status_code == 200
    role = r.json()["role"]
    assert role["source"] == "ai_draft"
    assert "滑点" in role["system_prompt"] or role["title"]
    # 必须带平台红线
    assert "禁止" in role["system_prompt"]


# ---------------- Agent 团队 ----------------
def test_teams_and_builtin(client):
    r = client.get("/api/agents/teams")
    teams = r.json()["teams"]
    ids = {t["id"] for t in teams}
    assert {"default_research_team", "strategy_build_team", "review_board", "adversarial_team"} <= ids
    modes = {t["mode"] for t in teams}
    assert {"sequential", "parallel", "debate"} <= modes


def test_team_crud(client):
    r = client.post(
        "/api/agents/teams",
        json={
            "name": "测试团队",
            "mode": "parallel",
            "role_ids": ["market_analyst", "risk_officer"],
            "synthesizer_id": "research_lead",
        },
    )
    team = r.json()["team"]
    assert team["mode"] == "parallel"

    r = client.put(
        f"/api/agents/teams/{team['id']}",
        json={"name": "改名团队", "mode": "sequential", "role_ids": ["market_analyst"]},
    )
    assert r.json()["team"]["name"] == "改名团队"

    r = client.delete(f"/api/agents/teams/{team['id']}")
    assert r.json()["ok"] is True


def test_team_run_degrades_without_llm(client):
    """没有可用 LLM 时，团队运行不得 500，应返回失败步骤与状态。"""
    r = client.post(
        "/api/agents/teams/default_research_team/run",
        json={"topic": "评估 BTC 均线策略", "include_market_data": True, "bars_limit": 200},
    )
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert run["topic"] == "评估 BTC 均线策略"
    assert run["status"] in ("done", "failed")
    assert isinstance(run["steps"], list) and run["steps"]
    # 每个步骤都必须记录角色与状态
    for s in run["steps"]:
        assert s["role_id"] and s["status"] in ("ok", "error")

    r = client.get("/api/agents/team-runs")
    assert any(x["id"] == run["id"] for x in r.json()["runs"])


def test_team_run_404(client):
    r = client.post("/api/agents/teams/nope/run", json={"topic": "x"})
    assert r.status_code == 404


# ---------------- 长期 Agent 与通知 ----------------
def test_long_running_preview_and_run(client):
    r = client.get("/api/long-running/preview")
    assert r.status_code == 200
    assert "digest_data" in r.json() and "evidence" in r.json()

    r = client.post("/api/long-running/digest/run", json={"dry_run": True})
    assert r.status_code == 200
    record = r.json()["record"]
    assert record["agent_id"] == "daily_digest"
    assert "非投资建议" in record["output"]

    r = client.post("/api/long-running/maintainer/run", json={"dry_run": True})
    assert r.status_code == 200
    assert r.json()["record"]["agent_id"] == "strategy_maintainer"


def test_notifications_and_schedules(client):
    r = client.post("/api/notifications/test", params={"channel": "console"})
    assert r.status_code == 200 and r.json()["ok"] is True

    r = client.get("/api/notifications")
    assert r.status_code == 200

    r = client.post("/api/schedules", json={"id": "morning_digest", "cron": "0 9 * * *", "kind": "digest"})
    assert r.status_code == 200
    r = client.get("/api/schedules")
    assert any(j["id"] == "morning_digest" for j in r.json()["jobs"])

    r = client.post("/api/schedules", json={"cron": "bad cron", "kind": "digest"})
    assert r.status_code == 400

    r = client.post("/api/schedules/tick")
    assert r.status_code == 200 and "fired" in r.json()

    r = client.delete("/api/schedules/morning_digest")
    assert r.json()["ok"] is True


# ---------------- 总览与提示词库 ----------------
def test_overview_and_prompts(client):
    r = client.get("/api/overview")
    assert r.status_code == 200
    body = r.json()
    assert "counts" in body and "models" in body
    assert body["disclaimer"] == "仅为研究结果，非投资建议。"

    r = client.get("/api/agents/prompts")
    prompts = r.json()["prompts"]
    assert len(prompts) >= 8
    ids = {p.get("id") for p in prompts}
    assert "strategy_gen.trend" in ids
    assert all("body" in p for p in prompts)
    # 角色提示词也应在库中，且不重复维护两份
    assert "role.market_analyst" in ids
    assert sum(1 for p in prompts if p.get("kind") == "role") >= 9

    r = client.get("/api/agents/store-stats")
    assert r.json()["roles"] >= 9


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_openapi_schema(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    for p in ["/api/backtests", "/api/agents/roles", "/api/agents/teams/{team_id}/run", "/api/overview"]:
        assert p in paths


def test_every_endpoint_has_chinese_summary(client):
    """API 文档是交付物的一部分：不能出现 Read Config 这种默认英文名。"""
    paths = client.get("/openapi.json").json()["paths"]
    missing = []
    for path, ops in paths.items():
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete"):
                continue
            summary = op.get("summary", "")
            if not any("一" <= ch <= "鿿" for ch in summary):
                missing.append(f"{method.upper()} {path} -> {summary!r}")
    assert not missing, "以下端点缺少中文摘要：" + "; ".join(missing)


def test_summaries_cover_all_routes():
    """summaries.py 里的映射不应留下无用条目（防止路由改名后文档悄悄失配）。"""
    from quant_agent.api.summaries import SUMMARIES

    app = create_app()
    spec = app.openapi()
    live = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops if m in ("get", "post", "put", "delete")}
    stale = sorted(k for k in SUMMARIES if k not in live)
    assert not stale, f"summaries.py 中有已失效的条目：{stale}"
