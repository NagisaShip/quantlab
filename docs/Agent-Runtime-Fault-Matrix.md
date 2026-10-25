# Agent Runtime 故障与迁移验收矩阵

本矩阵覆盖内核首版的状态、租约、信号、模型费用、外部效果、恢复及宿主迁移边界。测试使用确定性模型与工具；HTTP/UI 验收使用本地模型服务，通过真实 ModelHub、Runtime、研究室 API 和浏览器运行。它不证明任意第三方工具支持幂等，也不承诺外部效果恰好一次。

## 内核与持久化

`packages/agent-runtime/tests/test_engine.py`、`test_process_crash.py`、`test_fault_matrix.py` 在配置 `QAGENT_TEST_PG` 时，将同一组行为分别运行于 SQLite 与独立 PostgreSQL schema。每个 PostgreSQL 用例结束后删除自己的临时 schema。

| 注入位置 / 故障 | 必须保持的结果 | 对应测试 |
| --- | --- | --- |
| 模型请求中直接退出 Python | 租约过期后等待费用核对，不能盲目重发 | `test_killed_model_worker_waits_for_usage_before_regeneration` |
| 外部工具已成功，提交前退出 Python | 稳定 effect_id；核对后继续，不重新发布 | `test_killed_tool_worker_reconciles_external_effect_without_republishing` |
| 等待审批时退出 Python | 新进程接收原步骤审批信号并完成 | `test_killed_approval_wait_resumes_from_durable_signal` |
| 工具成功后本地事务提交失败 | 结果保持不确定，外部效果不能盲重放 | `test_external_success_then_database_commit_failure_does_not_replay` |
| 旧 worker 与新 worker 竞争 | 旧 epoch 的结果无法覆盖新结果；重试复用 effect_id | `test_expired_worker_cannot_commit_after_takeover` |
| 租约过期但还没有接管者 | 原 worker 同样不能提交过期结果 | `test_expired_lease_rejects_result_even_without_a_successor` |
| 模型调用长于两次租约 | 心跳续租，竞争者不派发第二次请求 | `test_long_call_renews_lease_and_competitor_cannot_claim` |
| 多工具批次中途取消 | 未派发步骤取消，已派发效果等待核对 | `test_cancel_batch_prevents_remaining_tools_and_reconciles_only_dispatched_effect` |
| 取消后模型返回 | 迟到输出不进入已取消的执行状态 | `test_cancel_fences_late_model_result` |
| 人工确认效果成功 / 不存在 | 持久幂等信号；成功不重做，不存在才允许重试 | `test_manual_effect_resolution_is_a_durable_idempotent_signal` |
| read_only 但没有安全重试承诺 | 暂停核对，声明只读本身不构成自动重试许可 | `test_read_only_without_retry_contract_pauses` |
| 审批参数摘要不匹配 | 拒绝，原审批目标及状态不改变 | `test_approval_survives_reopen_and_signal_is_idempotent` |
| 授权后暂停直至审批过期 | 派发前重新审批，过期授权不产生外部效果 | `test_expired_approval_requires_a_fresh_grant_before_dispatch` |
| 相同信号 ID 变更身份、参数或目标运行 | 拒绝冲突；完全相同的重放无副作用 | `test_signal_identity_cannot_be_reused_for_another_actor_payload_or_run` |
| 用户输入等待后重启 | 使用原模型上下文和原步骤，信号重放安全 | `test_input_wait_survives_reopen_and_keeps_initial_context` |
| 批次超出硬步骤上限 | 在创建任何工具效果前整体拒绝该批次 | `test_batch_step_limit_rejects_all_tools_before_any_effect` |
| 单次模型返回工具数超出本轮工具预算 | 不让多调用响应绕过宿主硬上限，整批工具均不派发 | `test_tool_budget_cannot_be_exceeded_by_a_single_multi_call_response` |
| 父子 / 共享预算竞争 | 同一账本原子预留，不复制可用额度 | `test_shared_budget_atomic_reservations` |
| 模型正文已确认但用量缺失 | 保留正文、预算不释放；确认费用后无需重新生成 | `test_confirmed_model_output_waits_for_usage_then_completes_without_regeneration` |
| 派发前模型硬配额或配置拒绝 | 明确失败并释放预算，不误判外部结果不确定 | `test_pre_dispatch_model_rejection_fails_and_releases_budget` |
| 旧检查点 / 校验和损坏 | 不能覆盖新事件；可退回已验证检查点或完整日志 | `test_stale_or_corrupt_checkpoint_cannot_override_new_events` |
| 压缩检查点后最新快照损坏 | 保留回退快照；执行事件不删除，序号连续 | `test_checkpoint_compaction_retains_fallback_and_all_execution_facts` |
| 状态表与事件不一致 | 停止推进，不能从不一致记录派发动作 | `test_state_event_divergence_stops_execution` |
| 8 个独立执行进程、4 路并发共用 PG schema | 完成状态、事件游标与审计一致 | `tests/test_runtime_postgres.py` |

