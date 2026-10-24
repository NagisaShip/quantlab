# Agent Runtime 核心架构提案（审阅稿）

状态：首版内核、研究室交互主对话迁移与故障验收已落地。独立内核位于 `packages/agent-runtime`；设置 `QAGENT_AGENT_RUNTIME=1` 后，Team 角色循环、单角色试跑、研究室团队评审及交互主对话均使用 Runtime。默认开关仍关闭，保留分阶段启用策略；通用 `Agent` 也继承宿主 Journal，覆盖策略生成、研究摘要和定时 Agent；后续 P1/P2 能力仍按下文分阶段实现。

## 核心决定

建立独立、可嵌入的 Python Agent Runtime 包。它负责 Agent 执行状态、持久化、调度、模型请求准入、工具副作用、人工中断、恢复和事件流；产品通过适配器提供模型、工具、存储、身份和界面。QuantLab 是首个宿主，另做一个最小宿主验证复用。

Agent run 类比进程，并行子任务类比协程；LLM 调用更像受限的远程加速器请求，工具调用更像有副作用的 I/O。调度和抢占发生在步骤边界。已发出的模型请求可以尝试取消，但不承诺从中间 token 恢复。

| 操作系统概念 | 内核对应 | 类比的边界 |
| --- | --- | --- |
| 进程控制块 | 持久化 Run 与 AgentSpec 快照 | 状态跨进程重启，不依赖原 Python 栈 |
| 线程/协程 | Step 与父子任务树 | 是逻辑并行，未必占一个系统线程 |
| 就绪队列 | READY 步骤队列 | 等模型、审批和计时器时不参与竞争 |
| CPU/设备配额 | 模型及工具准入预算 | 外部 provider 的实际算力不可由宿主抢占 |
| 中断/系统调用 | 工具结果、人工输入、超时和取消信号 | 以持久事件推进，而非内存回调 |

## 业界取舍

