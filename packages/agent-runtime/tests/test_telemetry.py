"""Exporter failure and restart must never change execution or billing."""

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult
from agent_runtime.telemetry import OpenTelemetryMetrics, OpenTelemetrySpanSink, RuntimeTelemetry


class Model:
    def generate(self, messages, *, request_id):
        return ModelResult(text="secret response", usage_micro=3)


class Sink:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.batches = []

    def export(self, spans):
        self.batches.append(spans)
        return self.accepted


def make_run(journal, tenant):
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start(
        "secret prompt", AgentSpec("test", "1", budget_limit_micro=10, model_reservation_micro=4), tenant_id=tenant
    )
    runtime.resume(run_id)
    return runtime, run_id


def test_export_ack_failure_restart_and_tenant_scope(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime, run_id = make_run(journal, "a")
    make_run(journal, "b")
    telemetry = RuntimeTelemetry(journal)
    before = runtime.get(run_id)
    sink = Sink(accepted=False)
    assert telemetry.export_attempts(sink, consumer_id="collector", tenant_id="a") == 0
    first = sink.batches[0][0]
    assert first.attributes["agent_runtime.run_id"] == run_id
    assert "secret" not in repr(first)
    reopened = RuntimeTelemetry(Journal(journal.path))
    sink.accepted = True
    assert reopened.export_attempts(sink, consumer_id="collector", tenant_id="a") == 1
    assert sink.batches[1][0] == first
    assert reopened.export_attempts(sink, consumer_id="collector", tenant_id="a") == 0
    assert reopened.export_attempts(sink, consumer_id="collector", tenant_id="b") == 1
    assert runtime.get(run_id) == before
    assert reopened.snapshot(tenant_id="a")["spent_micro"] == 3
    assert reopened.snapshot()["spent_micro"] == 6


def test_exception_does_not_ack_or_change_runtime(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime, run_id = make_run(journal, "a")

    class Broken:
        def export(self, spans):
            raise OSError("collector unavailable")

    telemetry = RuntimeTelemetry(journal)
    with pytest.raises(OSError):
        telemetry.export_attempts(Broken(), consumer_id="collector", tenant_id="a")
    assert telemetry.export_attempts(Sink(), consumer_id="collector", tenant_id="a") == 1
    assert runtime.get(run_id)["state"] == "COMPLETED"
    journal.audit(run_id)


def test_metrics_read_snapshot_does_not_take_the_execution_writer_lock(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "runtime.sqlite")
    make_run(journal, "a")

    def forbidden():
        raise AssertionError("metrics attempted an execution write transaction")

    monkeypatch.setattr(journal, "tx", forbidden)
    assert RuntimeTelemetry(journal).snapshot(tenant_id="a")["spent_micro"] == 3
    error = sqlite3.OperationalError
    if journal.dialect == "postgres":
        from psycopg.errors import ReadOnlySqlTransaction

        error = ReadOnlySqlTransaction
    with journal.read_tx() as db:
        with pytest.raises(error):
            db.execute("UPDATE runs SET output='forbidden'")


def test_crash_after_export_ack_redelivers_identical_span_ids(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "runtime.sqlite")
    _runtime, run_id = make_run(journal, "a")
    telemetry = RuntimeTelemetry(journal)
    sink = Sink()
    original_tx = journal.tx

    @contextmanager
    def failed_commit():
        with original_tx() as db:
            yield db
            raise OSError("ack commit failed")

    monkeypatch.setattr(journal, "tx", failed_commit)
    with pytest.raises(OSError, match="ack commit"):
        telemetry.export_attempts(sink, consumer_id="collector", tenant_id="a")
    monkeypatch.setattr(journal, "tx", original_tx)
    assert telemetry.export_attempts(sink, consumer_id="collector", tenant_id="a") == 1
    assert sink.batches[0] == sink.batches[1]
    journal.audit(run_id)


def test_real_opentelemetry_export_and_observable_metrics(tmp_path):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    journal = Journal(tmp_path / "runtime.sqlite")
    _runtime, run_id = make_run(journal, "a")
    telemetry = RuntimeTelemetry(journal)
    exporter = InMemorySpanExporter()
    assert telemetry.export_attempts(OpenTelemetrySpanSink(exporter), consumer_id="sdk", tenant_id="a") == 1
    span = exporter.get_finished_spans()[0]
    assert span.name == "agent_runtime.model"
    assert span.attributes["agent_runtime.run_id"] == run_id
    assert span.end_time >= span.start_time
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    instrumentation = OpenTelemetryMetrics(telemetry, provider.get_meter("test"), tenant_id="a")
    assert len(instrumentation.instruments) == 7
    data = reader.get_metrics_data()
    metrics = {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert metrics["agent_runtime.runs"].data.data_points[0].value == 1
    # A second collection observes durable totals instead of incrementing again.
    assert reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics[0].data.data_points[0].value == 1
    provider.shutdown()


def test_streaming_first_token_survives_restart_and_counts_once(tmp_path):
    from agent_runtime.observations import first_token_observer, observe_first_token

    assert not observe_first_token()

    class StreamingModel:
        def generate(self, messages, *, request_id):
            from concurrent.futures import ThreadPoolExecutor

            callback = first_token_observer()
            with ThreadPoolExecutor(max_workers=1) as reader:
                assert reader.submit(callback).result(timeout=5)
                assert not reader.submit(callback).result(timeout=5)
            return ModelResult(text="saved", usage_micro=0)

    journal = Journal(tmp_path / "timing.sqlite")
    runtime = AgentRuntime(journal, StreamingModel(), None)
    run_id = runtime.start("secret", AgentSpec("stream", "1"), tenant_id="a")
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    assert not observe_first_token()  # context does not leak past generate
    reopened = RuntimeTelemetry(Journal(journal.path))
    assert reopened.snapshot(tenant_id="a")["first_token"]["n"] == 1
    assert reopened.snapshot(tenant_id="b")["first_token"] == {"n": 0, "total_ms": 0}
    sink = Sink()
    assert reopened.export_attempts(sink, consumer_id="timing", tenant_id="a") == 1
    assert sink.batches[0][0].attributes["agent_runtime.model.first_token_ms"] >= 0
    runtime.resume(run_id)
    assert reopened.snapshot(tenant_id="a")["first_token"]["n"] == 1
    journal.audit(run_id)


def test_nonstreaming_and_late_callback_do_not_invent_first_token(tmp_path):
    from agent_runtime.observations import first_token_observer

    callbacks = []

    class NonstreamingModel:
        def generate(self, messages, *, request_id):
            callbacks.append(first_token_observer())
            return ModelResult(text="saved", usage_micro=0)

    journal = Journal(tmp_path / "late.sqlite")
    runtime = AgentRuntime(journal, NonstreamingModel(), None)
    run_id = runtime.start("secret", AgentSpec("stream", "1"), tenant_id="a")
    runtime.resume(run_id)
    assert not callbacks[0]()
    telemetry = RuntimeTelemetry(journal)
    assert telemetry.snapshot(tenant_id="a")["first_token"]["n"] == 0
    sink = Sink()
    telemetry.export_attempts(sink, consumer_id="late", tenant_id="a")
    assert "agent_runtime.model.first_token_ms" not in sink.batches[0][0].attributes


def test_expired_lease_rejects_first_token_observation(tmp_path):
    from agent_runtime.observations import first_token_observer

    journal = Journal(tmp_path / "expired.sqlite")

    class ExpiredModel:
        def generate(self, messages, *, request_id):
            callback = first_token_observer()
            with journal.tx() as db:
                db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (request_id,))
            assert not callback()
            return ModelResult(text="late", usage_micro=0)

    runtime = AgentRuntime(journal, ExpiredModel(), None)
    run_id = runtime.start("secret", AgentSpec("stream", "1"), tenant_id="a")
    runtime.resume(run_id)
    assert RuntimeTelemetry(journal).snapshot(tenant_id="a")["first_token"]["n"] == 0


def test_trace_tree_parentage_bounded_delivery_and_restart(tmp_path):
    journal = Journal(tmp_path / "tree.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    root = runtime.start("secret root", AgentSpec("root", "1"), tenant_id="a")
    children = runtime.fork_children(root, [("secret child", AgentSpec("child", "1"))], idempotency_key="tree")
    runtime.resume(children[0])
    telemetry = RuntimeTelemetry(journal)
    sink = Sink(False)
    assert telemetry.export_trace(sink, root_run_id=root, consumer_id="tree", tenant_id="a") == 0
    child_batch = sink.batches[-1]
    assert len(child_batch) == 3  # child run, model step, attempt; parent still waiting
    assert "secret" not in repr(child_batch)
    runtime.resume(root)
    sink.accepted = True
    telemetry = RuntimeTelemetry(Journal(journal.path))
    collected = []
    while telemetry.export_trace(sink, root_run_id=root, consumer_id="tree", tenant_id="a", limit=2):
        assert len(sink.batches[-1]) <= 2
        collected.extend(sink.batches[-1])
    assert len(collected) == 6  # 2 runs + 2 model/Join steps + 2 attempts
    assert len({span.span_id for span in collected}) == 6
    assert len({span.trace_id for span in collected}) == 1
    by_id = {span.span_id: span for span in collected}
    roots = [span for span in collected if span.parent_span_id is None]
    assert len(roots) == 1 and roots[0].attributes["agent_runtime.run_id"] == root
    for span in collected:
        if span.parent_span_id:
            assert span.parent_span_id in by_id
            assert by_id[span.parent_span_id].start_ns <= span.start_ns
            assert by_id[span.parent_span_id].end_ns >= span.end_ns
    child_run = next(span for span in collected if span.attempt_id == "run:" + children[0])
    assert by_id[child_run.parent_span_id].attributes["agent_runtime.run_id"] == root
    assert by_id[child_run.parent_span_id].name == "agent_runtime.step.model"
    assert all(span in collected for span in child_batch)
    with pytest.raises(ValueError):
        telemetry.export_trace(sink, root_run_id=root, consumer_id="other", tenant_id="b")
    with pytest.raises(ValueError):
        telemetry.export_trace(sink, root_run_id=children[0], consumer_id="other", tenant_id="a")
    journal.audit(root)


def test_tree_export_ack_crash_redelivers_identical_hierarchy(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "ack-tree.sqlite")
    runtime, root = make_run(journal, "a")
    before = runtime.get(root)
    telemetry = RuntimeTelemetry(journal)
    sink = Sink()
    original = journal.tx

    @contextmanager
    def failed_commit():
        with original() as db:
            yield db
            raise OSError("tree ack failed")

    monkeypatch.setattr(journal, "tx", failed_commit)
    with pytest.raises(OSError):
        telemetry.export_trace(sink, root_run_id=root, consumer_id="tree", tenant_id="a")
    monkeypatch.setattr(journal, "tx", original)
    assert telemetry.export_trace(sink, root_run_id=root, consumer_id="tree", tenant_id="a") == 3
    assert sink.batches[0] == sink.batches[1]
    assert runtime.get(root) == before


def test_sdk_exports_real_parent_contexts(tmp_path):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    journal = Journal(tmp_path / "sdk-tree.sqlite")
    _, root = make_run(journal, "a")
    exporter = InMemorySpanExporter()
    assert (
        RuntimeTelemetry(journal).export_trace(
            OpenTelemetrySpanSink(exporter), root_run_id=root, consumer_id="sdk-tree", tenant_id="a"
        )
        == 3
    )
    spans = exporter.get_finished_spans()
    root_span = next(span for span in spans if span.name == "agent_runtime.run")
    step = next(span for span in spans if span.name == "agent_runtime.step.model")
    attempt = next(span for span in spans if span.name == "agent_runtime.model")
    assert root_span.parent is None
    assert step.parent.span_id == root_span.context.span_id
    assert attempt.parent.span_id == step.context.span_id
    assert len({span.context.trace_id for span in spans}) == 1


def test_live_model_context_matches_restored_child_attempt_span(tmp_path):
    from agent_runtime.observations import current_model_trace

    observed = {}

    class Traced:
        def generate(self, messages, *, request_id):
            observed[request_id] = current_model_trace()
            return ModelResult(text="done", usage_micro=0)

    journal = Journal(tmp_path / "propagation.sqlite")
    runtime = AgentRuntime(journal, Traced(), None)
    root = runtime.start("parent", AgentSpec("root", "1"), tenant_id="a")
    child = runtime.fork_children(root, [("child", AgentSpec("child", "1"))], idempotency_key="context")[0]
    restored = AgentRuntime(Journal(journal.path), Traced(), None)
    assert restored.resume(child)["state"] == "COMPLETED"
    assert restored.resume(root)["state"] == "COMPLETED"
    assert current_model_trace() is None
    sink = Sink()
    RuntimeTelemetry(journal).export_trace(sink, root_run_id=root, consumer_id="context", tenant_id="a")
    attempts = [span for span in sink.batches[0] if "agent_runtime.attempt_id" in span.attributes]
    assert len(attempts) == 2
    for span in attempts:
        live = observed[span.attributes["agent_runtime.step_id"]]
        assert live.trace_id == span.trace_id and live.span_id == span.span_id
        assert live.traceparent == f"00-{span.trace_id:032x}-{span.span_id:016x}-01"
    assert len({trace.trace_id for trace in observed.values()}) == 1
    assert len({trace.span_id for trace in observed.values()}) == 2


def test_regeneration_uses_new_attempt_context_in_same_trace(tmp_path):
    from agent_runtime.observations import current_model_trace

    observed = []

    class Flaky:
        def generate(self, messages, *, request_id):
            observed.append(current_model_trace())
            if len(observed) == 1:
                raise OSError("provider result unknown")
            return ModelResult(text="regenerated", usage_micro=0)

    journal = Journal(tmp_path / "retry-context.sqlite")
    runtime = AgentRuntime(journal, Flaky(), None)
    root = runtime.start("request", AgentSpec("test", "1"), tenant_id="a")
    waiting = runtime.resume(root)
    assert waiting["state"] == "WAITING"
    assert runtime.settle_model_uncertainty(root, waiting["steps"][0]["id"], usage_micro=0, actor="reviewer")
    assert AgentRuntime(Journal(journal.path), Flaky(), None).resume(root)["state"] == "COMPLETED"
    assert len(observed) == 2
    assert observed[0].trace_id == observed[1].trace_id
    assert observed[0].span_id != observed[1].span_id