## ModelHub 与宿主边界

| 故障 / 场景 | 必须保持的结果 | 测试文件 |
| --- | --- | --- |
| 并发争抢 provider、成员或工作区额度 | 已用 + 未结算预留 + 新请求原子约束 | `tests/test_model_admission.py` |
| 并发争抢模型执行槽 | 工作区默认 8 槽，未知结果继续占槽；响应已结束但费用未结算时仅保留预算 | `tests/test_model_admission.py` |
| 费用估计不足、价格配置随后变更 | 记录超额，按准入时价格结算 | `tests/test_model_admission.py` |
| 跨 UTC 午夜延迟结算 | 计入准入当天，不占用次日已预留额度 | `tests/test_model_admission.py` |
| 用量计数器写入失败 | 用量、admission 与工作区计数全部回滚 | `tests/test_model_admission.py` |
| 旧 Router 与 Runtime 同时调用 | 共用 ModelHub 预留，成功只写一条用量 | `tests/test_runtime_model_adapter.py`、`tests/test_model_admission.py` |
| 顶层 ticket 提交失败 / 旧 worker 认领 | ticket 和兼容 Run 同事务；旧 worker 无执行状态写权 | `tests/test_runtime_bridge.py` |
| 队首全在等待审批 | 不占执行槽，不阻塞后续 READY 任务 | `tests/test_runtime_app_wiring.py` |

## 研究室、界面与第二宿主

| 场景 | 验收结果 | 测试文件 |
| --- | --- | --- |
| 同一模型响应多个工具 | 每个工具独立 Step / Attempt / effect_id，全部完成后再请求模型 | `tests/test_lab_runtime.py` |
| 重建研究室实例后批准或回答 | 沿用原运行、原上下文和原步骤 | `tests/test_lab_runtime.py`、`tests/test_lab_runtime_api.py` |
| 工具执行后结果丢失 | 新研究室实例不重新执行该工具 | `tests/test_lab_runtime.py` |
| 分叉对话 | 新分支不继承执行实例或可执行的旧审批卡片 | `tests/test_lab_runtime.py` |
| 编辑历史 / 删除项目时仍有未确认效果 | 旧审批先取消；未知外部效果阻止重试和删除，避免绕开效果身份 | `tests/test_lab_runtime.py`、`tests/test_lab_runtime_api.py` |
| 只请求恢复、没有新的用户回答 | 不把旧提示词误当成 ask_user 的回答 | `tests/test_lab_runtime.py` |
| 事件流分批补读与重连 | 数据库游标递增、无重复缺口 | `tests/test_lab_runtime_api.py` |
| 状态快照与事件读取之间发生新提交 | 游标不越过状态快照，下一次补读仍获得新事件 | `tests/test_lab_runtime_api.py` |
| 普通成员裁决效果或账单 | HTTP 403，执行状态不改变 | `tests/test_lab_runtime_api.py` |
| 把另一对话 run_id 填入查询 / 裁决请求 | 项目及租户校验拒绝，原运行保持不变 | `tests/test_lab_runtime_api.py` |
| 浏览器核对未计费用量 | 展示已保存回答、去除生成状态，实际模型请求次数仍为 1 | `tests/e2e/19-runtime-recovery.spec.ts` |
| 通用 Agent 与 Team 共用执行适配 | 策略生成、研究摘要和定时 Agent 自动继承宿主 Journal；保留 trace，恢复不重做工具，隔离工作区与用户 | `tests/test_generic_agent_runtime.py` |
| 文档检查步骤恢复 | 从已提交预览结果重建图片输入，不重跑文档生成工具 | `tests/test_generic_agent_runtime.py` |
| 独立安装包、CLI 暂停后新进程恢复 | 不导入 QuantLab 业务代码和配置 | `packages/agent-runtime/tests/test_independent_host.py` |

