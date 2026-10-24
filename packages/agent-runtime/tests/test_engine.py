import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, Journal
from agent_runtime.contracts import AgentSpec, ModelResult, ModelUnavailable, ToolCall, ToolSpec
from agent_runtime.journal import JournalIntegrityError


class Model:
    def generate(self, messages, *, request_id):
        if not any(message["role"] == "tool" for message in messages):
            return ModelResult(tool_call=ToolCall("calculator", {"x": 2, "y": 3}), usage_micro=2)
        return ModelResult(text="5", usage_micro=2)


class Tool:
    def __init__(self):
        self.calls = []

    def execute(self, call, *, effect_id):
        self.calls.append(effect_id)
        return call.arguments["x"] + call.arguments["y"]

    def reconcile(self, call, *, effect_id):
        return None


def test_multiple_tools_are_individual_steps_before_next_model(tmp_path):
    class BatchModel:
        def generate(self, messages, *, request_id):
            if any(message["role"] == "tool" for message in messages):
                assert len([message for message in messages if message["role"] == "tool"]) == 2
                return ModelResult(text="done", usage_micro=0)
            return ModelResult(
                tool_calls=[ToolCall("calculator", {"x": 1, "y": 2}), ToolCall("calculator", {"x": 3, "y": 4})],
                usage_micro=0,
            )

    tool = Tool()
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), BatchModel(), tool)
    run_id = runtime.start("batch", AgentSpec("demo", "1", tools=(ToolSpec("calculator"),)))
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    assert [step["kind"] for step in runtime.get(run_id)["steps"]] == ["model", "tool", "tool", "model"]
    assert len(set(tool.calls)) == 2


def test_input_wait_survives_reopen_and_keeps_initial_context(tmp_path):
    class AskingModel:
        def generate(self, messages, *, request_id):
            assert messages[0] == {"role": "system", "content": "durable context"}
            if messages[-1]["role"] == "user" and messages[-1]["content"] == "answer":
                return ModelResult(text="done", usage_micro=0)
            return ModelResult(tool_call=ToolCall("calculator", {"x": 1, "y": 2}), usage_micro=0)

    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, AskingModel(), Tool())
    run_id = runtime.start(
        "ask",
        AgentSpec("demo", "1", tools=(ToolSpec("calculator", pause_after=True),)),
        initial_messages=[{"role": "system", "content": "durable context"}, {"role": "user", "content": "ask"}],
    )
    assert runtime.resume(run_id)["state"] == "WAITING"
    reopened = AgentRuntime(Journal(journal.path), AskingModel(), Tool())
    target = reopened.get(run_id)["steps"][-1]["id"]
    data = {"step_id": target, "content": "answer"}
    assert reopened.signal(run_id, "input", data, signal_id="answer-1", actor="user")
    assert not reopened.signal(run_id, "input", data, signal_id="answer-1", actor="user")
    assert reopened.resume(run_id)["state"] == "COMPLETED"


def test_approval_survives_reopen_and_signal_is_idempotent(tmp_path):
    db = tmp_path / "runtime.sqlite"
    tool = Tool()
    spec = AgentSpec("demo", "1", tools=(ToolSpec("calculator", requires_approval=True),))
    first = AgentRuntime(Journal(db), Model(), tool)
    run_id = first.start("add", spec)
    assert first.resume(run_id)["state"] == "WAITING"
    step_id = first.get(run_id)["steps"][1]["id"]
    digest = next(
        event["data"]["arguments_sha256"] for event in first.events(run_id) if event["type"] == "approval.required"
    )
    second = AgentRuntime(Journal(db), Model(), tool)
    with pytest.raises(ValueError, match="arguments"):
        second.signal(
            run_id,
            "approval",
            {"step_id": step_id, "approved": True, "arguments_sha256": "wrong"},
            signal_id="bad",
            actor="user",
        )
    approval = {"step_id": step_id, "approved": True, "arguments_sha256": digest}
    assert second.signal(run_id, "approval", approval, signal_id="approval-1", actor="user")
    assert not second.signal(run_id, "approval", approval, signal_id="approval-1", actor="user")
    assert second.resume(run_id)["state"] == "COMPLETED"
    assert len(tool.calls) == 1
    assert [event["seq"] for event in second.events(run_id)] == list(range(1, len(second.events(run_id)) + 1))


