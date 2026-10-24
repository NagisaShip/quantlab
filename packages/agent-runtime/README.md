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

## Structured child runs and join

```python
from agent_runtime import AgentSpec, ChildTask

parent = runtime.start("summarize", AgentSpec("parent", "1", budget_limit_micro=100, model_reservation_micro=10))
children = runtime.fork_children(
    parent,
    [
        ChildTask(
            "review A",
            AgentSpec("reviewer", "1", model_reservation_micro=20),
            initial_messages=[{"role": "system", "content": "Reviewer rules"}, {"role": "user", "content": "Review A"}],
        ),
        ChildTask("review B", AgentSpec("reviewer", "1", model_reservation_micro=20)),
    ],
    idempotency_key="review-round-1",
)
# Host workers rebind each child's ports, then call resume(child_id).
# Parent resume never dispatches children through the parent's model/tool ports.
runtime.resume(parent)
```

Fork requires an unclaimed READY model boundary. Parent suspension, child
creation, inherited budget accounts and join intent commit atomically. The same
key and identical request return the original child IDs; changed input is
rejected. Child results are joined in declaration order only after **all children
complete successfully**. A failed/canceled child fails the join once all children
are terminal. Unknown effects keep the child and parent waiting.

`runtime.children(parent)` discovers committed direct children after restart.
Each child keeps its own AgentSpec and complete initial system/history/image
messages. Children share the actual parent/ancestor budget accounts, not copied
limits, and may additionally have their own budget. Children with inherited
budget accounts must provide a positive model reservation estimate.

AgentSpec limits default to 32 direct children, 256 total descendants and depth
16. Every ancestor's limits apply; setting any relevant limit to zero disables
forking at that boundary. Each fork batch is also capped at 32. Hosts still own
tenant-wide root-run queue limits and child worker admission.

Canceling the parent propagates durably through the child tree in the same
transaction. In-flight/unknown effects and unsettled model charges remain in
CANCELING until reconciled. Parent resume refreshes joins bottom-up without
re-executing completed child work. This is structured fork/join. QuantLab's parallel, sequential and debate Team modes now use atomic graphs,
persistent dependencies and all-settled joins.

The standalone `examples/structured_cli.py DATABASE --pause-after-first` exits
with a WAITING parent. Run it again with `DATABASE --parent PARENT_ID` to finish
using only the independent package and its own database.

`start_with_children` commits the initial parent, children and join as one graph,
identified by a stable run ID and request digest. Repeated identical creation
returns the original graph; different input is rejected. `join_policy="all_settled"`
allows a report to summarize known failed/canceled branches once every child is
terminal. It never advances past unresolved model fees or unknown effects.
The default remains `all_success`. Previously persisted list-shaped fork requests
retain their idempotency identity under the default policy.

## Declared child dependencies

ChildTask accepts `depends_on=(0, 1)` to refer to earlier children in the same
fork batch. Declarations must be topologically ordered: forward references,
self-dependencies and duplicate edges are rejected before graph commit.
`dependency_policy` defaults to `all_success`; `all_settled` permits known failed
or canceled predecessors, never unresolved work. Dependencies do not reserve
model budget or occupy an execution attempt while blocked.

A blocked child persists WAITING_DEPENDENCIES on its original model step.
When predecessors meet the policy, one committed `dependency_results` input is
added and that same step becomes READY. Effects and attempts retain their own
identities. `runtime.refresh(parent_id)` resolves dependency and join facts
without dispatching any model or tool. Host workers can then schedule newly
READY children using their frozen ports; admission waits are retried on the next
explicit wake, not by an unbounded busy loop.

QuantLab sequential roles depend on all prior role outcomes. Debate critics
read the snapshot from before their round; same-round critics cannot see one
another's new opinions. These declarations, role bindings, rounds and system
messages are committed up front. Host reconstruction never recompiles a live
plan from editable role definitions. Existing graph/fork identity checks retain
compatibility with the previous no-dependency request shape.
