"""部署管理员调整 worker 槽位、服务器内存总览与自动排队。"""

from __future__ import annotations

import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import context
from quant_agent.platform.worker import Worker
from quant_agent.platform.worker_config import DEFAULT_WORKER_SLOTS, write_worker_config


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    try:
        yield create_app()
    finally:
        reset_services()


def test_admin_can_set_worker_slots_and_overview_reports_memory(app):
    with TestClient(app) as client:
        login(client)
        cfg = client.get("/api/admin/worker-config")
        assert cfg.status_code == 200, cfg.text
        assert cfg.json()["config"]["slots"] == DEFAULT_WORKER_SLOTS == 8

        saved = client.put("/api/admin/worker-config", json={"slots": 6})
        assert saved.status_code == 200, saved.text
        assert saved.json()["config"]["slots"] == 6
        assert saved.json()["config"]["configured"] is True

        overview = client.get("/api/admin/overview").json()
        assert overview["worker_config"]["slots"] == 6
        assert overview["server"]["memory"]["total"] > 0
        assert "rss_bytes" in overview["server"]["process"]


def test_worker_refreshes_slots_without_restart(app):
    with TestClient(app) as client:
        login(client)
        services = get_services()
        worker = Worker(services, slots=2)
        worker.refresh_config()
        assert worker.slots == 2  # 未显式配置时保留启动参数
        write_worker_config(services.global_store, 7, user_id="admin")
        worker._config_checked_at = 0
        worker.refresh_config()
        assert worker.slots == 7
        worker.pool.shutdown(wait=False)


def test_queued_runs_report_their_position(app):
    with TestClient(app) as client:
        login(client)
        ws = client.headers["X-Workspace-Id"]
        services = get_services()
        with context.bind(ws, user_id="queue-test"):
            first = services.runs.create(
                workspace_id=ws, user_id="queue-test", kind="backtest", mode="backtest", title="queue-1"
            )
            time.sleep(0.01)
            second = services.runs.create(
                workspace_id=ws, user_id="queue-test", kind="backtest", mode="backtest", title="queue-2"
            )
        rows = client.get("/api/runs?limit=10").json()["items"]
        by_id = {r["id"]: r for r in rows}
        assert by_id[first["id"]]["state"] == "QUEUED"
        assert by_id[second["id"]]["state"] == "QUEUED"
        assert by_id[first["id"]]["queue_position"] == 1
        assert by_id[second["id"]]["queue_position"] == 2
