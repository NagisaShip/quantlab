"""Experiment A/B: terminate a real worker between dispatch and result commit."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult, ToolCall, ToolSpec


def _crash_worker(tmp_path, database, run_id, marker, source):
    script = tmp_path / "worker.py"
    script.write_text(textwrap.dedent(source), encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    return subprocess.run(
        [sys.executable, str(script), env.get("QAGENT_TEST_RUNTIME_DB", str(database)), run_id, str(marker)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


class FinalModel:
    calls = 0

    def generate(self, messages, *, request_id):
        self.calls += 1
        return ModelResult(text="recovered", usage_micro=2)


class NoTool:
    def execute(self, call, *, effect_id):
        raise AssertionError("tool was dispatched again")

    def reconcile(self, call, *, effect_id):
        return None


def test_killed_model_worker_waits_for_usage_before_regeneration(tmp_path):
    database = tmp_path / "runtime.sqlite"
    marker = tmp_path / "provider-requests.txt"
    journal = Journal(database)
    runtime = AgentRuntime(journal, FinalModel(), NoTool())
    run_id = runtime.start("question", AgentSpec("demo", "1", budget_limit_micro=20, model_reservation_micro=5))
    child = _crash_worker(
        tmp_path,
        database,
        run_id,
        marker,
        """
        import os
        import sys
        from agent_runtime import AgentRuntime, Journal

        class Model:
            def generate(self, messages, *, request_id):
                with open(sys.argv[3], "a", encoding="utf-8") as file:
                    file.write(request_id + "\\n")
                    file.flush()
                os._exit(17)

        class Tools:
            def execute(self, call, *, effect_id):
                raise AssertionError("unexpected tool")
            def reconcile(self, call, *, effect_id):
                return None

        AgentRuntime(Journal(sys.argv[1], schema=os.environ.get("QAGENT_TEST_RUNTIME_SCHEMA", "agent_runtime")), Model(), Tools(), worker_id="crashed").resume(sys.argv[2])
    """,
    )
    assert child.returncode == 17, child.stderr
    step_id = runtime.get(run_id)["steps"][0]["id"]
    assert marker.read_text(encoding="utf-8").splitlines() == [step_id]
    assert runtime.get(run_id)["steps"][0]["state"] == "IN_FLIGHT"
    with journal.tx() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (step_id,))
    restarted_model = FinalModel()
    restarted = AgentRuntime(Journal(database), restarted_model, NoTool(), worker_id="replacement")
    assert restarted.resume(run_id)["state"] == "WAITING"
    assert restarted_model.calls == 0
    assert restarted.settle_model_uncertainty(run_id, step_id, usage_micro=1, actor="provider")
    assert restarted.resume(run_id)["state"] == "COMPLETED"
    assert restarted_model.calls == 1
    assert journal.budget("run:" + run_id)["spent_micro"] == 3
    events = restarted.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))


def test_killed_tool_worker_reconciles_external_effect_without_republishing(tmp_path):
    database = tmp_path / "runtime.sqlite"
    marker = tmp_path / "published-effects.txt"

    class PublishModel:
        def generate(self, messages, *, request_id):
            if any(message["role"] == "tool" for message in messages):
                return ModelResult(text="published", usage_micro=1)
            return ModelResult(tool_call=ToolCall("publish", {"name": "strategy"}), usage_micro=1)

    journal = Journal(database)
    runtime = AgentRuntime(journal, PublishModel(), NoTool())
    run_id = runtime.start("publish", AgentSpec("demo", "1", tools=(ToolSpec("publish", effect="external_effect"),)))
    child = _crash_worker(
        tmp_path,
        database,
        run_id,
        marker,
        """
        import os
        import sys
        from agent_runtime import AgentRuntime, Journal, ModelResult, ToolCall

        class Model:
            def generate(self, messages, *, request_id):
                return ModelResult(tool_call=ToolCall("publish", {"name": "strategy"}), usage_micro=1)

        class Tools:
            def execute(self, call, *, effect_id):
                with open(sys.argv[3], "a", encoding="utf-8") as file:
                    file.write(effect_id + "\\n")
                    file.flush()
                os._exit(23)
            def reconcile(self, call, *, effect_id):
                return None

        AgentRuntime(Journal(sys.argv[1], schema=os.environ.get("QAGENT_TEST_RUNTIME_SCHEMA", "agent_runtime")), Model(), Tools(), worker_id="crashed").resume(sys.argv[2])
    """,
    )
    assert child.returncode == 23, child.stderr
    step = runtime.get(run_id)["steps"][1]
    assert marker.read_text(encoding="utf-8").splitlines() == [step["effect_id"]]
    assert step["state"] == "IN_FLIGHT"
    with journal.tx() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (step["id"],))

    class ReconcilingTool(NoTool):
        def reconcile(self, call, *, effect_id):
            assert effect_id == step["effect_id"]
            return True, "published"

    restarted = AgentRuntime(Journal(database), PublishModel(), ReconcilingTool(), worker_id="replacement")
    assert restarted.resume(run_id)["state"] == "WAITING"
    assert restarted.reconcile_effect(run_id, step["id"], actor="external-status")
    assert restarted.resume(run_id)["state"] == "COMPLETED"
    assert marker.read_text(encoding="utf-8").splitlines() == [step["effect_id"]]
    events = restarted.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))


def test_killed_approval_wait_resumes_from_durable_signal(tmp_path):
    database = tmp_path / "runtime.sqlite"
    marker = tmp_path / "waiting.txt"

    class ApprovalModel:
        def generate(self, messages, *, request_id):
            if any(message["role"] == "tool" for message in messages):
                return ModelResult(text="approved", usage_micro=1)
            return ModelResult(tool_call=ToolCall("publish", {"name": "strategy"}), usage_micro=1)

    class ApprovalTool(NoTool):
        def execute(self, call, *, effect_id):
            return "published"

    journal = Journal(database)
    runtime = AgentRuntime(journal, ApprovalModel(), ApprovalTool())
    run_id = runtime.start("publish", AgentSpec("demo", "1", tools=(ToolSpec("publish", requires_approval=True),)))
    child = _crash_worker(
        tmp_path,
        database,
        run_id,
        marker,
        """
        import os
        import sys
        from agent_runtime import AgentRuntime, Journal, ModelResult, ToolCall

        class Model:
            def generate(self, messages, *, request_id):
                return ModelResult(tool_call=ToolCall("publish", {"name": "strategy"}), usage_micro=1)

        class Tools:
            def execute(self, call, *, effect_id):
                raise AssertionError("approval was bypassed")
            def reconcile(self, call, *, effect_id):
                return None

        state = AgentRuntime(Journal(sys.argv[1], schema=os.environ.get("QAGENT_TEST_RUNTIME_SCHEMA", "agent_runtime")), Model(), Tools()).resume(sys.argv[2])["state"]
        with open(sys.argv[3], "w", encoding="utf-8") as file:
            file.write(state)
            file.flush()
        os._exit(31)
    """,
    )
    assert child.returncode == 31, child.stderr
    assert marker.read_text(encoding="utf-8") == "WAITING"
    restarted = AgentRuntime(Journal(database), ApprovalModel(), ApprovalTool(), worker_id="replacement")
    step_id = restarted.get(run_id)["steps"][1]["id"]
    digest = next(
        event["data"]["arguments_sha256"] for event in restarted.events(run_id) if event["type"] == "approval.required"
    )
    assert restarted.signal(
        run_id,
        "approval",
        {"step_id": step_id, "approved": True, "arguments_sha256": digest},
        signal_id="approval-1",
        actor="reviewer",
    )
    assert restarted.resume(run_id)["state"] == "COMPLETED"
    events = restarted.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
