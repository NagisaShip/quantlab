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
| 派发前模型准入失败 | 明确失败并释放预算，不误判外部结果不确定 | `test_pre_dispatch_model_rejection_fails_and_releases_budget` |
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

父子任务定向双数据库实验最初 32 项通过，随后补充冷启动、取消后的模型费用预留和终态复活防护用例。独立 CLI 已实际跨进程从 WAITING 恢复为 COMPLETED。该 API 仍由宿主选择子任务端口并调度；QuantLab Team 的外层编排尚未切入这条显式 Join 路径，不能把内核契约验收等同于产品迁移完成。

并发冷启动验收：取消父进程预先建表后，4 个真实 PostgreSQL Worker 同时首次初始化曾复现系统目录唯一键冲突。已将 schema 与全部 DDL 放在同一 advisory-lock 事务中；修复后 PostgreSQL 3 项验收通过。综合内核/宿主回归 124 项通过；收尾增加的 Join 终态防护另跑双数据库定向矩阵，38 项通过。