def test_expired_approval_requires_a_fresh_grant_before_dispatch(tmp_path, monkeypatch):
    from agent_runtime import engine

    clock = engine.now_ms()
    monkeypatch.setattr(engine, "now_ms", lambda: clock)
    tool = Tool()
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), Model(), tool)
    run_id = runtime.start(
        "add", AgentSpec("demo", "1", tools=(ToolSpec("calculator", requires_approval=True, approval_ttl_ms=100),))
    )
    assert runtime.resume(run_id)["state"] == "WAITING"
    event = next(event for event in runtime.events(run_id) if event["type"] == "approval.required")
    approval = {
        "step_id": event["data"]["step_id"],
        "approved": True,
        "arguments_sha256": event["data"]["arguments_sha256"],
    }
    runtime.signal(run_id, "approval", approval, signal_id="first", actor="user")
    clock += 101
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert tool.calls == []
    runtime.signal(run_id, "approval", approval, signal_id="renewed", actor="user")
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    assert len(tool.calls) == 1


def test_confirmed_model_output_waits_for_usage_then_completes_without_regeneration(tmp_path):
    class MissingUsage:
        calls = 0

        def generate(self, messages, *, request_id):
            self.calls += 1
            return ModelResult(text="answer", usage_micro=None)

    model = MissingUsage()
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), model, Tool())
    run_id = runtime.start("question", AgentSpec("demo", "1", budget_limit_micro=20, model_reservation_micro=10))
    assert runtime.resume(run_id)["state"] == "WAITING"
    step_id = runtime.get(run_id)["steps"][0]["id"]
    assert runtime.get(run_id)["steps"][0]["state"] == "RECONCILING"
    assert runtime.journal.budget("run:" + run_id)["reserved_micro"] == 10
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert model.calls == 1
    assert runtime.settle_model_uncertainty(run_id, step_id, usage_micro=3, actor="provider")
    assert runtime.get(run_id)["state"] == "COMPLETED"
    assert runtime.get(run_id)["output"] == "answer"
    assert runtime.journal.budget("run:" + run_id)["reserved_micro"] == 0
    assert runtime.journal.budget("run:" + run_id)["spent_micro"] == 3
    assert model.calls == 1


def test_pre_dispatch_model_rejection_fails_and_releases_budget(tmp_path):
    class RejectedModel:
        def generate(self, messages, *, request_id):
            raise ModelUnavailable("quota exhausted before dispatch")

    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, RejectedModel(), Tool())
    run_id = runtime.start("question", AgentSpec("demo", "1", budget_limit_micro=20, model_reservation_micro=10))
    result = runtime.resume(run_id)
    assert result["state"] == "FAILED"
    assert result["steps"][0]["state"] == "FAILED"
    assert "before dispatch" in result["error"]
    assert journal.budget("run:" + run_id)["reserved_micro"] == 0
    assert journal.budget("run:" + run_id)["spent_micro"] == 0


def test_cancel_fences_late_model_result(tmp_path):
    db = tmp_path / "runtime.sqlite"
    tool = Tool()
    runtime = AgentRuntime(Journal(db), Model(), tool)
    run_id = runtime.start("add", AgentSpec("demo", "1", tools=(ToolSpec("calculator"),)))

    class CancelingModel(Model):
        def generate(self, messages, *, request_id):
            runtime.signal(run_id, "cancel", {}, signal_id="cancel-1", actor="user")
            return super().generate(messages, request_id=request_id)

    runtime.model = CancelingModel()
    assert runtime.resume(run_id)["state"] == "CANCELING"
    assert not tool.calls
    step_id = runtime.get(run_id)["steps"][0]["id"]
    assert runtime.settle_model_uncertainty(run_id, step_id, usage_micro=0, actor="provider")
    assert runtime.get(run_id)["state"] == "CANCELED"


def test_shared_budget_atomic_reservations(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    journal.ensure_budget("tenant", 4)
    runtime = AgentRuntime(journal, Model(), Tool())
    spec = AgentSpec("demo", "1", budget_limit_micro=100, model_reservation_micro=3)
    first = runtime.start("one", spec, account_ids=("tenant",))
    second = runtime.start("two", spec, account_ids=("tenant",))
    assert runtime.tick(first)
    assert runtime.tick(second) is False
    assert runtime.get(second)["steps"][0]["state"] == "WAITING_ADMISSION"
    assert journal.budget("tenant")["spent_micro"] == 2


def test_uncertain_external_effect_requires_reconciliation(tmp_path):
    class UncertainTool(Tool):
        def execute(self, call, *, effect_id):
            self.calls.append(effect_id)
            raise ConnectionError("result lost after external success")

        def reconcile(self, call, *, effect_id):
            assert effect_id == self.calls[0]
            return True, 5

    tool = UncertainTool()
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), Model(), tool)
    spec = AgentSpec("demo", "1", tools=(ToolSpec("calculator", effect="external_effect"),))
    run_id = runtime.start("add", spec)
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert len(tool.calls) == 1
    step_id = runtime.get(run_id)["steps"][1]["id"]
    assert runtime.reconcile_effect(run_id, step_id, actor="test")
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    assert len(tool.calls) == 1


