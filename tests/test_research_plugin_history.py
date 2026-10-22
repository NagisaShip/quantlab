"""Round 4: disabling a domain blocks new work but keeps historical research readable."""

from __future__ import annotations

import math

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform import context
from quant_agent.workflows.contracts import WorkflowError


@pytest.fixture()
def client(tmp_path):
    reset_services()
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)
    with TestClient(create_app()) as c:
        me = login(c)
        with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
            yield c, services, me
    reset_services()


def _disable(c: TestClient, plugin: str) -> None:
    r = c.put(f"/api/auth/research-plugins/{plugin}", json={"enabled": False})
    assert r.status_code == 200, r.text
    row = next(p for p in r.json()["plugins"] if p["id"] == plugin)
    assert row["enabled"] is False


def _blocked(c: TestClient, method: str, path: str, **kwargs) -> None:
    r = getattr(c, method)(path, **kwargs)
    assert r.status_code == 400, f"{method.upper()} {path} should stay blocked, got {r.status_code}: {r.text}"


def _seed_factor(c: TestClient) -> dict:
    r = c.post("/api/factors", json={"name": "历史反转", "expression": "-returns(5)", "evaluate": False})
    assert r.status_code == 200, r.text
    fid = r.json()["factor"]["id"]
    # Real backend evaluation, persisted before the capability is disabled.
    r = c.post(f"/api/factors/{fid}/refresh", json={"source": "synthetic", "bars_limit": 600})
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert report["summary"]["rank_ic"] is not None and report["panel"]
    return {"id": fid, "expression": r.json()["factor"]["expression"], "report": report}


def _seed_model(c: TestClient) -> dict:
    r = c.post(
        "/api/models/train",
        json={
            "model": "ridge",
            "universe": "major10",
            "timeframe": "4h",
            "bars_limit": 400,
            "source": "synthetic",
            "folds": 3,
            "wait": True,
        },
    )
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["status"] == "done", job
    r = c.post("/api/models", json={"run_id": job["run_id"], "name": "历史基线模型", "status": "candidate"})
    assert r.status_code == 200, r.text
    model = r.json()["model"]
    return {"id": model["id"], "run_id": job["run_id"], "job_id": job["id"], "spec": model["spec"]}


def _seed_portfolio(c: TestClient) -> dict:
    r = c.post(
        "/api/portfolio/evaluate",
        json={
            "config": {"optimizer": "hrp"},
            "universe": "major10",
            "timeframe": "4h",
            "bars_limit": 500,
            "source": "synthetic",
        },
    )
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    r = c.post("/api/portfolio", json={"run_id": run["id"], "name": "历史组合方案", "status": "candidate"})
    assert r.status_code == 200, r.text
    scheme = r.json()["scheme"]
    return {"id": scheme["id"], "run_id": run["id"], "config": scheme["config"]}


def _seed_execution(c: TestClient) -> dict:
    r = c.post(
        "/api/execution/evaluate",
        json={"config": {"algo": "twap", "episodes": 6}, "symbols": ["BTC/USDT"], "source": "synthetic", "steps": 800},
    )
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    r = c.post("/api/execution", json={"run_id": run["id"], "name": "历史执行方案", "status": "candidate"})
    assert r.status_code == 200, r.text
    scheme = r.json()["scheme"]
    return {"id": scheme["id"], "run_id": run["id"], "config": scheme["config"]}


class _FakeBook:
    """Deterministic public order book; only used to start/stop a runtime recording."""

    def __init__(self) -> None:
        self.i = 0

    def fetch_order_book(self, symbol: str, depth: int):
        self.i += 1
        mid = 100 + 0.01 * math.sin(self.i / 3)
        return {
            "ts": 1_700_000_000_000 + self.i * 1000,
            "bids": [[mid - 0.01 - 0.01 * k, 5 + k] for k in range(depth)],
            "asks": [[mid + 0.01 + 0.01 * k, 5 + k] for k in range(depth)],
        }

    def fetch_trades(self, symbol: str):
        t = 1_700_000_000_000 + self.i * 1000
        return [
            {"id": f"{self.i}-{j}", "t": t + 100 * j, "p": 100 + 0.01 * (-1) ** j, "q": 0.5, "side": "buy" if j % 2 else "sell"}
            for j in range(5)
        ]


