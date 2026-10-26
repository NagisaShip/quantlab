from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform import context
from quant_agent.store import Collections

BUILTIN_FACTORS = {
    "builtin_factor_reversal_5",
    "builtin_factor_momentum_30_5",
    "builtin_factor_low_vol",
}
BUILTIN_MODELS = {"builtin_model_ridge_multi", "builtin_model_garch_vol"}
BUILTIN_PORTFOLIOS = {"builtin_portfolio_risk_parity", "builtin_portfolio_min_var"}
BUILTIN_STRATEGIES = {"builtin-ma-cross", "builtin-donchian-breakout"}


@pytest.fixture()
def seeded_client(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_SEED_BUILTINS", "1")
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)
    with TestClient(create_app()) as c:
        login(c)
        yield c, services
    reset_services()


def test_builtin_assets_are_seeded_once_and_pass_admission(seeded_client):
    client, services = seeded_client

    factors = [f for f in client.get("/api/factors").json()["factors"] if f.get("created_by") == "builtin"]
    assert {f["id"] for f in factors} == BUILTIN_FACTORS
    assert all(f["status"] == "candidate" and f["admitted"] and not f["activation_eligible"] for f in factors)
    for f in factors:
        report = client.get(f"/api/factors/{f['id']}/report").json()["report"]
        assert report["admitted"] is True
        assert report["builtin_review"]["mechanism"] == "factor_evaluate"

    models = [m for m in client.get("/api/models").json()["models"] if m.get("created_by") == "builtin"]
    assert {m["id"] for m in models} == BUILTIN_MODELS
    assert all(m["status"] == "candidate" and m["admitted"] and not m["activation_eligible"] for m in models)
    for m in models:
        assert client.get(f"/api/models/{m['id']}/report").json()["report"]["admitted"] is True

    portfolios = [p for p in client.get("/api/portfolio").json()["schemes"] if p.get("created_by") == "builtin"]
    assert {p["id"] for p in portfolios} == BUILTIN_PORTFOLIOS
    assert all(p["status"] == "candidate" and p["admitted"] and not p["activation_eligible"] for p in portfolios)
    for p in portfolios:
        assert client.get(f"/api/portfolio/{p['id']}/report").json()["report"]["admitted"] is True

    strategies = client.get("/api/studio/strategies").json()["strategies"]
    builtin = [s for s in strategies if s.get("source") == "builtin"]
    assert {s["slug"] for s in builtin} == BUILTIN_STRATEGIES
    assert all(s["status"] == "backtested" and s["human_approved"] for s in builtin)
    for s in builtin:
        rows = client.get(f"/api/research/backtests?slug={s['slug']}").json()["backtests"]
        assert len(rows) == 1
        assert rows[0]["status"] == "done"
        assert (rows[0].get("review") or {}).get("blocks_promotion") is False

    # 群组与任务页的引导需要一个平台内置示例；它必须幂等且能被成员打开。
    groups = client.get("/api/collab/groups").json()["groups"]
    builtin_groups = [g for g in groups if g["name"] == "平台内置 · 研究协作示例"]
    assert len(builtin_groups) == 1
    gid = builtin_groups[0]["id"]
    tasks = client.get(f"/api/collab/groups/{gid}/tasks").json()["tasks"]
    assert any(t["title"] == "复核内置双均线策略的回测与审查" for t in tasks)

    assert len([f for f in client.get("/api/factors").json()["factors"] if f.get("created_by") == "builtin"]) == 3
    assert len([m for m in client.get("/api/models").json()["models"] if m.get("created_by") == "builtin"]) == 2
    assert len([p for p in client.get("/api/portfolio").json()["schemes"] if p.get("created_by") == "builtin"]) == 2

    ws = client.headers["X-Workspace-Id"]
    with context.bind(ws, user_id="test"):
        store = services.store.current()
        marker = store.get(Collections.SETTINGS, "builtin_assets_v1")
        assert marker and marker["complete"] is True and not marker["errors"]
        assert len(store.list(Collections.BACKTESTS)) == 2
