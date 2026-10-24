# Agent Runtime Core (early implementation)

An independent Python package for durable, single-agent model/tool execution.
It does not import QuantLab. SQLite or PostgreSQL transactions are the
execution authority; the append-only event stream is committed in the same
transaction. API readers can use `events(run_id, after=cursor)` to replay from
a database cursor. PostgreSQL uses a dedicated schema and a transaction-scoped
advisory lock so multiple worker processes can share the same Runtime journal.

Install with `python -m pip install ./packages/agent-runtime`, then run the
independent host (or use `PYTHONPATH=packages/agent-runtime/src` during development):

```text
python packages/agent-runtime/examples/minimal_cli.py demo.sqlite start
python packages/agent-runtime/examples/minimal_cli.py demo.sqlite resume RUN_ID
```

The first command pauses for tool approval. The second command runs in a new
process, records an idempotent approval signal, and finishes. The example uses
only local model and calculator adapters.

Current safety boundary: an uncertain `external_effect` remains in
`RECONCILING` until the host confirms its status with `reconcile_effect`.
The runtime does not automatically retry such an effect. Unknown model calls
wait for provider-confirmed usage before a new logical step can be created;
read-only tools require an explicit `retry_safe` declaration. A lease epoch prevents an old worker
from committing after lease expiry or takeover; heartbeats renew a valid lease
while model/tool I/O is outstanding. It cannot undo an external effect. Hosts can raise
`ModelUnavailable` for a definitive pre-dispatch rejection (for example, quota or
missing credentials); the Runtime marks that attempt and run failed and releases
its budget reservation instead of entering reconciliation.

`Journal.checkpoint(run_id)` records an optional checksummed state summary at
an event cursor. Before each transition, `Journal.audit(run_id)` folds the
committed event stream and compares it with the transactionally maintained
state rows. Stale or corrupt checkpoints are ignored; state/event divergence
stops execution for investigation.
Checkpoint retention defaults to the latest two snapshots; compaction never
deletes execution events. `ModelResult.tool_calls` supports multiple tools in a
single model response, each with its own step, attempt and stable effect ID.
`ToolSpec.pause_after=True` creates a durable `WAITING_INPUT` step. An `input`
signal must identify that step and resumes the saved context; signal ID reuse
with different parameters, actors or target runs is rejected.

QuantLab now has an opt-in ingress/projection bridge in
`src/quant_agent/platform/runtime_bridge.py`. It uses a distinct ticket kind,
preserves the external run ID, and blocks the legacy worker from claiming
Runtime-owned runs. QuantLab also has a ModelHub reservation ledger and
single-dispatch model adapter; tested Runtime calls use that ledger rather than
the legacy router's guard/record callbacks. Workspace daily call/token quotas
are checked with active reservations and settled in the same transaction as
provider usage. Team role execution uses TeamRunner's existing sequential,
parallel and debate orchestration while each role's model/tool loop is durable
through Runtime when `QAGENT_AGENT_RUNTIME=1`. A PostgreSQL journal test runs
eight independent Runtime processes across four workers. Interactive Lab chat
also uses Runtime when enabled: system/history/image context, multiple tool
calls, exact approvals, user input waits, cancellation and recovery are durable.
Unknown effects can be resolved by an authenticated administrator through a
persistent `effect_resolution` signal. The Lab API exposes database-cursor
events and idempotent resume requests; browser recovery controls use those APIs.

The same kernel and fault tests run against SQLite and, when `QAGENT_TEST_PG`
is configured, isolated PostgreSQL schemas. See
[`docs/Agent-Runtime-Fault-Matrix.md`](../../docs/Agent-Runtime-Fault-Matrix.md)
for the fault cases, host boundaries and verification commands.

Approval grants expire before dispatch after `ToolSpec.approval_ttl_ms` (24 hours by default). An expired grant returns the step to approval waiting; renewal uses a new signal identity. QuantLab generic Agents and Team roles share the same durable execution adapter when enabled.

## Telemetry

Install `pip install -e './packages/agent-runtime[telemetry]'` to use the optional
OpenTelemetry SDK adapter. The host owns exporters, endpoints and metric readers;
the core never installs a global provider. This follows the
[OpenTelemetry Python instrumentation boundary](https://opentelemetry.io/docs/languages/python/instrumentation/).

```python
from agent_runtime import RuntimeTelemetry
from agent_runtime.telemetry import OpenTelemetrySpanSink, OpenTelemetryMetrics

telemetry = RuntimeTelemetry(journal)
telemetry.export_attempts(OpenTelemetrySpanSink(exporter), consumer_id="production-collector", tenant_id="workspace")
OpenTelemetryMetrics(telemetry, meter, tenant_id="workspace")
```

Only completed attempt facts are exported. Attempts in one run share a stable
trace ID; every attempt has a stable span ID and carries run/step/attempt/effect
correlation attributes. These are flat attempt spans, without fabricated parent
spans. Export acknowledgement is durable and delivery is **at least once**:
a crash after collector acceptance and before local acknowledgement can redeliver
the same IDs. Exporter errors never change execution state or dispatch work.
One consumer must use a stable consumer ID across restarts; concurrent exporters
can also redeliver, so collectors must deduplicate by trace/span identity.

Metrics observe durable totals (run/step states, attempts, duration and confirmed
cost), rather than replaying increments after restart. Attributes omit prompts,
responses, tool arguments and exception text. QuantLab exposes the current
workspace's aggregate snapshot to workspace administrators through
`GET /api/runs/runtime-metrics`.
