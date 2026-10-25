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


## Hierarchical traces and first-token facts

`telemetry.export_trace(sink, root_run_id=root, consumer_id="tree-collector", tenant_id=tenant, limit=100)`
exports completed Run, Step and Attempt spans from one authoritative root tree.
Run/Step/Attempt IDs and parent contexts are stable across collector restart;
child runs attach to the parent's persisted Join step. Parent spans can arrive
later than completed children while a root is still running. A failed sink or
failed acknowledgement commit redelivers identical span IDs without executing
Agent work. Use this instead of the legacy flat `export_attempts` for new
collectors, with its own consumer ID. Batch size is bounded; each call scans one
bounded execution tree. No prompts, arguments, output or provider secrets are
included. Exporters are host supplied and no global OpenTelemetry provider is installed.

Inside `ModelPort.generate`, capture `agent_runtime.observations.first_token_observer()`
and invoke it upon the first nonempty streaming content, reasoning or tool delta.
The callback can be carried to a streaming reader thread. It records once per
Attempt and checks the current lease and Run state; late/expired callbacks cannot
write observations. Observation storage failures do not fail the model call.
Nonstreaming responses retain an unknown first-token time, including providers
that emulate UI deltas after receiving the complete response. QuantLab's three
provider protocols report actual streaming deltas only and never force streaming
for otherwise nonstreaming requests.

Durable first-token count and total duration are available in `snapshot` and
OpenTelemetry observable gauges; attempt spans include `agent_runtime.model.first_token_ms`
only when observed. Collection and restart cannot increment these totals.
`current_model_trace()` exposes the current model Attempt's immutable trace context
inside generate. QuantLab adds its W3C `traceparent` to each provider request,
without baggage or user data. Restored children use the root trace identity;
regeneration uses a new Attempt span. Cached clients never retain per-request
headers. A custom threaded transport should capture the context explicitly before
handing off work. Core execution remains independent of OpenTelemetry SDKs.


## Temporary model admission waits

A ModelPort that definitively has not dispatched a request may raise
`ModelAdmissionPending(retry_after_ms=1000)`. Runtime preserves the logical Step,
records the rejected admission Attempt as NOT_ADMITTED, clears its lease and
persists WAITING_ADMISSION with a retry deadline. Resume before that deadline
has no effect. A later wake creates a new Attempt on the same Step; Run budget
reservation is retained once, not duplicated. Cancel releases the unspent
reservation. An admission rejection that arrives during cancellation can close
that canceled model step only while the original lease and epoch are valid.
Expired workers still cannot write.

Use ModelUnavailable for definitive pre-dispatch failures (such as missing
configuration or a hard quota denial), not temporary capacity pressure. Never
raise ModelAdmissionPending after a provider request was dispatched: uncertain
requests require reconciliation. Tools cannot use this model exception to make
an unknown external effect automatically retryable.

QuantLab ModelHub remains the single actual provider admission authority. It
marks only concurrent-capacity exhaustion as retryable; it creates no admission
row and sends no request for that denial. Worker polling skips retry deadlines
without claiming an execution slot. Research conversations and Team cards expose
queued status and an explicit resume action; they do not ask for billing review.


## Shared tool resource admission

All Runtime hosts using the same Journal share `RuntimeScheduler` limits.
Defaults are 32 managed tool operations globally and 8 per tenant. Model
requests remain under the host's AdmissionPort/ModelHub authority; this scheduler
does not reserve model fees or create a second model ledger.

```python
from agent_runtime import RuntimeScheduler
scheduler = RuntimeScheduler(journal)
scheduler.set_limit("tools", 16)                          # global aggregate
scheduler.set_limit("tools", 4, tenant_default=True)      # default per tenant
scheduler.set_limit("tools", 6, tenant_id="workspace-a") # tenant override
scheduler.set_limit("class:office", 2)                   # optional capability pool
```