本次本地验证：双数据库内核与 PG 多进程共 **55 项通过**；开启 Runtime 的策略、因子、模型、组合和执行研究室回归 **89 项通过**，另有 3 项 PyTorch 模型训练测试因环境未安装该可选依赖而跳过。默认配置全量后端回归 **1507 项通过、9 项跳过**；收尾综合矩阵（含 PostgreSQL）85 项通过；最后的单次派发与上下文持久化调整之后，再跑 SQLite 内核、桥接、模型适配、研究室 HTTP、Team、额度和 sidecar 包装共 64 项，全部通过。以上为不同测试选择的独立结果，不相加为唯一用例数。浏览器恢复验收通过，截图已检查，确认恢复后没有遗留的“生成中”或“执行失败”投影。CI 已加入独立包安装、开启 Runtime 的研究室回归、PG 故障矩阵及浏览器恢复用例。

## 运行

```powershell
python -m pip install -e ./packages/agent-runtime
python -m pytest packages/agent-runtime/tests tests/test_lab_runtime.py tests/test_lab_runtime_api.py tests/test_model_admission.py -q

# 使用独立测试数据库；矩阵仅创建和删除自己随机命名的 schema。
$env:QAGENT_TEST_PG = 'postgresql://USER:PASSWORD@127.0.0.1:5432/TEST_DATABASE'
python -m pytest packages/agent-runtime/tests tests/test_runtime_postgres.py -q

$env:QAGENT_AGENT_RUNTIME = '1'
python -m pytest tests/test_strategy_lab.py tests/test_factor_research.py tests/test_model_research.py tests/test_portfolio.py tests/test_execution.py -q
npx playwright test tests/e2e/19-runtime-recovery.spec.ts --workers=1
```

边界：本矩阵覆盖交互对话迁移与首版故障保障。高级公平调度、完整 OpenTelemetry 和通用 DAG / Join 属于 RFC 中的后续 P1/P2 范围。

最后入口迁移验证：开启 Runtime 的研究室及通用 Agent 回归 93 项通过、3 项可选训练测试跳过；审批有效期与入口迁移定向验收 50 项通过；最终 SQLite/PostgreSQL 内核、独立进程及通用 Agent 矩阵 60 项通过；文档交付和工作流兼容测试 33 项通过、1 项跳过。各组选择有重叠，不合并计数。Docker 镜像与源码安装命令均显式安装独立 Runtime 包。

## 可观测性故障边界

`packages/agent-runtime/tests/test_telemetry.py` 同时覆盖 SQLite 与可选 PostgreSQL：导出拒绝或抛错不确认游标、不改变执行状态；采集器重建后补发未确认跨度；外部导出成功但本地确认提交失败时使用相同 trace/span ID 重发；真实 OpenTelemetry SDK 内存导出器及指标采集验证；重复采集指标不重复计费。跨度投递为至少一次，不承诺采集端恰好一次。

`tests/test_lab_runtime_api.py` 验证管理员指标 API 的工作区隔离、普通成员拒绝访问及不泄露对话正文。当前跨度为按 Run 关联的平面 Attempt 跨度；首 token、完整父子跨度树与外部 provider 分布式上下文仍需后续接入，不能据此宣称完整遥测已经完成。

可观测性阶段验收：SQLite/PostgreSQL 内核、进程故障、宿主接线及 HTTP 指标验收共 74 项通过；新增只读快照保障后，双数据库遥测用例 10 项通过。指标读取使用只读一致性事务，不获取执行全局写锁；数据库拒绝在该快照内写入执行状态。

## 父子任务与显式 Join

`packages/agent-runtime/tests/test_children.py` 在 SQLite 与 PostgreSQL 共用以下故障实验：