| 参考 | 借鉴 | 不直接采用的部分 |
| --- | --- | --- |
| [OpenAI Agents SDK](https://developers.openai.com/api/docs/guides/agents/running-agents) | 模型、工具、handoff、审批和结果构成统一循环 | 不依赖其提供跨产品任务队列和额度仲裁 |
| [Temporal](https://docs.temporal.io/tasks) | 持久任务、事件历史、worker 认领、活动边界与故障恢复 | 桌面版不强制部署 Temporal 服务；首版不要求确定性代码全量重放 |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/persistence) | 检查点、线程内状态与跨线程记忆分离、人工中断 | 不要求所有动态 Agent 流程先写成 DAG |
| [vLLM](https://docs.vllm.ai/en/stable/api/vllm/config/scheduler/) | 准入、并发、排队上限和预算是明确策略 | token 级 GPU 调度归推理服务负责 |

QuantLab 现有 platform/runs.py 与 platform/worker.py 已有顶层队列、租约、槽位和检查点；llm/router.py 与 llm/hub.py 已有模型降级和额度记录。但 agents/base.py 与 agents/team.py 各自执行模型—工具循环，状态、事件和恢复边界不统一。新内核应整合这些语义，不另起一套与现有平台竞争的顶层队列。

## P0：唯一执行状态权威

一个外部 run_id 只对应一个 Runtime 执行实例。Agent run 进入内核后，**Runtime 是该逻辑运行的执行状态、完成判定、取消结果和恢复决策的唯一写入权威**。QuantLab RunService 保留顶层入队、租户入口与既有 API，但其 Agent 状态字段只是 Runtime 状态的兼容投影，不再独立推进 Agent 的执行状态。其他非 Agent run 继续由原 RunService 管理。

兼容投影不得反向驱动内核，也不得宣布内核尚未确认的完成。投影可以延迟，但带来源 event_seq 与投影时间；API 对需要强一致的状态从 Runtime 权威记录读取。映射示例：WAITING_APPROVAL 在旧 API 可显示为 PAUSED，却不占 worker 执行槽；IN_FLIGHT 工具取消时显示“取消中”，只有效果确认或安全终止后才进入终态；旧租约失效由 Runtime 重新认领，RunService 只负责唤醒入口。这样明确消除两套状态机争夺同一个 run 的控制权。

QuantLab 桥接时必须把现有 RunService 的“排队认领”与“写 runs.state / lease_owner”拆开：Agent 类型使用独立的入口 ticket（只表示待交付、已认领、已交付），认领 ticket 不授予 Agent 执行状态写权；交付后由 Runtime 自己认领可执行步骤。既有 runs 行只作为外部 ID 与 API 投影，投影器单向读取 Runtime 事件更新它。旧 Worker 不得再认领 Agent 类型。这样保留顶层排队入口，同时避免旧 claim、heartbeat、set_state 与新内核并发改同一逻辑状态。

## 包边界

建议从第一天将公共包放在 packages/agent-runtime，分为 contracts、engine、scheduler、journal、effects、telemetry。公共包不得导入 quant_agent，不含交易或前端术语。首版提供 SQLite 持久化；PostgreSQL 多 worker 在 MVP 稳定后实现。QuantLab 的 RunService、ModelRouter、ModelHub、MCP/本地工具、权限是宿主适配器。

逻辑上分三层：宿主提供身份、权限、模型配置、工具实现、UI 和业务资源；Runtime 是执行**控制面**，其 Engine 决定状态转换与恢复、Scheduler 决定何时执行、Journal 保证事务事实、Effects 处理外部效果与审批；模型 SDK/API、MCP/沙箱和 SQLite/PostgreSQL 属于**执行面**或持久化实现。Contracts 规定边界，Telemetry 横贯各层。首版可在一个 Python 进程内运行，不要求微服务。

公共 API 首版保持小：start、signal、resume、get、events、register_model、register_tool。所有写操作携带调用方身份和幂等键。signal 还包含稳定 signal_id、目标 run/step 与预期版本；审批、取消和外部回调通过持久信号进入内核。resume 只表示“请求唤醒既有运行”，不由 API 调用方直接恢复 Python 栈；重复 resume 安全，多个 worker 争抢仍由认领协议和 fencing token 决定唯一有效提交者。模型、工具、存储与时钟均是可替换端口；不绑定单一 SDK 或云服务。

内核只接收明确的能力对象，不直接访问宿主全局变量。端口至少包括 ModelPort、ToolPort、JournalPort、ArtifactPort、IdentityPort、ClockPort。每个端口声明版本；事件采用向后兼容的 schema 版本。宿主可以选择自己的模型 SDK，但它的会话 ID 只能作为 provider 私有引用保存，不能代替内核的 run ID 与事件历史。

## P0：Run / Step / Attempt 分层

1. AgentSpec：版本化指令、模型策略、工具能力、预算、步骤上限和输出契约。创建 run 时固定版本和配置摘要。
2. Run：run_id、租户、会话、父 run、状态、截止时间、预算和检查点。会话保存长期上下文；run 代表一次任务。
3. Step：model、tool、handoff、approval、timer、join、final。逻辑步骤有稳定 step_id；每次实际派发创建新的 attempt_id。并行任务形成父子树并显式 join。同一逻辑外部操作的 effect_id 跨 attempt 保持稳定。
4. Event：按 run 单调递增的序号记录转移、请求、结果、用量、产物引用和失败。检查点只保存恢复所需的精简状态，大内容和文件保存不可变引用。

Run 级只表达聚合状态：QUEUED、RUNNING、WAITING、RECOVERING、CANCELING、COMPLETED、FAILED、CANCELED。它由内部 Step、父子依赖和未决外部效果计算，不能由 API 或 Worker 随意设置终态。Step 级状态为 READY、WAITING_ADMISSION、IN_FLIGHT、RECONCILING、WAITING_APPROVAL、WAITING_TIMER、SUCCEEDED、FAILED、CANCELED。Attempt 记录派发、租约、请求 ID、响应和错误；重试创建新 attempt，不改变 step_id 和 effect_id。

WAITING_ADMISSION 未占模型并发许可；IN_FLIGHT 已向外部发出请求并持有许可；RECONCILING 表示结果不确定或需外部查询。等待人工审批、计时器或 provider 回调时不占普通 Worker 执行槽。流式 token 是临时视图，完整结果提交后才是可恢复事实。

状态更新、事件追加、检查点和待执行动作的 outbox 在同一事务提交。worker 认领使用租约与递增 fencing token；过期 worker 不能写入内核数据库。这个围栏**不能阻止已被外部系统接受的操作**，其安全性必须由下文的 effect 协议保障。

## P1：执行事实、事件与快照

采用“事务性状态表为权威执行状态，同事务追加不可变事件”的模式；不采用首版全量纯事件溯源。Event Journal 记录已提交的变化与执行事实；Checkpoint 是某个 event_seq 对应的恢复加速摘要；Projection 是可重建的 API、前端与统计视图。状态表和事件必须在一个事务内提交，禁止只写一边。

恢复时先读取权威状态、最近有效 Checkpoint，再按序应用快照之后已提交的事件：recovered_state = fold(checkpoint_state, events[checkpoint_seq+1..last_seq])。这里重放的是**状态转换**，不重跑旧 Python Agent 代码，也不再次调用已确认完成的工具。若快照版本、校验和或序号不匹配，将其丢弃并从较早有效快照或权威事务记录重建；过期快照不得覆盖新事件。Projection 落后只影响查询新鲜度，不决定执行。

恢复计算结果要与权威状态表的版本和摘要核对。若状态表与已提交事件出现无法由正常事务解释的分歧，停止该 run 的自动执行并报警、保留证据；不任意选择一边覆盖另一边。首版不要求能从事件单独重建全部业务数据，但要求所有影响后续执行的事实可核验。

## 两级调度

第一级调度 READY 步骤。MVP 使用简单优先级队列、租户并发上限与基本轮转；必须具备背压、队列上限和取消。加权公平、优先级老化、复杂吞吐优化留待有真实负载数据后实施。子任务继承父任务预算约束，而不是复制父任务可用金额字段。

第二级是资源准入：模型调用按 provider/model/账号控制并发、请求速率、估计 token 与金额；工具按能力类别限制并发。内核定义 AdmissionPort，但**一个调用只能有一个准入与费用账本权威**。QuantLab 初期由 ModelHub 适配器担任权威；Runtime 模型适配器单次调用，既有 ModelRouter 的每次实际 provider 尝试也先向 ModelHub 原子预留，再按结果结算或保留未结算占用。未来若跨产品共享额度，再迁移实现，不改变 AdmissionPort 契约。

统一预算账本满足 available = limit - spent - reserved。层级可含 provider 账户、租户、用户和单 Run；一次请求必须在**同一事务**对所有适用层级原子预留，失败则全部不预留。并发子任务共用父 Run 的余额约束，不能各自读取并复制剩余额度。完成后用实际用量结算，释放差额；估算不足需按策略阻止继续生成、补充预留或记录超额，不得静默突破硬限额。取消未确认、provider 延迟回执或 usage 缺失时保留未结算项，不提前释放 reserved；通过回调、查询、上限估计和人工对账关闭。账本记录 request_id、step_id、attempt_id、币种/单位、估算、实用量、来源与结算状态。

MVP 必须有基本硬预算和准入；高级熔断、复杂模型动态降级优先沿用宿主已有实现。路由、重试和 Retry-After 决策作为可审计事件；模型价格和策略由宿主提供，不写死在内核。

运行中的生成不做时间片抢占。停止时向 provider 请求取消；若无法确认取消或请求结果，模型步骤进入 RECONCILING，费用预留保持未结算。模型生成结果不确定时允许按预算上限和明确策略重新生成，但保留原请求可能收费、生成结果不同的事实；高风险工具不能套用这条模型重试规则。

## P0：外部效果、Fencing 边界、安全和审批

工具声明 read_only、idempotent_write 或 external_effect，以及作用域、超时、输入输出 schema、能力、审批策略和重试策略。read_only 只是声明，**不是自动重试许可**：读取也可能依赖波动数据或隐藏副作用。内核按经审核的工具契约决定重试。

一次逻辑操作先持久化稳定 effect_id、参数摘要、外部幂等键和意图，再通过 outbox 派发；所有 attempt 重用同一个 effect_id。已派发但未确认的操作恢复时先进入 RECONCILING。数据库 fencing token 只隔离旧 worker 的数据库写入，不隔离外部服务。处理矩阵如下：

| 外部操作 | 结果不确定时 |
| --- | --- |
| 经审核确认为无副作用、可安全重复的读取 | 可自动重试，同时记录数据版本或观测时间 |
| 外部支持幂等键的写入 | 用同一 effect_id 对应的键重试或查询 |
| 外部支持按请求 ID 查询状态的写入 | 先查询并核对，再决定是否重试 |
| 无幂等、无核对能力的写入 | 暂停为 UNKNOWN_EFFECT，等待人工判定，不自动重放 |
| 模型生成 | 单独保留费用/输出不确定状态；只在预算与策略允许时重新生成 |

下单、支付、发布、删除等高风险效果默认走保守策略；外部成功但数据库提交失败必须在测试中验证不会盲重放。内核承诺可恢复和可审计，**不承诺跨独立系统的恰好一次效果**。[Temporal 官方 MCP 集成说明](https://docs.temporal.io/develop/python/integrations/mcp)也明确指出，worker 丢失已完成工具的响应时，活动可能再次执行，因此有副作用工具必须设计幂等性。

审批绑定精确的工具名、参数摘要、运行身份与有效期；参数变动须重新审批。模型输出和 MCP 返回值视为不可信输入。密钥不进入提示词、事件或检查点；工具在宿主指定的权限与沙箱内运行。审计记录与用户可见事件分开，敏感字段可脱敏并有保留期。

## 可观测性与产物

统一 run_id、step_id、attempt_id、trace_id，输出结构化事件；MVP 先保证基本指标与关联 ID，完整 OpenTelemetry 在后续阶段补齐。客户端重连以数据库 event_seq 为准：使用“先订阅并缓冲，再补读游标之后的历史，最后按序去重应用”的协议，或由服务端提供原子游标订阅。不得采用存在空窗的“先读完历史，再建立实时订阅”。测量排队、首 token、生成、工具、重试、费用、取消和恢复。

产物是独立记录，包含类型、哈希、来源步骤、预览图、下载位置和保留策略。Word/PPT 渲染、截图和视觉检查是普通工具步骤与产物，而非 Agent 循环中的特殊分支。

## MVP 范围与 QuantLab 迁移

首版目标是“小而可靠的 Agent 执行内核”，不是通用工作流平台。P0 范围：独立包与端口、单 Agent 模型/工具循环、Run/Step/Attempt、SQLite 持久状态与事件、租约和恢复、幂等信号、审批、效果记录与对账、取消、基本硬预算、背压和第二宿主。PostgreSQL 多 worker、Team 并行及显式 join、完整 OpenTelemetry 为 P1。加权公平、优先级老化、高级熔断和复杂调度优化为 P2；已有模型降级先由宿主提供。

| 阶段 | 工作 | 退出条件 |
| --- | --- | --- |
| 0. 基线 | 固定 Agent、Team、Lab 的输入输出与事件样本，测量延迟、成本和失败率 | 有回归样本和性能基线 |
| 1. 内核 MVP | 唯一状态权威、Run/Step/Attempt、SQLite 状态与日志、效果协议、基本准入；用模拟模型和工具故障注入 | 下方实验 A/B 通过 |
| 2. 第二宿主 | 独立 CLI 安装公共包，用自己的 SQLite、模拟模型和计算器工具，暂停后退出并重启恢复 | 下方实验 C 通过，且无 QuantLab 业务依赖 |
| 3. QuantLab 桥接 | RunService 保留顶层排队和兼容投影；Runtime 决定 Agent 执行状态；ModelHub 适配 AdmissionPort 并补原子账本 | 单 Agent 端到端运行，旧 API 与历史仍可读 |
| 4. 扩展迁移 | 迁移 Team、Lab 长任务和文档产物；再扩展 PostgreSQL 多 worker、并行 join 和完整遥测 | 对照输出、用量和事件，无明显回归 |

创建 run 时固定 runtime_version：旧 run 继续由旧执行器处理，或通过显式转换器恢复；新 run 可按租户灰度进入新内核。运行中不自动升级。数据库迁移只追加字段和表，保留既有 run ID 与读取接口；入口可回滚。桥接时应先定义“RunService 只负责入口与投影”的写权限限制，再迁移 Agent 类型，避免双写窗口。

## 三项决定性验收实验

**A. 崩溃恢复。** Agent 分别在等待模型、工具和人工审批时被强制终止 Python 进程，重启后从已提交事实恢复。完成的步骤不再执行，未完成步骤按状态进入重试或核对，事件序号连续。证明生命周期不依赖 Python 栈。

**B. 外部效果故障注入。** 工具已成功在外部产生效果，随后人为制造本地提交失败、租约过期和 worker 争抢。支持幂等键的工具仅产生一次效果；无幂等也无法核对的操作安全暂停。再验证旧 fencing token 无法写库，但不会被误认为能撤销外部效果。

**C. 脱离 QuantLab。** 独立 CLI 宿主安装公共包，用自己的 SQLite、模拟模型与计算器工具完成多步任务，暂停审批，结束进程，再启动恢复。全程不导入 QuantLab 设置、数据库表、用户系统或模型路由。证明是语义复用而非目录拆分。

## 其他验收门槛

- 同一外部 run_id 只有一个 Agent 执行状态权威；旧 API 投影不得先于内核宣布终态；审批等待不占执行槽位。
- Step 的 WAITING_ADMISSION、IN_FLIGHT、RECONCILING 在资源占用和恢复行为上有独立测试；attempt_id 变化时 effect_id 稳定。
- 共享父子预算并发预留不能超额；模型取消未确认和 usage 缺失时保留未结算账项；单一调用不能双重预留或双重记账。
- 权威状态与事件同事务，旧快照不能覆盖新事件；重连无事件空窗，重复 signal/resume 不重复推进。
- 审批恢复时工具参数与签核记录一致；取消传播到子任务和可取消请求；产物与截图可追溯到步骤，跨租户读取被拒绝。
- PostgreSQL 多 worker 阶段沿用 SQLite 的行为测试，并补多 worker 竞争测试。

## 本次请审阅的架构决定

实施记录（2026-10-24）：已建立独立包、SQLite Run/Step/Attempt/Effect 与预算表、同事务事件、可丢弃检查点及状态审计、稳定 effect_id、审批与取消信号、结果不明时的对账入口。模拟模型/工具和安装后的独立 CLI 已跨进程完成暂停恢复，正式包安装测试通过；真实子进程强杀实验覆盖模型请求、外部工具效果和审批等待，核对或信号处理后可从新进程恢复，事件序号连续。另有故障注入覆盖外部成功但本地提交失败以及旧 worker 失去租约。QuantLab 已加入 opt-in 入口 ticket、单向投影及 worker 推进：ticket 与旧 runs 行同事务创建，旧 Worker 不能认领带 runtime_version 的运行；详情走 Runtime 强读，列表及旧事件流由游标投影刷新，取消走持久信号。设置 `QAGENT_AGENT_RUNTIME=1` 后，可通过 `POST /api/runs/runtime-agent` 提交单 Agent 模型任务；审批等待与有效租约中的步骤不占 worker 槽位，队首等待审批也不阻塞后续可执行任务。ModelHub 已增加事务性预留、未结算与幂等用量结算，且准入时冻结计费价格。Runtime 模型适配器对选定 provider 仅调用一次；既有 ModelRouter 的每次 provider 尝试也使用同一 ModelHub 账本，成功后不会再调用旧用量记录入口。模型返回正文但用量缺失时，Runtime 保留输出并等待费用核对；QuantLab 适配器先确认 ModelHub 用量，再结算内核预算，重复确认不会二次记账。桌面 sidecar 构建已显式收集独立 Runtime 包，冻结后端冒烟会以 `QAGENT_AGENT_RUNTIME=1` 启动验证；当前仍通过 opt-in 开关启用，Team/Lab 的迁移情况见下文；工作区每日 LLM 次数/token 配额已并入 ModelHub 同一事务：准入时同时检查已结算用量、在途预留与本次估算，结算时在写入 `llm_usage` 和更新 admission 状态的同一事务中累加工作区计数器，重复结算不会二次计量；Runtime 与旧 ModelRouter 共用该账本；准入拒绝被标记为派发前确定性失败，Runtime 不再误入 RECONCILING，并释放内核预算预留。TeamRunner 在 Runtime 开关开启时把每个角色的模型—工具循环交给共享 Journal，同时保留 sequential / parallel / debate 编排；Team API、单角色试跑以及研究室发起的团队评审都走该路径。Journal 现支持 PostgreSQL 独立 schema、BIGINT 时间戳和事务级 advisory lock，已用临时 PostgreSQL 16 实例完成 8 个独立 Runtime 进程、4 路并发共享 schema 的真实验证，事件序号连续且审计一致。研究室交互式主对话现已迁移到 Runtime：上下文与图片持久化、多工具独立步骤、审批和用户输入信号、停止与费用核对均沿用同一执行状态。检查点默认保留最近两份，压缩不删除执行事件。SQLite 与真实 PostgreSQL 共用的内核故障及多进程验证共 55 项通过；开启 Runtime 的各研究室回归 89 项通过，3 项 PyTorch 可选训练用例跳过。故障范围、测试映射与复现命令见 [故障验收矩阵](Agent-Runtime-Fault-Matrix.md)。

每日配额补充：请求的预留与最终计量统一归属准入时的 UTC 日期。跨午夜返回的结果结算到原日期，不挤占次日的新请求额度；准入事务使用同一个时间戳确定日桶与 admission 身份。模型用量、结算状态及工作区计数器保持同事务提交，计数器写入失败时全部回滚。旧 Router 的重复每日只读检查已移除，实际派发统一经过 ModelHub 准入。

1. 从第一天建独立 Python 包，以依赖边界证明跨产品复用。
2. 对 Agent run，Runtime 是唯一执行状态权威；QuantLab RunService 只提供顶层入口和兼容投影。
3. 采用 Run/Step/Attempt/Effect 分层，以及事务性状态表、同步事件日志与可丢弃快照；不重放 Python Agent 代码。
4. Fencing 只保护内核数据库；外部效果按能力对账或安全暂停，模型不确定结果走独立费用策略。
5. AdmissionPort 从第一天定义，QuantLab ModelHub 先作为唯一准入和账本实现并补原子预留。
6. MVP 聚焦恢复、审批、副作用、基本预算和第二宿主；复杂公平调度与多 worker 后置。

最长期的原则是：Agent 可以提出行动，Runtime 必须保证行动受到正确管理。由于外部模型和工具不与内核共享事务，它更接近分布式执行系统，而非可完全控制的单机操作系统。首版不重写 GPU 推理调度、不承诺 token 级抢占、不承诺所有外部效果恰好一次，也不强制所有 Agent 用 DAG 描述。Temporal、LangGraph 或特定模型 SDK 可在未来通过适配器接入。

通用入口收尾（2026-10-24）：`Agent.run` 与 Team 共用 `runtime_agent` 适配；开启开关后，宿主 Router 注入同一 Journal，已构建的 Agent 也可使用。`resume_runtime` 检查工作区、用户、Agent 身份和绑定后唤醒原执行实例，已提交工具不重做。通用 trace 从已提交步骤重建；文档检查图片从持久化预览结果重建。工具审批授权默认有效 24 小时（由 `ToolSpec.approval_ttl_ms` 配置），超过有效期仍未派发则重新审批。

可观测性实施：独立包增加只读 RuntimeTelemetry 投影、带持久确认的至少一次跨度导出和可选 OpenTelemetry SDK 适配；宿主指定 exporter 与 meter。运行、步骤、尝试、耗时和确认费用从权威记录观察，采集器重启不重放计数。QuantLab 管理员可读取当前工作区聚合指标。当前为平面 Attempt 跨度，完整父子跨度、首 token 与 provider 上下文传播尚未完成。

父子任务实施：内核提供原子 fork_children、持久 WAITING_CHILDREN、显式 all-success Join、children 恢复发现、完整子任务初始消息和祖先预算账户继承。取消在同一事务传播整棵树，未知效果继续等待核对；子任务数量、后代总数和深度服从所有祖先限制。SQLite/PostgreSQL 定向故障实验与真实进程退出恢复已验证。Team 外层编排接入和任意依赖 DAG 尚未完成。审计仅做一致性读取，已改为只读事务，避免父任务写事务内审计子任务时重复争抢写锁。