def test_disabled_domains_keep_history_and_block_new_work(client):
    c, services, me = client
    factor = _seed_factor(c)
    model = _seed_model(c)
    portfolio = _seed_portfolio(c)
    execution = _seed_execution(c)

    # Runtime recording started before the domain is disabled; stop must keep working after.
    services.market.adapters["binance"] = _FakeBook()
    rec = c.post("/api/execution/recordings", json={"symbol": "BTC/USDT", "seconds": 30, "interval": 1})
    assert rec.status_code == 200, rec.text
    recording_id = rec.json()["recording"]["id"]

    _disable(c, "factor")
    _disable(c, "model")
    _disable(c, "portfolio")
    _disable(c, "execution")

    # --- factor: list/detail/report/source/PIN survive; every new write is rejected.
    rows = c.get("/api/factors")
    assert rows.status_code == 200 and any(x["id"] == factor["id"] for x in rows.json()["factors"])
    detail = c.get(f"/api/factors/{factor['id']}")
    assert detail.status_code == 200 and detail.json()["factor"]["expression"] == factor["expression"]
    report = c.get(f"/api/factors/{factor['id']}/report")
    assert report.status_code == 200 and report.json()["report"]["summary"]["rank_ic"] is not None
    src = c.post("/api/research/pycode", json={"kind": "factor", "artifact": factor["expression"], "name": "历史反转"})
    assert src.status_code == 200 and "def " in src.json()["code"]
    pin = c.post(f"/api/research/record-retention/factor/{factor['id']}/pin")
    assert pin.status_code == 200 and pin.json()["pinned"] is True
    _blocked(c, "post", "/api/factors/evaluate", json={"expression": "-returns(5)", "source": "synthetic", "bars_limit": 300})
    _blocked(c, "post", "/api/factors", json={"name": "新因子", "expression": "returns(3)", "evaluate": False})
    _blocked(c, "post", f"/api/factors/{factor['id']}/refresh", json={"source": "synthetic", "bars_limit": 300})
    _blocked(c, "post", f"/api/factors/{factor['id']}/status", json={"status": "active"})
    _blocked(c, "patch", f"/api/factors/{factor['id']}", json={"note": "停用期间不能改"})
    _blocked(c, "delete", f"/api/factors/{factor['id']}")

    # Agent policy follows the same capability switch: no live model call involved.
    from quant_agent.agents import platform_tools as pt

    names = {tool.name for tool in pt.available_registry().values()}
    assert "list_factors" not in names and "workflow_plugins" in names
    # Registered handler path and model-facing invoke path both enforce the policy.
    with pytest.raises(WorkflowError, match="因子研究"):
        pt.registry()["list_factors"].handler()
    invoked = pt.invoke("list_factors", {}, user=me["user"]["id"])
    assert "因子研究" in invoked["result"]["error"]

    # --- model: history and run reports survive; train/save/update/refresh are rejected.
    assert c.get("/api/models").status_code == 200
    assert c.get(f"/api/models/{model['id']}").status_code == 200
    assert c.get(f"/api/models/{model['id']}/report").status_code == 200
    assert c.get(f"/api/models/runs/{model['run_id']}").status_code == 200
    src = c.post("/api/research/pycode", json={"kind": "model", "artifact": model["spec"], "name": "历史基线模型"})
    assert src.status_code == 200 and "model" in src.json()["code"].lower()
    _blocked(c, "post", "/api/models/train", json={"model": "ridge", "source": "synthetic", "bars_limit": 300})
    _blocked(c, "post", "/api/models", json={"run_id": model["run_id"], "name": "停用期间新模型"})
    _blocked(c, "patch", f"/api/models/{model['id']}", json={"status": "active"})
    _blocked(c, "post", f"/api/models/{model['id']}/refresh", json={"source": "synthetic", "bars_limit": 300})
    _blocked(c, "delete", f"/api/models/{model['id']}")
    cancelled = c.post(f"/api/models/jobs/{model['job_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text

    # --- portfolio: historical scheme/report survive; evaluate/risk/save/update are rejected.
    assert c.get("/api/portfolio").status_code == 200
    assert c.get(f"/api/portfolio/{portfolio['id']}").status_code == 200
    assert c.get(f"/api/portfolio/{portfolio['id']}/report").status_code == 200
    assert c.get(f"/api/portfolio/runs/{portfolio['run_id']}").status_code == 200
    src = c.post("/api/research/pycode", json={"kind": "portfolio", "artifact": portfolio["config"], "name": "历史组合方案"})
    assert src.status_code == 200 and "optimize" in src.json()["code"]
    _blocked(c, "post", "/api/portfolio/evaluate", json={"config": {"optimizer": "hrp"}, "source": "synthetic", "bars_limit": 300})
    _blocked(c, "post", "/api/portfolio/risk", json={"weights": {"BTC/USDT": 1.0}, "source": "synthetic"})
    _blocked(c, "post", "/api/portfolio", json={"run_id": portfolio["run_id"], "name": "停用期间新方案"})
    _blocked(c, "patch", f"/api/portfolio/{portfolio['id']}", json={"status": "active"})
    _blocked(c, "delete", f"/api/portfolio/{portfolio['id']}")

    # --- execution: historical scheme/report/costs/recordings survive; new runs are rejected.
    assert c.get("/api/execution").status_code == 200
    assert c.get(f"/api/execution/{execution['id']}").status_code == 200
    assert c.get(f"/api/execution/{execution['id']}/report").status_code == 200
    assert c.get(f"/api/execution/runs/{execution['run_id']}").status_code == 200
    assert c.get("/api/execution/costs").status_code == 200
    assert c.get("/api/execution/recordings").status_code == 200
    # 雷达是新的行情计算，不是冻结历史；停用时必须在取数前拒绝。
    _blocked(c, "get", "/api/execution/radar?universe=major10&timeframe=5m&source=synthetic")
    src = c.post("/api/research/pycode", json={"kind": "execution", "artifact": execution["config"], "name": "历史执行方案"})
    assert src.status_code == 200 and "def simulate(" in src.json()["code"]
    _blocked(c, "post", "/api/execution/evaluate", json={"config": {"algo": "twap", "episodes": 4}, "symbols": ["BTC/USDT"], "source": "synthetic", "steps": 600})
    _blocked(c, "post", "/api/execution/replay", json={"config": {"algo": "twap"}, "symbols": ["BTC/USDT"], "source": "synthetic", "steps": 600})
    _blocked(c, "post", "/api/execution", json={"run_id": execution["run_id"], "name": "停用期间新执行方案"})
    _blocked(c, "patch", f"/api/execution/{execution['id']}", json={"status": "active"})
    _blocked(c, "delete", f"/api/execution/{execution['id']}")
    _blocked(c, "post", "/api/execution/capacity", json={"symbols": ["BTC/USDT"], "turnover": 20, "alpha": 0.2})
    _blocked(c, "post", "/api/execution/costs/calibrate", json={})
    _blocked(c, "post", "/api/execution/recordings", json={"symbol": "BTC/USDT", "seconds": 10, "interval": 1})
    _blocked(c, "delete", f"/api/execution/recordings/{recording_id}")
    stopped = c.post(f"/api/execution/recordings/{recording_id}/stop")
    assert stopped.status_code == 200 and stopped.json()["ok"] is True

    # Re-enabling restores normal writes without losing any history.
    r = c.put("/api/auth/research-plugins/factor", json={"enabled": True})
    assert r.status_code == 200
    assert c.post("/api/factors/validate", json={"expression": "-returns(5)"}).json()["ok"] is True
    assert c.get(f"/api/factors/{factor['id']}/report").status_code == 200


def test_disabled_execution_radar_rejected_before_market_read(client, monkeypatch):
    c, services, _ = client
    _disable(c, "execution")
    original = services.market.get_bars
    calls: list[tuple] = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("execution radar fetched market data while disabled")

    monkeypatch.setattr(services.market, "get_bars", boom)
    blocked = c.get("/api/execution/radar?universe=major10&timeframe=5m&source=synthetic")
    assert blocked.status_code == 400, blocked.text
    assert calls == []

    # Enabled behavior is preserved once the capability is restored.
    assert c.put("/api/auth/research-plugins/execution", json={"enabled": True}).status_code == 200
    monkeypatch.setattr(services.market, "get_bars", original)
    enabled = c.get("/api/execution/radar?universe=major10&timeframe=5m&source=synthetic")
    assert enabled.status_code == 200, enabled.text
    assert len(enabled.json()["rows"]) == 10


def test_visibility_agent_and_workspace_remain_independent(client):
    c, _, me = client
    # A personal hidden navigation / Agent switch does not revoke HTTP history access.
    r = c.put("/api/auth/research-plugins/factor", json={"visible": False, "agent": False})
    assert r.status_code == 200, r.text
    row = next(p for p in r.json()["plugins"] if p["id"] == "factor")
    assert row["enabled"] is True and row["visible"] is False and row["agent"] is False
    assert c.get("/api/factors").status_code == 200
    from quant_agent.agents import platform_tools as pt

    assert "list_factors" not in {tool.name for tool in pt.available_registry().values()}
    with pytest.raises(WorkflowError, match="Agent"):
        pt.registry()["list_factors"].handler()
    invoked = pt.invoke("list_factors", {}, user=me["user"]["id"])
    assert "Agent" in invoked["result"]["error"]
    # Restoring the personal Agent preference restores the read tool.
    assert c.put("/api/auth/research-plugins/factor", json={"agent": True}).status_code == 200
    assert "list_factors" in {tool.name for tool in pt.available_registry().values()}
    assert isinstance(pt.registry()["list_factors"].handler(), list)

    # Disable factor only in this workspace; a second workspace keeps it enabled and writable.
    _disable(c, "factor")
    other = c.post("/api/workspaces", json={"name": "Round 4 独立工作区"})
    assert other.status_code == 200, other.text
    headers = {"X-Workspace-Id": other.json()["id"]}
    listing = c.get("/api/auth/research-plugins", headers=headers)
    assert listing.status_code == 200
    other_factor = next(p for p in listing.json()["plugins"] if p["id"] == "factor")
    assert other_factor["enabled"] is True and other_factor["visible"] is True and other_factor["agent"] is True
    assert c.get("/api/factors", headers=headers).status_code == 200
    created = c.post(
        "/api/factors",
        headers=headers,
        json={"name": "另一个工作区的因子", "expression": "-returns(5)", "evaluate": False},
    )
    assert created.status_code == 200, created.text
    # The disabled workspace still cannot create.
    _blocked(c, "post", "/api/factors", json={"name": "停用工作区新因子", "expression": "returns(3)", "evaluate": False})