| 场景 | 约束 |
| --- | --- |
| 两个 Worker 同时派生相同请求 | 一组稳定子任务身份，不重复创建 |
| 派生事务中断 | 子任务、父任务等待点和 Join 全部回滚 |
| 两个子任务争抢父预算 | 同一账本原子预留；不会各自复制余额 |
| 子任务进程提交结果后直接退出 | 父任务重启后沿用已提交结果，按声明顺序 Join |
| 子任务失败 | 默认 all-success Join 不宣布父任务成功 |
| 嵌套子任务外部效果未知时取消根任务 | 整棵树保留 CANCELING，核对后才逐层终止 |
| Join 成员缺失、父子跨租户或循环 | 停止推进，不误判完成或跨边界取消 |
| 子任务尝试绕过祖先数量/深度限制 | 拒绝且不创建部分任务 |
| 子任务系统消息与图片 | 持久化并由恢复后的宿主重新读取 |

父子任务定向双数据库实验最初 32 项通过，随后补充冷启动、取消后的模型费用预留和终态复活防护用例。独立 CLI 已实际跨进程从 WAITING 恢复为 COMPLETED。该 API 仍由宿主选择子任务端口并调度；该轮验收尚未包含 QuantLab Team 外层；后续并行 Team 接入的证据见下节。

并发冷启动验收：取消父进程预先建表后，4 个真实 PostgreSQL Worker 同时首次初始化曾复现系统目录唯一键冲突。已将 schema 与全部 DDL 放在同一 advisory-lock 事务中；修复后 PostgreSQL 3 项验收通过。综合内核/宿主回归 124 项通过；收尾增加的 Join 终态防护另跑双数据库定向矩阵，38 项通过。


## 并行 Team 产品接入（2026-10-25）

- 内核图创建与 Join 策略：`test_initial_graph_is_atomic_and_repeated_creation_keeps_original_children`、`test_all_settled_join_waits_for_unknown_child_and_then_includes_known_failure`、`test_legacy_join_request_retries_keep_the_same_identity`。
- Team 宿主：`tests/test_team_structured_runtime.py` 验证重建宿主、已完成角色不重复调用、未知工具效果不重放、角色定义变更仍沿用冻结配置、取消保留未知效果、已知失败可以进入汇总、索引丢失可重建，以及汇总截断不修改持久结果。
- HTTP：`tests/test_team_runtime_api.py` 验证完成与等待运行的幂等 resume、普通成员不能借原发起人身份继续/取消，以及只读视图中的管理权限。
- 浏览器：`tests/e2e/20-team-runtime.spec.ts` 使用真实本地 HTTP 模型服务、真实 ModelHub 与数据库。等待时继续、取消、刷新，模型调用始终为 1；等待核对不能标成“已生成”，页头与评审卡片均跟随权威状态。截图保留在 E2E 产物目录。
- 状态判定：`tests/agent-status.test.mjs` 新增 Team 等待/取消/完成证据测试；状态单测 7 项通过。

双数据库内核与宿主/API 综合回归 129 项通过，开启 Runtime 的策略研究室与 Team 定向回归 57 项通过，最后的适配和上下文裁剪定向验收 13 项通过。浏览器主对话恢复与并行 Team 验收 2 项通过；前端构建和翻译提取检查通过。各测试选择有重叠，不累加为唯一用例数。当前产品接入仅覆盖 parallel 外层，不能宣称 sequential/debate 外层已经完成迁移。


## 顺序与辩论模式（2026-10-25）

`packages/agent-runtime/tests/test_dependencies.py` 共用 SQLite/PostgreSQL：依赖满足前零模型派发；重建宿主后读取同一前序结果；自环、前向和重复边使整图回滚；all_success/all_settled 区分已知失败与未知结果；边丢失安全停止；重复 refresh 不追加第二份上下文；并发 Worker 只提交一次依赖结果；取消后不解锁未开始的角色；旧图创建请求保持身份兼容。

`tests/test_team_modes_runtime.py` 验证真实 Team 顺序、跨轮观点和同轮快照隔离；未知工具效果中断后重建 TeamRunner，完成核对后继续原角色，不重跑已发生效果。`tests/test_team_runtime_api.py` 通过 HTTP 验证顺序和辩论完成后的重复 resume 无额外模型调用。