By default each tool also has identity `tool:NAME`. A host may freeze a shared
capability pool in `AgentSpec.metadata["tool_resource_classes"]`, for example
`{"render_word": "class:office", "render_ppt": "class:office"}`. Global aggregate,
tenant aggregate and applicable class limits are all checked atomically in the
same write transaction as the Step lease and effect-dispatch record. Changing a
limit affects new dispatches; it does not terminate remote operations. Capacity
zero pauses new operations. `if_absent=True` seeds a host default without replacing
operator configuration on restart. Required aggregate policy missing from storage
stops dispatch rather than treating capacity as unlimited.

A denied tool stays WAITING_ADMISSION with a durable retry time, no Attempt and
no external dispatch. Cancel before dispatch releases that queued intent. Active
and RECONCILING tool Steps retain logical permits, including after lease expiry
or a cancellation whose external effect is not confirmed. Only a committed
terminal step or verified absent effect frees that permit. A contractually safe
retry reuses its own Step's logical permit and stable effect ID. This bounds
Runtime-managed logical operations; it cannot prove an external process stopped
or impose physical concurrency on systems outside Runtime's control.

QuantLab freezes document tool classes for generic Agents, Team roles and the
research conversation. Its initial shared class limits are 2 document previews
and 4 document-authoring operations. Historical specs without class metadata
retain name-based identity; aggregate limits still apply. Scheduler policy is
stored in the Journal and is immediately shared across API/Worker processes.


Cancellation ends a fenced Attempt's managed lifetime as UNKNOWN while its Step
and external effect can remain RECONCILING. The permit remains held until the
external fact is resolved. This is not confirmation of remote cancellation.
On Journal open, legacy unclosed Attempts whose committed Steps are no longer
IN_FLIGHT are audited and closed from their recorded transition boundary.
Repair adds an interruption event, never calls a model/tool, never modifies the
frozen plan, Step, effect or budget, and is idempotent. Runs with mismatching
Attempt identities/counts or state/event facts are not repaired or executed.

## 生命周期指标口径（2026-10-25）

只读快照补齐原方案指标：`waiting` 按步骤种类和状态记录连续等待区间数量、总毫秒数及当前等待数，覆盖 READY、额度、审批、输入、计时、子任务、依赖和结果核对；活动区间计算到采集时刻，重复同状态事件不拆分区间。生成/工具耗时沿用已结束 Attempt 的时长，首 token 单独统计。`retries` 是同一逻辑 Step 超过第一次的 Attempt 数，包含未获模型准入后的再申请，不等于 provider 实际请求数；重新生成的新 Step 可由费用核对事件与跨度关联辨认。

`cancellation` 按 Run 首次 CANCELING（直接取消则 CANCELED）起算，分别输出请求数、完成数、完成耗时及未确认取消的当前时长；根与子 Run 分别计量。`recoveries` 仅计已提交的租约失效、效果核对与模型费用核对事实，幂等 resume 或只读查询不伪装恢复次数。所有值均可从持久记录重新计算，按租户隔离；OpenTelemetry 使用有限种类/状态标签，不把 Run ID、参数或正文作为指标标签。快照用于运维采集，不参与执行或计费。

## 可追溯交付物（2026-10-25）

`ArtifactRef` 定义宿主管理的存储键、SHA-256、字节数、名称、媒体类型、来源及可选过期时间。工具结果的 `_runtime_artifacts` 清单与原 Step/effect 结果同事务提交；非法清单保持结果未知，不将外部效果误判为未执行。`RuntimeArtifacts(journal).list(run_id, tenant_id=...)` 只投影已成功提交的工具步骤，补充稳定产物 ID、Run/Step/effect 身份；不导入宿主、不读取文件、不宣称引用指向的字节仍可用。采集不执行模型/工具，过期也不删除执行证据。

QuantLab 本地文档适配器为正式导出与页面预览工具建立内容寻址快照，存储在项目私有数据目录；覆盖用户原文件不会改变旧交付。下载仍检查原目录授权、Run 租户和所有者/管理员，校验长度及 SHA-256 后返回字节。快照保留 30 天；过期即拒绝下载，`POST /api/local/runtime-artifacts/cleanup` 只删除达到期限、名称匹配且不为符号链接的缓存，保留原文件和 Journal 引用。清理接口不被描述为后台定时清理。账户部署不开放本地快照。宿主可以替换成自己的对象存储与保留策略，无需修改内核。
