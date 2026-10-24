"""PostgreSQL shared-journal verification for multi-process Runtime workers."""

from __future__ import annotations

import os
import sys
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult

PG = os.environ.get("QAGENT_TEST_PG", "").strip()
pytestmark = pytest.mark.skipif(not PG, reason="QAGENT_TEST_PG is not configured")


def _schema() -> str:
    return "agent_rt_" + uuid.uuid4().hex[:12]


def _worker_run(args):
    url, schema, index = args
    from agent_runtime import Journal

    class Model:
        def generate(self, messages, *, request_id):
            return ModelResult(text=f"answer-{index}", usage_micro=0)

    class Tools:
        def execute(self, call, *, effect_id):
            raise AssertionError("no tool expected")

        def reconcile(self, call, *, effect_id):
            return None

    run_id = f"pg-{index}-{uuid.uuid4().hex}"
    runtime = AgentRuntime(Journal(url, schema=schema), Model(), Tools(), worker_id=f"worker-{index}")
    runtime.start("question", AgentSpec("demo", "1"), run_id=run_id, tenant_id="tenant")
    result = runtime.resume(run_id)
    return run_id, result["state"], result["seq"], result["output"]


def test_postgres_journal_supports_concurrent_runtime_workers():
    schema = _schema()
    Journal(PG, schema=schema)  # initialize once for deterministic DDL diagnostics
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_worker_run, [(PG, schema, index) for index in range(8)]))
    reader = Journal(PG, schema=schema)
    for run_id, state, seq, output in results:
        assert state == "COMPLETED"
        assert output.startswith("answer-")
        run = reader.run(run_id)
        assert run is not None and run["seq"] == seq
        assert [event["seq"] for event in reader.events(run_id)] == list(range(1, seq + 1))
        assert reader.audit(run_id)["seq"] == seq


def test_app_services_uses_shared_postgres_runtime_journal(tmp_path, monkeypatch):
    from quant_agent.api.deps import AppServices

    schema = _schema()
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME_DB", PG)
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME_SCHEMA", schema)
    services = AppServices.build(store_root=str(tmp_path))
    journal = services.runtime_bridge.runtime.journal
    assert journal.dialect == "postgres"
    assert journal.schema == schema