`packages/agent-runtime/tests/test_journal_startup.py` 注入 SQLite BUSY、LOCKED 和 CORRUPT：只重试锁竞争，不掩盖损坏。已有四宿主并发冷启动验收继续保留。

双数据库内核、Team 模式及 HTTP 综合回归 154 项通过；开启 Runtime 的研究室/Team 回归 59 项通过。最后补充的请求兼容、取消展示与浏览器模式矩阵另列最终结果，测试选择存在重叠，不累加计数。

顺序/辩论阶段收尾：兼容请求、依赖等待、SQLite 冷启动、Team 模式及 HTTP 定向验收 39 项通过；浏览器主对话与三种 Team 模式共 4 项通过。截图已确认等待前序、等待核对及取消状态展示；未开始的角色不显示虚构的模型名称。前端构建、类型和翻译检查通过。


## Team 人工核对（2026-10-25）

`tests/test_team_recovery_api.py` 验证真实 HTTP 权限和账本：已保存模型输出核对后不重新生成；取消后确认未计费不重新派发；跨执行图与非法用量不改变账本；其他管理员可确认但不能继续原发起人的个人模型；普通成员看不到工具参数且不能核对；效果信号重复幂等、内容冲突拒绝。额外注入 ModelHub 已提交、Runtime 尚未提交的故障，重试仅补交内核状态，费用与模型调用均不重复。

`tests/e2e/20-team-runtime.spec.ts` 为三种 Team 模式增加卡片内人工核对验收：取消中确认未计费后进入已取消，刷新仍一致，模型调用保持一次。主对话原有恢复用例继续覆盖共享表单。

本阶段宿主与 API 回归 39 项通过，状态单测 7 项通过；主对话与 Team 浏览器 4 项通过，补强页头/消息标签同步断言后三种 Team 模式再验收 3 项通过。Windows 前端构建、类型检查、Ruff 与翻译提取检查通过。本机首次浏览器运行因系统代理导致本地模型请求 502，设置本地地址 NO_PROXY 后完成真实调用验收；未放宽调用次数断言。


## 跨度树与首 token 故障验证（2026-10-25）

`packages/agent-runtime/tests/test_telemetry.py` 新增 SQLite/PostgreSQL 共用验证：父任务未完成时先导出子跨度，恢复后使用原标识补齐整树；批次上限不改变标识；错误租户或非根入口拒绝；外部导出成功但 ACK 事务失败后补发相同 parent context；真实 OpenTelemetry SDK 验证 Run/Step/Attempt 关系。正文、参数和输出不进入遥测。

首 token 验证跨线程回调、重复增量只计一次、非流式及已完成后的迟到回调不写、失效租约不写、采集器重启与租户隔离。宿主协议验证 OpenAI-compatible、Responses 与 Anthropic 流式增量，最终响应模拟 UI delta 不计首 token，观察器存储失败不破坏模型结果。

双数据库内核/父子/依赖回归 120 项通过，故障矩阵与协议综合回归 64 项通过；后续补强成功终态断言及跨线程验证，遥测专项双数据库 22 项通过，协议与研究室接口综合 47 项通过。各选择有重叠，不累加为唯一用例数。Ruff 通过。provider HTTP trace-context 传播仍未接线，不能据此宣称整个重构完成。


## provider 请求追踪关联（2026-10-25）

内核验证恢复后的子 Agent 实时 trace/span 身份与持久跨度导出一致；模型结果不确定，经人工费用核对再生成时，trace ID 不变、Attempt span ID 改变。宿主通过三种真实协议的 HTTP MockTransport 检查 traceparent；同一客户端后续普通调用无追踪头，两线程并发调用各带自己的头。双数据库内核/崩溃恢复与宿主协议/API 综合回归 91 项通过；补充重新生成关联测试的结果另列。权限审计另发现来源私有会话的 Team 可见范围缺口，尚待修复，不宣称整体完成。


## 评审来源权限（2026-10-25）