def test_unknown_model_is_not_reissued_before_usage_reconciliation(tmp_path):
    class LostModel:
        def __init__(self):
            self.calls = 0

        def generate(self, messages, *, request_id):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("provider may still charge")
            return ModelResult(text="done", usage_micro=1)

    model = LostModel()
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, model, Tool())
    run_id = runtime.start("go", AgentSpec("demo", "1", budget_limit_micro=10, model_reservation_micro=3))
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert model.calls == 1
    assert journal.budget("run:" + run_id)["reserved_micro"] == 3
    step_id = runtime.get(run_id)["steps"][0]["id"]
    assert runtime.settle_model_uncertainty(run_id, step_id, usage_micro=2, actor="provider")
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    assert model.calls == 2
    assert journal.budget("run:" + run_id)["spent_micro"] == 3


def test_read_only_without_retry_contract_pauses(tmp_path):
    class LostTool(Tool):
        def execute(self, call, *, effect_id):
            self.calls.append(effect_id)
            raise TimeoutError("unknown read result")

    tool = LostTool()
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), Model(), tool)
    run_id = runtime.start("add", AgentSpec("demo", "1", tools=(ToolSpec("calculator"),)))
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert len(tool.calls) == 1


def test_external_success_then_database_commit_failure_does_not_replay(tmp_path):
    class FailingJournal(Journal):
        fail_next_commit = False

        @contextmanager
        def tx(self):
            with super().tx() as db:
                yield db
                if self.fail_next_commit:
                    self.fail_next_commit = False
                    raise OSError("injected database commit failure")

    journal = FailingJournal(tmp_path / "runtime.sqlite")

    class ExternalTool(Tool):
        def execute(self, call, *, effect_id):
            self.calls.append(effect_id)
            journal.fail_next_commit = True
            return 5

        def reconcile(self, call, *, effect_id):
            return True, 5

    tool = ExternalTool()
    runtime = AgentRuntime(journal, Model(), tool)
    run_id = runtime.start("add", AgentSpec("demo", "1", tools=(ToolSpec("calculator", effect="external_effect"),)))
    assert runtime.tick(run_id)
    with pytest.raises(OSError, match="commit failure"):
        runtime.tick(run_id)
    assert len(tool.calls) == 1
    step_id = runtime.get(run_id)["steps"][1]["id"]
    with journal.tx() as db:
        db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (step_id,))
    restarted = AgentRuntime(Journal(journal.path), Model(), tool, worker_id="replacement")
    assert restarted.resume(run_id)["state"] == "WAITING"
    assert len(tool.calls) == 1
    assert restarted.reconcile_effect(run_id, step_id, actor="provider")
    assert restarted.resume(run_id)["state"] == "COMPLETED"
    assert len(tool.calls) == 1


def test_expired_worker_cannot_commit_after_takeover(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    workers = {}

    class IdempotentTool(Tool):
        def execute(self, call, *, effect_id):
            self.calls.append(effect_id)
            if len(self.calls) == 1:
                step_id = workers["first"].get(run_id)["steps"][1]["id"]
                with journal.tx() as db:
                    db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (step_id,))
                assert workers["second"].tick(run_id)
                return "stale response"
            return 5

    tool = IdempotentTool()
    workers["first"] = AgentRuntime(journal, Model(), tool, worker_id="first")
    workers["second"] = AgentRuntime(journal, Model(), tool, worker_id="second")
    run_id = workers["first"].start(
        "add", AgentSpec("demo", "1", tools=(ToolSpec("calculator", effect="idempotent_write"),))
    )
    assert workers["first"].tick(run_id)
    assert workers["first"].tick(run_id) is False
    assert tool.calls[0] == tool.calls[1]
    assert workers["second"].resume(run_id)["output"] == "5"
    assert workers["second"].get(run_id)["steps"][1]["result"] == 5


def test_stale_or_corrupt_checkpoint_cannot_override_new_events(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), Tool())
    run_id = runtime.start("add", AgentSpec("demo", "1", tools=(ToolSpec("calculator"),)))
    old_seq = journal.checkpoint(run_id)
    assert runtime.tick(run_id)
    audited = journal.audit(run_id)
    assert audited["checkpoint_seq"] == old_seq
    assert audited["state"]["steps"][run_id + ":000001"] == "SUCCEEDED"
    with journal.tx() as db:
        db.execute("UPDATE checkpoints SET state='{}' WHERE run_id=? AND seq=?", (run_id, old_seq))
    assert journal.audit(run_id)["checkpoint_seq"] == 0
    assert runtime.resume(run_id)["state"] == "COMPLETED"


def test_state_event_divergence_stops_execution(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), Tool())
    run_id = runtime.start("go", AgentSpec("demo", "1"))
    with journal.tx() as db:
        db.execute("UPDATE runs SET state='COMPLETED' WHERE id=?", (run_id,))
    with pytest.raises(JournalIntegrityError, match="differs"):
        runtime.resume(run_id)
