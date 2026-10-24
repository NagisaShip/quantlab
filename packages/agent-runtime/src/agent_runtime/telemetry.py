"""Read-only observability projections; exporter failure cannot change execution.

Trace delivery is at least once. A consumer acknowledges only after its sink
accepts the batch. Crashes across the external acknowledgement boundary can
redeliver identical trace/span IDs, never regenerate model or tool work.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

from .journal import Journal, load, now_ms


def _identity(value: str, size: int) -> int:
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], "big") or 1


@dataclass(frozen=True)
class AttemptSpan:
    attempt_id: str
    trace_id: int
    span_id: int
    name: str
    start_ns: int
    end_ns: int
    status: str
    attributes: dict[str, str | int]


class SpanSink(Protocol):
    def export(self, spans: tuple[AttemptSpan, ...]) -> bool: ...


class RuntimeTelemetry:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    def snapshot(self, *, tenant_id: str | None = None) -> dict[str, Any]:
        """Consistent aggregates, without prompts, output, arguments or secrets.

        Hosts must authorize operator-wide access before omitting tenant_id.
        Totals are observed from durable facts, so collector restart does not
        double count attempts or budget settlement.
        """
        where = " WHERE r.tenant_id=?" if tenant_id is not None else ""
        args = (tenant_id,) if tenant_id is not None else ()
        with self.journal.read_tx() as db:
            runs = db.execute("SELECT r.state,COUNT(*) AS n FROM runs r" + where + " GROUP BY r.state", args).fetchall()
            steps = db.execute(
                "SELECT s.kind,s.state,COUNT(*) AS n FROM steps s JOIN runs r ON r.id=s.run_id"
                + where
                + " GROUP BY s.kind,s.state",
                args,
            ).fetchall()
            attempts = db.execute(
                "SELECT s.kind,a.status,COUNT(*) AS n,"
                "COALESCE(SUM(CASE WHEN a.ended_at>=a.started_at THEN a.ended_at-a.started_at ELSE 0 END),0) AS duration_ms "
                "FROM attempts a JOIN steps s ON s.id=a.step_id JOIN runs r ON r.id=s.run_id"
                + where
                + " GROUP BY s.kind,a.status",
                args,
            ).fetchall()
            # Count each reservation once, not once per shared budget account.
            fees = db.execute(
                "SELECT e.type,e.data FROM events e JOIN runs r ON r.id=e.run_id"
                + (where + " AND " if where else " WHERE ")
                + "e.type='budget.settled' ORDER BY e.run_id,e.seq",
                args,
            ).fetchall()
        # Settlement events may be replayed by administrative recovery. The
        # most recent confirmed amount for a logical step is counted once.
        settled = {data["step_id"]: data["actual_micro"] for data in (load(row["data"]) for row in fees)}
        return {
            "runs": [dict(row) for row in runs],
            "steps": [dict(row) for row in steps],
            "attempts": [{**dict(row), "duration_ms": int(row["duration_ms"])} for row in attempts],
            "spent_micro": sum(settled.values()),
        }

    def export_attempts(self, sink: SpanSink, *, consumer_id: str, tenant_id: str, limit: int = 100) -> int:
        if not consumer_id or not tenant_id or not 1 <= limit <= 1000:
            raise ValueError("consumer, tenant and a bounded batch are required")
        with self.journal.connection() as db:
            rows = db.execute(
                "SELECT a.*,s.run_id,s.kind,s.effect_id,r.tenant_id FROM attempts a "
                "JOIN steps s ON s.id=a.step_id JOIN runs r ON r.id=s.run_id "
                "WHERE r.tenant_id=? AND a.ended_at IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM telemetry_exports t WHERE t.consumer_id=? AND t.attempt_id=a.id) "
                "ORDER BY a.ended_at,a.id LIMIT ?",
                (tenant_id, consumer_id, limit),
            ).fetchall()
        spans = tuple(
            AttemptSpan(
                attempt_id=row["id"],
                trace_id=_identity("run:" + row["run_id"], 16),
                span_id=_identity("attempt:" + row["id"], 8),
                name="agent_runtime." + row["kind"],
                start_ns=row["started_at"] * 1000000,
                end_ns=max(row["started_at"], row["ended_at"]) * 1000000,
                status=row["status"],
                attributes={
                    "agent_runtime.run_id": row["run_id"],
                    "agent_runtime.step_id": row["step_id"],
                    "agent_runtime.attempt_id": row["id"],
                    "agent_runtime.tenant_id": row["tenant_id"],
                    "agent_runtime.epoch": row["epoch"],
                    "agent_runtime.effect_id": row["effect_id"] or "",
                    "agent_runtime.status": row["status"],
                },
            )
            for row in rows
        )
        if not spans or not sink.export(spans):
            return 0
        with self.journal.tx() as db:
            for span in spans:
                db.execute(
                    "INSERT INTO telemetry_exports(consumer_id,attempt_id,exported_at) VALUES(?,?,?) "
                    "ON CONFLICT(consumer_id,attempt_id) DO NOTHING",
                    (consumer_id, span.attempt_id, now_ms()),
                )
        return len(spans)


class OpenTelemetrySpanSink:
    """Explicit exporter supplied by the host, with synchronous acknowledgement.

    Does not install a global provider or choose an endpoint. Use the optional
    telemetry extra; core execution has no OpenTelemetry dependency.
    """

    def __init__(self, exporter: Any, *, service_name: str = "agent-runtime") -> None:
        from opentelemetry.sdk.resources import Resource

        self.exporter = exporter
        self.resource = Resource.create({"service.name": service_name})

    def export(self, spans: tuple[AttemptSpan, ...]) -> bool:
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.trace.export import SpanExportResult
        from opentelemetry.trace import SpanContext, Status, StatusCode, TraceFlags

        def context(span: AttemptSpan, span_id: int) -> SpanContext:
            return SpanContext(span.trace_id, span_id, False, TraceFlags(TraceFlags.SAMPLED))

        records = [
            ReadableSpan(
                name=span.name,
                context=context(span, span.span_id),
                resource=self.resource,
                attributes=span.attributes,
                start_time=span.start_ns,
                end_time=span.end_ns,
                status=Status(StatusCode.OK if span.status == "SUCCEEDED" else StatusCode.ERROR),
            )
            for span in spans
        ]
        return self.exporter.export(records) == SpanExportResult.SUCCESS


class OpenTelemetryMetrics:
    """Host-meter observable gauges backed by a durable snapshot, no replay counters."""

    def __init__(self, telemetry: RuntimeTelemetry, meter: Any, *, tenant_id: str) -> None:
        from opentelemetry.metrics import Observation

        if not tenant_id:
            raise ValueError("tenant is required")

        def callback(section: str, value: str, keys: tuple[str, ...]):
            def observe(options: Any):
                for row in telemetry.snapshot(tenant_id=tenant_id)[section]:
                    yield Observation(row[value], {key: row[key] for key in keys})

            return observe

        def spent(options: Any):
            yield Observation(telemetry.snapshot(tenant_id=tenant_id)["spent_micro"])

        self.instruments = [
            meter.create_observable_gauge("agent_runtime.runs", [callback("runs", "n", ("state",))]),
            meter.create_observable_gauge("agent_runtime.steps", [callback("steps", "n", ("kind", "state"))]),
            meter.create_observable_gauge("agent_runtime.attempts", [callback("attempts", "n", ("kind", "status"))]),
            meter.create_observable_gauge(
                "agent_runtime.attempt.duration_total",
                [callback("attempts", "duration_ms", ("kind", "status"))],
                unit="ms",
            ),
            meter.create_observable_gauge("agent_runtime.model.spent_micro", [spent]),
        ]