`tests/test_team_visibility_api.py` 对 Runtime 开启和旧执行器分别验证新评审的来源权限：私有会话详情与评审详情均拒绝未授权成员；列表不泄露议题；Runtime 恢复与核对视图拒绝；删掉 JSON 索引后从权威图重建仍保留限制；改为工作区或指定群组共享后可读；撤回共享或移除群成员后再次拒绝；来源项目缺失不默认公开。所有只读访问不增加模型调用。

相关后端回归 68 项通过；Runtime 开启后的研究室、Team 与协作回归 85 项通过；三种 Team 浏览器 3 项通过；补充群组成员撤销后权限专项 3 项通过。历史记录若从未保存来源字段，仍需独立迁移或安全降级验证，当前测试不证明这类历史来源完整。


## 临时准入等待与取消（2026-10-25）

`test_temporary_admission_survives_restart_without_busy_loop_or_double_reserve` 在 SQLite/PG 验证冷却窗口内重复 resume 不增加事件或 Attempt，重建宿主后沿用原 Step，额度可用时创建新 Attempt，Run 预算预留与结算各计一次。`test_cancel_admission_wait_releases_unspent_budget` 覆盖等待中取消及取消与准入拒绝的竞态，仍只接受有效租约/epoch 提交“未派发”事实。工具误抛模型准入异常仍走效果核对，不据此自动重试。

ModelHub HTTP 接入验证角色与汇总两种额度等待均展示 queued，未获许可时零 provider 调用、无模型账本占用，释放后只调用尚未完成的阶段；研究室无需人工费用核对即可恢复。Worker 测试在 33 个审批/冷却等待任务之前仍调度后续 READY，不先占执行槽。实际 SDK 将 NOT_ADMITTED 记录为 UNSET，不误报 provider 执行失败。

`tests/e2e/21-model-capacity.spec.ts` 限制真实 ModelHub 为一个槽：第一条本地 HTTP 模型请求保持未结束，第二个会话进入 WAITING_ADMISSION；UI 显示排队和恢复按钮，无费用核对表单或发送新消息的误导入口；释放首请求后，第二条从原 Step 完成，合计 provider 调用恰好两次。测试保留无需刷新即可看到回答的断言，复现并修复了恢复后未重建事件流跟随的问题。

双数据库内核、依赖/父子、故障矩阵、ModelHub 与宿主准入综合回归 167 项通过；Runtime 开启后的宿主/API 回归 48 项通过；新增遥测语义与跨数据库收尾 64 项通过，状态单测 8 项通过。前端构建、类型、Ruff 与翻译抽取通过。浏览器主对话、三种 Team 模式和真实单槽准入共 5 项通过；最后补强的内核工具异常/失效租约边界双数据库 40 项通过。测试选择有重叠，不累加为唯一覆盖数。


## 历史来源与安全降级（2026-10-25）

`tests/test_team_visibility_api.py` 使用生成时即缺少来源字段的旧形状，不篡改已提交 Runtime 计划来模拟历史记录。覆盖 SQLite 与 PostgreSQL：旧执行器与 Runtime 评审来源迁移；清空消息、删除索引、重建策略后仍受原会话 ACL 约束；确认来源后动态共享与撤回；后创建的共享分叉不冒充原始来源；多个来源要求全部可见；来源缺失不公开；来源未知 Runtime 仅原发起人/管理员可读，旧 JSON 无作者证据时仅管理员可读。读取不触发模型执行，冻结 Runtime 状态和计划保持不变。

证据写入事务失败后整组来源回滚，不能留下仅含公开来源的子集；重复迁移无重复行。已发现的来源关联独立于可重建 JSON 索引保存。PostgreSQL 旧查询的 LIKE 百分号解析问题已由这组真实接口测试复现并修复。

SQLite 相关迁移/Team/研究室/协作及存储回归 84 项通过，PostgreSQL 组合回归修复后 36 项通过。后补充事务回滚后，权限专项 SQLite 与 PostgreSQL 分别 11 项通过。测试选择有重叠，不累加。


## 原子 ticket 准入与基础轮转（2026-10-25）

