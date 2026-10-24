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
    assert len(instrumentation.instruments) == 5
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