`tests/test_runtime_admission.py`：8 个并发连接分别争抢工作区在途、Runtime 积压或每日唯一名额，只产生一组 Run/ticket/event；并发同幂等键在所有额度满后仍返回相同 ticket；取消后释放积压但不重置日创建次数；等锁跨日按实际准入日检查；非法规格和优先级不占队列。4 个独立 Python 进程共享数据库争抢单名额验证跨进程边界，SQLite 与 PostgreSQL 分别运行。

Worker 通过真实队列行和受控执行池验证单槽跨工作区轮转、多槽先分配到不同工作区、区内优先级与 FIFO。已有等待 33 项审批/冷却的验收扩大至人工核对模型，后续 READY 仍可获槽。真实 Worker 单 Agent 执行测试继续保留。调度顺序测试不被当作模型执行结果或跨 Worker 全局公平性的证明。

SQLite 宿主、队列、Worker、运行 API 与配额综合回归 78 项通过、1 项跳过；新增参数边界及真实多进程后 PostgreSQL 最终组合 21 项通过。Ruff 通过。所有选择有重叠，不累加为覆盖率；全局共享资源及其他直接入口仍需审计。


## 共享工具名额（2026-10-25）

`packages/agent-runtime/tests/test_scheduler.py` 共用 SQLite/PostgreSQL 验证：两个宿主实例争抢全局名额；租户默认与持久覆盖互不混淆；不同工具名共享冻结类别；取消、租约过期及已返回但未确认的外部效果仍占逻辑名额；核对后恢复原 Step/effect_id；等待资源不延长审批；共享策略缺失时停止派发。4 个真实 Python 进程同时争抢单名额，只有一个产生工具收据，其余保持零工具 Attempt；重建宿主后可逐个完成。

宿主 HTTP 验证研究室和 Team 文档工具同受类别限制：额度为零不派发，不重跑已完成模型；恢复后沿用原工具步骤。AppServices 重建不覆盖既有策略。状态测试验证等待工具空闲不会显示费用核对。原模型冷却测试改为显式固定在重试时间之前，避免 PostgreSQL 请求耗时超过 1 秒后合法重试被误判为重复尝试。

双数据库内核、父子/依赖和故障综合回归 164 项通过；后续策略缺失收尾另有专项。宿主 PG 组合 36 项通过；主对话、三种 Team 与模型准入浏览器回归 5 项通过；状态测试 9 项、Windows 前端构建、Ruff 与翻译抽取通过。各选择有重叠，不相加。逻辑名额不被当作外部物理进程强制终止的证据。


共享策略收尾专项：默认策略行缺失时，即使重启也不重新放宽为默认额度；未知版本拒绝派发。策略专项与冷启动双数据库 15 项通过。取消场景现检查 Attempt 已结束为 UNKNOWN、未知 Step 仍占名额；历史收尾修复验证重复打开不追加第二份事件、原计划不变、工具零执行及跨度可导出。孤立 Attempt 或计数与已提交事件不符时，修复和执行均拒绝。取消/故障及冷启动定向双数据库 75 项通过；最后的完整身份审计回归结果另列。

最终身份审计：SQLite/PostgreSQL 内核 Scheduler、engine、故障矩阵、真实进程崩溃、父子与遥测回归 152 项通过。补充未知策略版本后，Scheduler 与 PostgreSQL 研究室/Team/通用 Agent 集成回归 40 项通过。以上为有重叠的定向证据，不替代最终全量验收。

## 生命周期指标验收（2026-10-25）

`test_durable_wait_retry_and_cancellation_metrics` 在 SQLite/PostgreSQL 通过真实模型准入等待、冷却重试、结果未知、取消与人工费用核对，核对已知时钟下每段等待和取消时长。重建 Journal、重复 resume、重复取消均不重复计数；不同租户读不到数据；读取不触发模型执行。现有真实 OpenTelemetry SDK 验证新增 gauge 的值，全部标签保持有限类别。遥测与父子/依赖综合回归 98 项通过；新增 Join 等待专项结果另列。

Join 等待补充修正测试对既有模型边界的假设后，SQLite/PostgreSQL 遥测专项 32 项全部通过；验证 WAITING_CHILDREN 计入自己的等待状态，不误作模型额度等待。Ruff 通过。
