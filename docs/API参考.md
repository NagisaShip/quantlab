# QuantLab v0.1.0-preview.2 · API 参考

[English](API-Reference.en.md) | [文档索引](README.md) | [返回 README](../README.md)

平台共 **723 个端点**。本文件由 `scripts/gen_api_doc.py` 从 FastAPI 的 OpenAPI 规范生成，
与运行中服务的 `/docs`（可交互调试）来自同一份定义。

机器可读版本：`docs/openapi.json`（同一次生成）。

## 通用约定

- 所有接口前缀为 `/api`；除登录、注册、`/api/health`、`/api/healthz` 外都需要登录（Cookie `qa_session` 或 `Authorization: Bearer`）。
- 写操作（POST/PUT/PATCH/DELETE）必须带 `X-Requested-With`（防 CSRF）；工作区由 `X-Workspace-Id` 指定，服务端会核对成员关系，审阅者只读。
- 写操作可带 `Idempotency-Key`：24 小时内同一工作区同一键重放第一次的响应，不会重复创建任务。
- 错误统一为 `{code, message, trace_id, details}`，`code` 取值见 `quant_agent/platform/errors.py`。
- 金额、数量、价格在 JSON 中是**十进制字符串**（例如 `"1009.79"`），不要用浮点数解析后再参与记账。
- 回测、参数优化、实时模拟、账户同步都是 **run**：API 只入队，worker 执行。提交接口在短时间内完成时直接返回结果，
  否则返回 `202 + run_id`，用 `GET /api/runs/{id}`、`/api/runs/{id}/events?after=` 或 WebSocket `/api/runs/{id}/ws` 跟踪；
  控制命令 `POST /api/runs/{id}/commands`（pause / resume / cancel / liquidate_and_stop）。

## 账号与工作区

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/auth/account` | 账号资料：用户名、显示名、绑定的邮箱与是否已验证 |
| `PUT` | `/api/auth/account` | 修改用户名 / 显示名 |
| `POST` | `/api/auth/email/code` | 给要绑定的邮箱发验证码（60 秒冷却，每小时 5 次） |
| `POST` | `/api/auth/email/verify` | 填验证码完成绑定（之后可以用这个邮箱登录） |
| `POST` | `/api/auth/login` | 登录，建立服务端会话 |
| `POST` | `/api/auth/logout` | 退出登录，作废会话 |
| `GET` | `/api/auth/me` | 当前用户、可访问的工作区与角色 |
| `POST` | `/api/auth/password` | 修改密码（作废全部会话） |
| `POST` | `/api/auth/quantlab-id/rotate` | 销毁并重新生成 QuantLab UID |
| `POST` | `/api/auth/register` | 注册（首个用户成为管理员，之后凭邀请码） |
| `GET` | `/api/auth/status` | 部署状态（是否已初始化、是否开放注册） |
| `GET` | `/api/auth/totp` | 双因素认证状态（不回显密钥） |
| `POST` | `/api/auth/totp/disable` | 关闭双因素认证（需密码 + 验证码 / 恢复码） |
| `POST` | `/api/auth/totp/enable` | 用验证码确认启用，并一次性返回恢复码 |
| `POST` | `/api/auth/totp/recovery-codes` | 重新生成一次性恢复码（旧的作废） |
| `POST` | `/api/auth/totp/setup` | 开始启用双因素认证：返回 otpauth 链接与密钥（仅此一次） |
| `GET` | `/api/workspace` | 当前工作区详情与设置 |
| `GET` | `/api/workspace/audit` | 审计日志（管理员） |
| `POST` | `/api/workspace/audit/archive` | 按保留期归档过期审计记录（管理员） |
| `GET` | `/api/workspace/audit/archived` | 已归档的审计记录（管理员） |
| `GET` | `/api/workspace/audit/export` | 导出审计日志为 CSV（管理员，单元格做公式中和） |
| `GET` | `/api/workspace/features` | 工作区功能开关与实现状态 |
| `PUT` | `/api/workspace/features` | 管理员动态开关现货/永续/真实资金等能力 |
| `POST` | `/api/workspace/invites` | 创建邀请码（管理员） |
| `DELETE` | `/api/workspace/invites/{invite_id}` | 作废邀请码（管理员） |
| `GET` | `/api/workspace/members` | 成员与邀请 |
| `DELETE` | `/api/workspace/members/{user_id}` | 移除成员（管理员） |
| `PUT` | `/api/workspace/members/{user_id}` | 修改成员角色（管理员） |
| `PUT` | `/api/workspace/settings` | 修改工作区名称、时区与运行额度（管理员） |
| `GET` | `/api/workspace/storage` | 工作区存储分类、磁盘占用与保留策略 |
| `POST` | `/api/workspace/storage/cleanup` | 按分类清理超过保留期的数据（默认只预览） |
| `GET` | `/api/workspaces` | 我所在的工作区 |
| `POST` | `/api/workspaces` | 新建工作区 |
| `POST` | `/api/workspaces/join` | 凭邀请码加入工作区 |

## 运行与任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/runs` | 运行列表（回测、优化、实时模拟等所有长任务） |
| `POST` | `/api/runs/runtime-agent` | 提交一个 opt-in Agent Runtime 任务 |
| `GET` | `/api/runs/runtime-metrics` | 查询当前工作区的 Runtime 执行指标 |
| `GET` | `/api/runs/workers` | 在线 worker 与唤醒方式 |
| `GET` | `/api/runs/{run_id}` | 运行详情：状态、进度、租约、命令 |
| `POST` | `/api/runs/{run_id}/commands` | 发送运行命令（暂停、恢复、取消、模拟平仓并结束、立即采集） |
| `GET` | `/api/runs/{run_id}/events` | 运行事件流（游标分页） |
| `GET` | `/api/runs/{run_id}/fills` | 运行的成交记录（十进制字符串） |
| `GET` | `/api/runs/{run_id}/nav` | 运行的净值点（含单位净值与份额） |
| `GET` | `/api/runs/{run_id}/result` | 运行结果（已完成的回测；含落盘格式与校验和引用） |
| `GET` | `/api/runs/{run_id}/series` | 分页读取运行结果里的大数组（逐 bar 净值、信号、成交等） |
| `GET` | `/api/runs/{run_id}/signals` | 运行的信号与状态 |

## 规则 DSL 与策略版本

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/dsl/from-code` | 代码识别规则：用 AST 比较判断代码是否仍是 @rule 描述的规则 |
| `POST` | `/api/dsl/rank` | 预览多标的轮动的名次与目标权重（等权 / 按得分加权，同分按标的代码字典序） |
| `GET` | `/api/dsl/schema` | 规则 DSL 的 JSON Schema、指标与示例（表单与 JSON 视图共用） |
| `POST` | `/api/dsl/strategies` | 新建或保存 DSL 策略（保存即登记不可改写的版本） |
| `POST` | `/api/dsl/strategies/{slug}/to-code` | 把 DSL 规则策略转成代码策略（生成带 @rule 注释的 Python） |
| `POST` | `/api/dsl/to-code` | 规则生成代码：输出带 @rule / @param 中文注释的 Python 策略 |
| `POST` | `/api/dsl/validate` | 逐项校验规则 DSL（字段路径 + 说明、预热长度、中文描述） |
| `GET` | `/api/strategies/{slug}/versions` | 策略版本列表（草稿 / 已校验 / 已冻结 / 已归档） |
| `GET` | `/api/strategy-versions/{version_id}` | 取回某个策略版本的确切内容 |
| `POST` | `/api/strategy-versions/{version_id}/archive` | 归档策略版本（不再允许新的运行） |
| `POST` | `/api/strategy-versions/{version_id}/freeze` | 冻结策略版本（进入实时模拟的前提） |

## 研究中心

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/research/backtest-retention` | 当前成员的回测记录保留设置与占用 |
| `PUT` | `/api/research/backtest-retention` | 保存当前成员的回测自动清理设置 |
| `POST` | `/api/research/backtest-retention/cleanup` | 立即清理当前成员超出的最旧未 PIN 回测记录 |
| `PUT` | `/api/research/backtest-retention/workspace` | 设置每个成员可保留的回测记录总数（工作区管理员） |
| `GET` | `/api/research/backtests` | 回测记录列表 |
| `POST` | `/api/research/backtests` | 运行回测（persist=false 即编译运行） |
| `POST` | `/api/research/backtests/batch-delete` | 批量删除回测记录（跳过 PIN 保护或无权删除的记录） |
| `POST` | `/api/research/backtests/compare` | 多次回测对比 |
| `POST` | `/api/research/backtests/package/inspect` | 校验研究包 |
| `POST` | `/api/research/backtests/package/replay` | 重放研究包并比对哈希 |
| `DELETE` | `/api/research/backtests/{bt_id}` | 删除回测记录 |
| `GET` | `/api/research/backtests/{bt_id}` | 回测详情（收益概述、交易、持仓、日志、清单） |
| `GET` | `/api/research/backtests/{bt_id}/package` | 下载可复现研究包 |
| `POST` | `/api/research/backtests/{bt_id}/pin` | 切换回测记录的 PIN 保护 |
| `GET` | `/api/research/compare-batches` | 策略对比批次列表 |
| `POST` | `/api/research/compare-batches` | 创建策略对比批次：冻结标的、区间、资金与成本 |
| `DELETE` | `/api/research/compare-batches/{batch_id}` | 删除策略对比批次（回测记录保留） |
| `GET` | `/api/research/compare-batches/{batch_id}` | 策略对比批次详情：曲线与指标并排，含压力回测 |
| `POST` | `/api/research/compare-batches/{batch_id}/results` | 登记对比批次里某个策略的回测结果 |
| `GET` | `/api/research/jobs/{job_id}` | 后台任务进度 |
| `GET` | `/api/research/optimizations` | 参数优化记录列表 |
| `GET` | `/api/research/optimizations/{opt_id}` | 参数优化结果与过拟合诊断 |
| `POST` | `/api/research/optimize` | 启动参数优化（样本内 / 样本外，后台任务） |
| `POST` | `/api/research/pycode` | 研究产物 → Python 代码：因子（独立可运行，结果与平台一致）、模型 / 组合 / 执行（规格 + 复现代码） |
| `GET` | `/api/research/record-retention` | 统一研究记录占用、各类型倍率与保留设置 |
| `PUT` | `/api/research/record-retention` | 保存当前成员的研究记录自动清理设置 |
| `POST` | `/api/research/record-retention/cleanup` | 按记录单位上限立即清理最旧的未 PIN 记录 |
| `PUT` | `/api/research/record-retention/multipliers` | 设置各类研究记录的存储倍率（工作区管理员） |
| `GET` | `/api/research/record-retention/records` | 统一记录列表（当前成员） |
| `POST` | `/api/research/record-retention/records/batch-delete` | 批量删除研究记录及报告 / 结果文件 |
| `POST` | `/api/research/record-retention/records/batch-pin` | 批量 PIN / 取消 PIN 研究记录 |
| `PUT` | `/api/research/record-retention/workspace` | 设置每个成员的记录单位上限（工作区管理员） |
| `DELETE` | `/api/research/record-retention/{type}/{record_id}` | 删除任意自己的研究记录（PIN 需先取消） |
| `POST` | `/api/research/record-retention/{type}/{record_id}/pin` | 切换任意研究记录的 PIN 保护 |
| `POST` | `/api/research/strategies` | 新建策略（从模板或代码，同名不覆盖） |
| `POST` | `/api/research/strategies/{slug}/apply-params` | 把一组参数写回策略（生成新版本） |
| `POST` | `/api/research/strategies/{slug}/clone` | 克隆策略 |
| `GET` | `/api/research/strategies/{slug}/params` | 读取策略声明的 PARAMS |
| `GET` | `/api/research/templates` | SDK 策略模板库 |
| `GET` | `/api/research/templates/{template_id}` | 模板源码与参数 |

## 策略工作室

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/studio/backtest` | 跑一份策略源码，返回与平台同口径的回测结果 |
| `GET` | `/api/studio/exchanges/capabilities` | Binance / OKX 能力矩阵 |
| `POST` | `/api/studio/order` | 向交易所模拟盘提交订单（接口联调，仅模拟盘） |
| `POST` | `/api/studio/packages/import` | 导入策略包（导入后需人工批准才能运行） |
| `POST` | `/api/studio/packages/inspect` | 检查策略包：格式、安全检查、重跑标准答案 |
| `GET` | `/api/studio/paper` | 实时模拟实验列表 |
| `POST` | `/api/studio/paper/start` | 启动实时模拟（需已批准的策略版本） |
| `DELETE` | `/api/studio/paper/{session_id}` | 删除实时模拟实验 |
| `POST` | `/api/studio/paper/{session_id}/liquidate` | 模拟平仓并结束 |
| `POST` | `/api/studio/paper/{session_id}/pause` | 暂停实时模拟（不调用策略、不新增成交） |
| `POST` | `/api/studio/paper/{session_id}/recover` | 模拟服务重启恢复（标记中断） |
| `GET` | `/api/studio/paper/{session_id}/report` | 实时模拟报告（含 7/30 天检查点进度） |
| `POST` | `/api/studio/paper/{session_id}/resume` | 恢复实时模拟 |
| `POST` | `/api/studio/paper/{session_id}/stop` | 停止实时模拟（不自动平仓） |
| `GET` | `/api/studio/paper/{session_id}/terminal` | 交易终端数据：会话、K 线、成交、委托、行情状态 |
| `POST` | `/api/studio/paper/{session_id}/tick` | 立即采集一次行情：新 bar 上运行策略、撮合、估值 |
| `POST` | `/api/studio/paper/{session_id}/unhalt` | 解除交易所模拟盘熔断（需要人工确认） |
| `POST` | `/api/studio/reconcile` | 逐笔对照：模拟成交 vs 实盘成交，五层差异归因 |
| `POST` | `/api/studio/rules/preview-python` | 预览规则编译出的 Python 源码 |
| `GET` | `/api/studio/strategies` | 策略文件列表（每个策略 = 一个目录 + 一个 .py） |
| `POST` | `/api/studio/strategies` | 新建或保存策略源码（保存前做静态检查） |
| `POST` | `/api/studio/strategies/from-rule` | 把 JSON 规则编译成 Python 策略并落盘 |
| `DELETE` | `/api/studio/strategies/{slug}` | 删除策略目录 |
| `GET` | `/api/studio/strategies/{slug}` | 读取策略源码、元数据与历史版本 |
| `POST` | `/api/studio/strategies/{slug}/approve` | 人工批准策略版本 |
| `POST` | `/api/studio/strategies/{slug}/check` | 静态检查与试加载 |
| `GET` | `/api/studio/strategies/{slug}/export` | 导出可独立运行的 .py |
| `GET` | `/api/studio/strategies/{slug}/package` | 导出策略包（.qstrat） |
| `GET` | `/api/studio/strategies/{slug}/spec` | 读取策略说明书（SPEC.md） |
| `PUT` | `/api/studio/strategies/{slug}/spec` | 保存策略说明书（SPEC.md） |
| `GET` | `/api/studio/strategies/{slug}/versions/{filename}` | 读取某个历史版本的源码 |

## 真实记录与个人账户

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/accounts/connections` | 只读账户连接列表（只含掩码与 connection_id） |
| `POST` | `/api/accounts/connections` | 新建只读账户连接（检测时间、身份与权限，非只读 Key 拒绝） |
| `DELETE` | `/api/accounts/connections/{cid}` | 撤销连接（删除平台保存的加密凭证） |
| `POST` | `/api/accounts/connections/{cid}/sync` | 同步余额与成交（worker 执行） |
| `POST` | `/api/accounts/connections/{cid}/test` | 重新检测连接权限 |
| `GET` | `/api/accounts/connections/{cid}/watermarks` | 成交同步水位（每个交易对的分页游标、累计条数与最近错误） |
| `GET` | `/api/accounts/guide` | 个人账户只读接入教程 |
| `GET` | `/api/comparisons` | 实盘对照报告列表 |
| `POST` | `/api/comparisons` | 生成实盘对照报告（逐笔归因，带置信度） |
| `GET` | `/api/comparisons/{cid}` | 实盘对照报告详情 |
| `POST` | `/api/copy/exit-plan` | 源策略关闭时按 exit 处置生成平仓计划 |
| `POST` | `/api/copy/plan` | 按跟随账户权益折算跟单目标仓位（三种跟随模式） |
| `GET` | `/api/copy/policy` | 跟单模式与源策略关闭处置的可选项 |
| `GET` | `/api/copy/report` | 跟随账户业绩（只用跟随账户自己的成交与资金流） |
| `GET` | `/api/envelopes/schema` | 目标仓位信封（TargetPositionEnvelope）协议 |
| `POST` | `/api/envelopes/validate` | 按执行器规则校验信封（过期 / 重复 / 乱序拒绝） |
| `GET` | `/api/imports` | 导入记录列表 |
| `POST` | `/api/imports` | 导入真实记录 CSV（成交 / 资金流水 / 余额，全有或全无） |
| `GET` | `/api/imports/spec` | 真实记录导入的字段说明与限制 |
| `DELETE` | `/api/imports/{import_id}` | 删除一次导入及其数据 |
| `GET` | `/api/imports/{import_id}` | 导入详情与数据预览 |
| `GET` | `/api/imports/{import_id}/export.csv` | 导出已导入的成交（公式中和） |
| `GET` | `/api/live/status` | 真实成交接入状态（没有真实数据时明确显示未接入） |
| `GET` | `/api/runs/{run_id}/envelopes` | 把运行的信号转换为目标仓位信封 |

## 行情与数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/market/banner` | 全局横幅：运行模式、行情来源、最近有效事件时间、网络与 worker 状态 |
| `GET` | `/api/market/bars` | 读取 K 线（真实行情优先，失败回退合成数据） |
| `DELETE` | `/api/market/cache` | 清除一份 K 线缓存 |
| `GET` | `/api/market/cache` | 本地 K 线缓存清单 |
| `POST` | `/api/market/cache/fetch` | 预取一段真实历史行情进缓存 |
| `DELETE` | `/api/market/db-cache` | 清空服务端数据库缓存（可只清一个命名空间） |
| `GET` | `/api/market/db-cache` | 服务端数据库缓存：各类外部请求的缓存条目、命中与未命中 |
| `GET` | `/api/market/network` | 部署地网络实测状态（72 小时；不足时显示待验证） |
| `POST` | `/api/market/network/probe` | 立即探测一次行情接口可达性 |
| `GET` | `/api/market/quality` | 数据质量报告（缺口 / 重复 / 未收盘） |
| `GET` | `/api/market/quotes` | 关注列表行情：多个标的的收盘序列，未接入数据的标的如实标注 |
| `GET` | `/api/market/summary` | 标的市场概览（价格、波动、区间） |
| `GET` | `/api/market/symbols` | 支持的标的与周期 |
| `POST` | `/api/market/sync` | 同步真实行情：分段拉取并补齐缺口 |
| `GET` | `/api/market/ticker` | 最新价格与买卖价 |

## AI 策略助手

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/ai/actions/{action_id}` | 一个 AI 操作的状态 |
| `POST` | `/api/ai/actions/{action_id}` | 确认或取消一个需要确认的 AI 操作（只有发起人可以） |
| `POST` | `/api/ai/ask` | 页面内问 AI（一次性返回）：附带页面资料提问，没有模型时规则降级 |
| `GET` | `/api/ai/ask/meta` | 问 AI 面板元信息：可选模型、默认模型、思考努力程度、模型是否就绪 |
| `POST` | `/api/ai/ask/stream` | 页面内问 AI（流式 SSE）：逐段推送思考与正文，最后推送定稿 |
| `POST` | `/api/ai/chat` | 围绕当前策略的自由问答 |
| `POST` | `/api/ai/edit` | 按指令修改策略代码 |
| `POST` | `/api/ai/explain` | 解释策略逻辑与风险 |
| `POST` | `/api/ai/fix` | 根据报错修复策略 |
| `POST` | `/api/ai/generate` | 自然语言生成策略（自检 + 试跑 + 自动修复） |
| `GET` | `/api/ai/history` | AI 调用记录（模式、模型、提示词哈希） |
| `POST` | `/api/ai/interpret` | 解读回测结果 |
| `POST` | `/api/ai/review` | 以质疑者视角审阅策略 |
| `GET` | `/api/ai/tools` | 「问 AI」可调用的平台工具：场景、用途、何时可用、风险级别 |

## 策略广场

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `DELETE` | `/api/community/comments/{comment_id}` | 删除评论 |
| `POST` | `/api/community/comments/{comment_id}/like` | 评论点赞 |
| `GET` | `/api/community/favorites` | 我的收藏：帖子、策略、回测等统一列表 |
| `POST` | `/api/community/favorites` | 收藏内容；已收藏时再次调用会取消 |
| `DELETE` | `/api/community/favorites/{object_type}/{object_id}` | 取消收藏 |
| `GET` | `/api/community/favorites/{object_type}/{object_id}` | 检查某个对象是否已收藏 |
| `GET` | `/api/community/mutuals` | 和我互相关注的成员：@ 提及与快捷私聊的候选名单 |
| `GET` | `/api/community/posts` | 帖子列表（分类、标签、搜索、排序） |
| `POST` | `/api/community/posts` | 发帖（可附策略与回测快照） |
| `DELETE` | `/api/community/posts/{post_id}` | 删除自己的帖子 |
| `GET` | `/api/community/posts/{post_id}` | 帖子详情与评论 |
| `PUT` | `/api/community/posts/{post_id}` | 编辑自己的帖子 |
| `POST` | `/api/community/posts/{post_id}/accept/{comment_id}` | 问答采纳回答 |
| `POST` | `/api/community/posts/{post_id}/ai-review` | 请 AI 审稿人审阅 |
| `POST` | `/api/community/posts/{post_id}/clone` | 克隆帖子里的策略到我的策略 |
| `POST` | `/api/community/posts/{post_id}/comments` | 评论或回复 |
| `POST` | `/api/community/posts/{post_id}/dislike` | 踩帖子；再次调用取消 |
| `POST` | `/api/community/posts/{post_id}/favorite` | 收藏 / 取消 |
| `POST` | `/api/community/posts/{post_id}/feature` | 设为 / 取消精华 |
| `POST` | `/api/community/posts/{post_id}/like` | 点赞 / 取消 |
| `POST` | `/api/community/posts/{post_id}/pin` | 置顶 / 取消 |
| `POST` | `/api/community/posts/{post_id}/qmoji` | 给帖子贴一个 Qmoji 反应；再次调用取消 |
| `POST` | `/api/community/posts/{post_id}/report` | 举报帖子 |
| `GET` | `/api/community/profile` | 本机用户资料 |
| `PUT` | `/api/community/profile` | 修改作者名与简介 |
| `GET` | `/api/community/reports` | 帖子举报列表（工作区管理员） |
| `POST` | `/api/community/reports/{report_id}/decide` | 处理或忽略帖子举报（工作区管理员） |
| `GET` | `/api/community/stats` | 社区统计与贡献榜 |
| `GET` | `/api/community/tags` | 热门标签 |
| `GET` | `/api/community/users/{identifier}` | 用户主页：资料、关注/拉黑状态、私信策略与帖子 |
| `DELETE` | `/api/community/users/{identifier}/block` | 取消拉黑 |
| `POST` | `/api/community/users/{identifier}/block` | 拉黑用户：自动解除关注并隐藏其帖子 |
| `DELETE` | `/api/community/users/{identifier}/follow` | 取消关注 |
| `POST` | `/api/community/users/{identifier}/follow` | 关注用户 |
| `GET` | `/api/community/users/{identifier}/showcase` | 用户主页展示：按本人配置返回因子、策略、回测、模型与模拟收益 |

## 统一赛道与比较

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/arena/batches` | 历史批次列表（每批次冻结，改参数不影响原成绩） |
| `GET` | `/api/arena/batches/{batch_id}` | 批次详情（赛道、数据摘要、全部成绩） |
| `GET` | `/api/arena/batches/{batch_id}/leaderboard` | 批次排行榜（并列呈现收益/回撤/超额/完整性） |
| `GET` | `/api/arena/datasets` | CSV 数据集列表 |
| `GET` | `/api/arena/datasets/sample.csv` | 下载教学 CSV 样本 |
| `POST` | `/api/arena/datasets/upload` | 上传并严格校验一份 CSV 行情 |
| `DELETE` | `/api/arena/datasets/{dataset_id}` | 删除 CSV 数据集 |
| `GET` | `/api/arena/rules` | 规则策略列表 |
| `GET` | `/api/arena/rules/meta` | 规则策略的可用指标、模板与写法约束 |
| `POST` | `/api/arena/rules/save` | 把规则策略存进策略库 |
| `POST` | `/api/arena/rules/validate` | 校验一份规则策略并返回版本摘要 |
| `POST` | `/api/arena/run` | 跑一个统一赛道批次，返回比较结果与排行榜 |
| `GET` | `/api/arena/track/defaults` | 默认统一赛道配置（所有团队共用的同一组条件） |
| `POST` | `/api/arena/track/validate` | 校验一份赛道覆盖项并返回摘要 |
| `GET` | `/api/arena/windows/meta` | 7 天 / 30 天观察窗口口径说明 |

## 策略与实验

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/backtests` | 运行回测（可选四档压力场景对比） |
| `GET` | `/api/guidances` | 策略引导清单 |
| `POST` | `/api/guidances/render` | 把引导答案渲染成提示词 |
| `GET` | `/api/strategies` | 策略版本列表 |
| `POST` | `/api/strategies` | 保存一个新的策略草稿版本 |
| `GET` | `/api/strategies-migration` | 旧版统一赛道策略：还有多少条待转换成策略文件 |
| `POST` | `/api/strategies-migration` | 把旧版统一赛道策略转换成策略文件（原记录保留，可重复执行） |
| `POST` | `/api/strategies/generate` | 用 Agent 从自然语言生成候选策略 |
| `DELETE` | `/api/strategies/{strategy_id}` | 删除策略版本 |
| `GET` | `/api/strategies/{strategy_id}` | 策略版本详情 |
| `POST` | `/api/strategies/{strategy_id}/approve` | 人工批准策略版本（进实时模拟的前置条件） |
| `GET` | `/api/template-runs` | 模板实验记录列表 |
| `DELETE` | `/api/template-runs/{run_id}` | 删除模板实验记录 |
| `GET` | `/api/template-runs/{run_id}` | 模板实验详情（含净值、回撤、逐笔交易） |
| `GET` | `/api/templates` | 策略模板清单（含参数 schema） |

## Agent 角色与团队

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/agents/prompts` | 提示词库（任务模板 + 角色提示词） |
| `GET` | `/api/agents/roles` | 角色库（含可用工具与模型清单） |
| `POST` | `/api/agents/roles` | 新建自定义角色 |
| `POST` | `/api/agents/roles/draft` | AI 辅助起草角色卡 |
| `POST` | `/api/agents/roles/duplicate/{role_id}` | 复制角色（用于改内置角色） |
| `DELETE` | `/api/agents/roles/{role_id}` | 删除角色（内置角色不可删） |
| `PUT` | `/api/agents/roles/{role_id}` | 更新角色 |
| `POST` | `/api/agents/roles/{role_id}/reset` | 内置角色恢复默认设置 |
| `POST` | `/api/agents/roles/{role_id}/run` | 单独试跑一个角色 |
| `GET` | `/api/agents/store-stats` | 存储内容统计 |
| `GET` | `/api/agents/team-runs` | 团队调研记录列表 |
| `GET` | `/api/agents/team-runs/{run_id}` | 团队调研详情（各角色产出与结论） |
| `POST` | `/api/agents/team-runs/{run_id}/cancel` | 取消团队执行 |
| `POST` | `/api/agents/team-runs/{run_id}/resume` | 恢复团队执行 |
| `GET` | `/api/agents/team-runs/{run_id}/runtime` | 查询团队恢复状态 |
| `POST` | `/api/agents/team-runs/{run_id}/runtime/effect-resolution` | 核对团队工具效果 |
| `POST` | `/api/agents/team-runs/{run_id}/runtime/model-usage` | 核对团队模型用量 |
| `GET` | `/api/agents/teams` | 调研团队列表 |
| `POST` | `/api/agents/teams` | 新建团队 |
| `DELETE` | `/api/agents/teams/{team_id}` | 删除团队 |
| `PUT` | `/api/agents/teams/{team_id}` | 更新团队 |
| `POST` | `/api/agents/teams/{team_id}/run` | 执行一次团队调研 |

## 长期 Agent 与总览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/alert-rules` | 告警规则列表（事件类型、渠道、冷却窗口） |
| `POST` | `/api/alert-rules` | 新建或更新告警规则（管理员，默认 10 分钟冷却） |
| `DELETE` | `/api/alert-rules/{rule_id}` | 删除告警规则（管理员） |
| `GET` | `/api/alerts` | 告警历史（按时间倒序，可只看未读） |
| `POST` | `/api/alerts/preview` | 预览一条告警的标题与正文（不落库、不投递） |
| `POST` | `/api/alerts/read` | 把若干条告警标记为已读 |
| `GET` | `/api/alerts/status` | 告警通道健康状况：未读数与待投递队列 |
| `POST` | `/api/alerts/test` | 按当前规则真实触发一条告警（只入队，由 worker 投递） |
| `GET` | `/api/health` | 健康检查（含交易所连通性） |
| `GET` | `/api/healthz` | 容器探活（仅检查数据库） |
| `POST` | `/api/long-running/digest/run` | 生成并（可选）推送策略简报 |
| `POST` | `/api/long-running/maintainer/run` | 生成并（可选）推送策略维护评估 |
| `GET` | `/api/long-running/preview` | 预览简报与维护评估所需的结构化数据 |
| `GET` | `/api/long-running/records/{record_id}/runtime` | 查看通知生成与交付执行状态 |
| `POST` | `/api/long-running/records/{record_id}/runtime/cancel` | 取消通知执行并保留未确认效果 |
| `POST` | `/api/long-running/records/{record_id}/runtime/effect-resolution` | 核对通知或候选操作的外部效果 |
| `POST` | `/api/long-running/records/{record_id}/runtime/model-usage` | 核对通知生成请求的模型用量 |
| `POST` | `/api/long-running/records/{record_id}/runtime/resume` | 从原执行继续通知生成与交付 |
| `GET` | `/api/notifications` | 推送记录与待发队列 |
| `POST` | `/api/notifications/flush` | 重试待发队列 |
| `POST` | `/api/notifications/test` | 测试通知通道 |
| `GET` | `/api/notify/channels` | 通知渠道列表（凭证只回显掩码） |
| `POST` | `/api/notify/channels` | 新建通知渠道（凭证加密保存，管理员） |
| `DELETE` | `/api/notify/channels/{channel_id}` | 删除通知渠道并擦除其加密凭证（管理员） |
| `GET` | `/api/overview` | 平台总览（计数、最近实验、待批准版本、模型路由） |
| `GET` | `/api/schedules` | 定时任务列表 |
| `POST` | `/api/schedules` | 添加定时任务（cron） |
| `POST` | `/api/schedules/tick` | 手动触发一次调度检查 |
| `DELETE` | `/api/schedules/{job_id}` | 删除定时任务 |
| `GET` | `/api/workspace/usage` | 用量面板：各计量项的已用 / 上限 / 剩余 / 占比与近期走势 |
| `POST` | `/api/workspace/usage/check` | 配额预检：按运行类型判断是否还能运行（只读，超限返回 QUOTA_EXHAUSTED） |
| `GET` | `/api/workspace/usage/history` | 用量走势明细（按日期升序，可按计量项筛选） |
| `PUT` | `/api/workspace/usage/limits` | 调整配额上限（管理员；传 null 表示取消限制） |
| `POST` | `/api/workspace/usage/recount` | 重新统计工作区存储占用（管理员） |

## 配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/config` | 读取当前配置（密钥脱敏） |
| `GET` | `/api/config/defaults` | 默认配置模板 |
| `GET` | `/api/config/member-defaults` | 新成员默认限额：没有单独设置限额的成员使用工作区共享模型时按它计算 |
| `PUT` | `/api/config/member-defaults` | 设置新成员默认限额（管理员；支持每日 / 每周 / 每月 / 累计与滚动时间段） |
| `GET` | `/api/config/members` | 成员权限、限额、存储占用与近 30 天用量（管理员） |
| `PUT` | `/api/config/members/{user_id}` | 设置成员的模型权限与限额（管理员） |
| `GET` | `/api/config/models` | 列出所有可用的 provider/model 组合 |
| `GET` | `/api/config/presets` | 常见模型服务商预设 |
| `GET` | `/api/config/provider-quotas` | 按模型来源查询 5 小时 / 周 / 月 / 累计额度与用量 |
| `POST` | `/api/config/providers` | 登记或更新一个 Provider |
| `POST` | `/api/config/providers/discover` | 向服务商查询可用模型列表（OpenAI 兼容 /models 或 Anthropic /v1/models） |
| `DELETE` | `/api/config/providers/{name}` | 删除界面登记的 Provider 与密钥 |
| `GET` | `/api/config/quota-requests` | 额度申请：成员看自己的，管理员看全部 |
| `POST` | `/api/config/quota-requests` | 申请提升使用工作区模型的额度 |
| `POST` | `/api/config/quota-requests/{rid}/decide` | 批准或拒绝额度申请（管理员）；批准后自动更新该成员的限额 |
| `PUT` | `/api/config/routing` | 按用途设置模型路由与降级链 |
| `GET` | `/api/config/search` | 联网搜索配置状态（密钥只回显脱敏值） |
| `PUT` | `/api/config/search` | 设置联网搜索密钥（部署级，加密存放；留空删除） |
| `POST` | `/api/config/search/sources` | 添加自定义搜索来源（SearXNG / RSS） |
| `DELETE` | `/api/config/search/sources/{source_id}` | 删除自定义搜索来源 |
| `GET` | `/api/config/status` | 各用途是否有可用模型（决定 AI 功能是否降级） |
| `POST` | `/api/config/test` | 真实调用一次模型，验证密钥与连通性 |
| `GET` | `/api/config/usage` | 模型用量与费用统计（我的 / 整个工作区），以及当前限额状态 |

## AI 策略室

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/lab/goal/check` | 检查目标是否合理：返回结论与字段级建议值（可选让模型复核） |
| `POST` | `/api/lab/goal/parse` | 解析 /goal 文本：把“夏普>1.2 回撤<15% 4h 近 2 年”转成结构化目标（未出现的字段沿用项目现值） |
| `GET` | `/api/lab/meta` | 策略室元信息：可选模型、团队、默认验收目标、模型是否就绪 |
| `GET` | `/api/lab/projects` | 策略项目列表 |
| `POST` | `/api/lab/projects` | 新建策略项目（可带初始代码、模板或钉住的上下文） |
| `POST` | `/api/lab/projects/from-ask` | 把「问 AI」升级成研究会话：写入种子消息，默认私有归当前用户 |
| `DELETE` | `/api/lab/projects/{pid}` | 删除对话（已保存的平台策略不受影响）；cascade=true 时连同它的全部分叉一起删除 |
| `GET` | `/api/lab/projects/{pid}` | 项目详情与增量消息（after=已收到的最大序号，since=上次的 server_time） |
| `PATCH` | `/api/lab/projects/{pid}` | 修改项目：标题、模型、验收目标、标的、归档 |
| `POST` | `/api/lab/projects/{pid}/autopilot` | 自动迭代：调研 → 生成 → 回测 → 对照目标改进，结束后询问是否满意 |
| `POST` | `/api/lab/projects/{pid}/backtest` | 用平台回测跑当前版本（cost_scale>1 为成本压力测试） |
| `POST` | `/api/lab/projects/{pid}/code` | 保存手动编辑的代码为新版本（先做沙箱试跑） |
| `POST` | `/api/lab/projects/{pid}/compact` | 压缩对话历史：把之前的对话总结成摘要钉进上下文，之后不再发原文 |
| `GET` | `/api/lab/projects/{pid}/context-pack` | 统一上下文包：系统规则、项目规则、来源引用与预算 |
| `POST` | `/api/lab/projects/{pid}/cost-sweep` | 成本压力扫描：并行回测多档成本倍数，找出不再满足目标的成本极限 |
| `POST` | `/api/lab/projects/{pid}/decision` | 回答 AI 的提问：满意（保存为策略）/ 继续优化 / 换个方向 |
| `POST` | `/api/lab/projects/{pid}/discussion` | 在对话里挂一张团队讨论卡片（/discuss 发起的讨论，发言以气泡展示） |
| `GET` | `/api/lab/projects/{pid}/events` | 实时推送（SSE）：增量消息与项目状态，回合结束后自动关闭 |
| `POST` | `/api/lab/projects/{pid}/fork` | 从某条消息分叉出一个新对话（挂在原对话下） |
| `POST` | `/api/lab/projects/{pid}/goal` | 设定项目目标（/goal）：写入条件与回测范围，做合理性检查；可选立即开始自动迭代 |
| `GET` | `/api/lab/projects/{pid}/images/{iid}` | 读取对话里附带的图片 |
| `POST` | `/api/lab/projects/{pid}/messages` | 发送一条消息：模型带工具协作（/回测 /解释 /团队 /迭代 为快捷指令） |
| `POST` | `/api/lab/projects/{pid}/messages/{seq}/feedback` | 给一条 Agent 消息点赞 / 点踩（可附意见） |
| `POST` | `/api/lab/projects/{pid}/parameter-heatmap` | 二维参数敏感度热力图：网格回测并在对话里渲染参数平原 |
| `POST` | `/api/lab/projects/{pid}/pins` | 把一段材料钉进模型上下文（调研结论、笔记、回测解读） |
| `DELETE` | `/api/lab/projects/{pid}/pins/{pin_id}` | 把材料移出模型上下文 |
| `POST` | `/api/lab/projects/{pid}/plan` | /plan：AI 列分步计划并一步步执行，直到全部完成且目标达标 |
| `POST` | `/api/lab/projects/{pid}/render-errors` | 回答里的内容块没能显示：把错误交回 AI 自动修正（每条消息只修一次） |
| `POST` | `/api/lab/projects/{pid}/report` | /report：按调研报告流程列计划、收集资料、分析，最后生成可导出 PDF 的报告 |
| `GET` | `/api/lab/projects/{pid}/reports/{rid}` | 读取一份调研报告（Markdown 正文） |
| `POST` | `/api/lab/projects/{pid}/rewind` | 编辑或重试一条用户消息：删掉它之后的对话，用新内容重新发送 |
| `GET` | `/api/lab/projects/{pid}/runtime` | 读取对话的权威执行状态和数据库游标事件 |
| `POST` | `/api/lab/projects/{pid}/runtime/effect-resolution` | 管理员记录已核实的外部效果结果 |
| `POST` | `/api/lab/projects/{pid}/runtime/model-usage` | 管理员记录服务商已确认的模型用量 |
| `POST` | `/api/lab/projects/{pid}/runtime/resume` | 幂等请求推进已有对话运行 |
| `POST` | `/api/lab/projects/{pid}/save` | 把当前版本保存为平台策略草稿（可在编辑器、回测、模拟中继续使用） |
| `POST` | `/api/lab/projects/{pid}/stop` | 停止正在运行的回合：模型输出与联网搜索立即中断；其他工具在跑完当前一步后停下 |
| `POST` | `/api/lab/projects/{pid}/stress-test` | 一键过拟合压力测试：滚动分段、参数敏感性、成本压力 |
| `POST` | `/api/lab/projects/{pid}/versions/{v}/restore` | 切换到历史版本（不删除任何版本） |
| `POST` | `/api/lab/projects/{pid}/walkforward` | 分段回测：把回测区间等分几段分别回测，检查策略是否只在某一段有效 |

## Freqtrade 导入

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/freqtrade/check` | 检查一个 Freqtrade 策略能否导入（试跑一小段，不保存） |
| `GET` | `/api/freqtrade/export/{slug}` | 把导入过的策略导回 Freqtrade 文件（参数写回 default） |
| `POST` | `/api/freqtrade/import` | 导入 Freqtrade 策略为平台策略，并自动开始一次体检 |
| `POST` | `/api/freqtrade/rewrite` | AI 把导入不了的 Freqtrade 策略改写成平台原生策略 |
| `GET` | `/api/freqtrade/support` | 兼容范围：支持的指标、会被忽略和不支持的写法 |

## 上线验证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/validation/calibration` | 各交易所校准过的回测成本（回测表单里可以一键套用） |
| `GET` | `/api/validation/checkup` | 体检记录（不含明细） |
| `POST` | `/api/validation/checkup` | 开始一次策略体检（后台运行，轮询结果） |
| `GET` | `/api/validation/checkup/{cid}` | 一次体检的完整结果 |
| `GET` | `/api/validation/fidelity` | 真实度报告列表（可按策略过滤） |
| `POST` | `/api/validation/fidelity` | 生成仿真真实度报告：在实验跑过的行情上重跑回测，逐项拆解收益差并给出成本校准建议 |
| `GET` | `/api/validation/fidelity/{rid}` | 一份真实度报告 |
| `POST` | `/api/validation/fidelity/{rid}/calibration` | 把报告里的校准建议设为这家交易所的回测成本（需确认） |
| `GET` | `/api/validation/graduation` | 所有策略的阶段：回测 → 纸面模拟 → 模拟盘 → 毕业 |
| `POST` | `/api/validation/graduation-config` | 修改闸门门槛（部署管理员） |
| `GET` | `/api/validation/graduation/{slug}` | 一个策略的当前阶段与晋级检查 |
| `POST` | `/api/validation/graduation/{slug}/demote` | 退回到更早的阶段 |
| `POST` | `/api/validation/graduation/{slug}/promote` | 晋级到下一阶段（全部检查通过 + 你确认） |
| `GET` | `/api/validation/stress` | 压力演练记录（不含曲线） |
| `POST` | `/api/validation/stress` | 开始一次压力演练（后台运行，轮询结果） |
| `GET` | `/api/validation/stress/scenarios` | 压力演练的历史极端日 |
| `GET` | `/api/validation/stress/{rid}` | 一次压力演练的完整结果 |
| `GET` | `/api/validation/watcher` | 所有实验的值守结论摘要 |
| `GET` | `/api/validation/watcher/{session_id}` | 值守员对这个实验的最近一次结论 |
| `POST` | `/api/validation/watcher/{session_id}` | 让值守员现在看一遍这个实验：逐笔解释成交、标出和说明书矛盾的行为 |

## 交易所模拟盘

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/demo/connections` | 模拟盘连接列表（密钥只回显掩码） |
| `POST` | `/api/demo/connections` | 连接交易所模拟盘（验证通过才保存，密钥加密存储） |
| `DELETE` | `/api/demo/connections/{cid}` | 撤销模拟盘连接并擦除密钥 |
| `GET` | `/api/demo/connections/{cid}/contract-bills` | 拉取模拟账户账单并核验永续合约规则假设 |
| `POST` | `/api/demo/connections/{cid}/test` | 重新测试模拟盘连接 |
| `GET` | `/api/demo/environments` | 支持的交易所模拟环境 |
| `GET` | `/api/demo/live` | 实时行情：正在形成的 K 线与推送状态 |

## 信息源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/datahub/auto` | 研究数据自动同步：是否开启、间隔、上次运行结果 |
| `POST` | `/api/datahub/auto` | 开启 / 关闭研究数据自动同步（worker 按间隔把已有数据集补到最新） |
| `POST` | `/api/datahub/auto/run` | 立即把已有数据集补到最新（后台执行） |
| `GET` | `/api/datahub/connectors` | 连接器列表：类别、字段、是否需要密钥、已有数据集数与最近同步 |
| `GET` | `/api/datahub/datasets` | 已有数据集（字段 × 标的 × 来源） |
| `DELETE` | `/api/datahub/datasets/{key}` | 删除数据集 |
| `GET` | `/api/datahub/datasets/{key}/series` | 数据集序列预览（最多 limit 个点，等间隔抽样） |
| `GET` | `/api/datahub/events` | 事件流：交易所公告、新闻（含标注器与回看偏差标记）、宏观日程，按发布时间倒序 |
| `POST` | `/api/datahub/import` | 导入自定义 CSV：ts,value[,available_at]，在因子里用 x_<名字> 引用 |
| `POST` | `/api/datahub/keys` | 填写或清除连接器密钥（部署管理员） |
| `GET` | `/api/datahub/liquidations` | 强平流记录器状态：是否在记录、收到的推送数、最近一条的时间 |
| `POST` | `/api/datahub/liquidations` | 开始 / 停止记录币安全市场强平流，或把已结束的小时写进数据集 |
| `POST` | `/api/datahub/sync` | 同步连接器数据（wait=false 时后台执行，数据集状态显示进度） |

## 公开分享

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/collab/public-shares` | 为群组 / 用户 / 帖子 / 策略生成公开分享链接 |
| `DELETE` | `/api/collab/public-shares/{token}` | 撤销公开分享链接 |
| `GET` | `/api/public/shares/{token}` | 未登录可访问的公开分享预览 |

## 协作与通知

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/collab/discover` | 公开目录：按名称 / QuantLab UID / 群号搜索公开用户与公开群组 |
| `POST` | `/api/collab/dm` | 打开与某位成员的私聊（没有就新建），返回私聊会话 |
| `GET` | `/api/collab/editors/{object_type}` | 每个对象的编辑者（列表上的头像标记） |
| `GET` | `/api/collab/edits/{object_type}/{object_id}` | 编辑记录：谁（人 / Agent）在什么时候改出了哪个版本 |
| `GET` | `/api/collab/groups` | 群组列表：我加入的与工作区内公开的群组 |
| `POST` | `/api/collab/groups` | 新建群组（任何成员都可以组建） |
| `DELETE` | `/api/collab/groups/{gid}` | 解散（归档）群组：群主或工作区管理员 |
| `GET` | `/api/collab/groups/{gid}` | 群组详情与成员 |
| `PUT` | `/api/collab/groups/{gid}` | 修改群组名称、简介、可见性与群成员的群组模型限额（群主 / 群管理员） |
| `DELETE` | `/api/collab/groups/{gid}/bot` | 移除群组 QuantLab Bot（群主 / 群管理员） |
| `GET` | `/api/collab/groups/{gid}/bot` | 群组 QuantLab Bot 配置与可选模型 |
| `PUT` | `/api/collab/groups/{gid}/bot` | 添加或修改群组 QuantLab Bot（群主 / 群管理员） |
| `POST` | `/api/collab/groups/{gid}/join` | 加入公开群组 |
| `POST` | `/api/collab/groups/{gid}/members` | 拉成员进群（群主 / 群管理员；公开群的成员也可以邀请） |
| `DELETE` | `/api/collab/groups/{gid}/members/{user_id}` | 移出群成员（群主 / 群管理员）或退出群组（本人） |
| `PUT` | `/api/collab/groups/{gid}/members/{user_id}` | 设置群角色或转让群主 |
| `GET` | `/api/collab/groups/{gid}/messages` | 群聊消息（after 增量拉取，before 向上翻页） |
| `POST` | `/api/collab/groups/{gid}/messages` | 发群消息（Markdown + 引用回测 / 策略 / 代码 / 文件；@ 提及进通知中心） |
| `POST` | `/api/collab/groups/{gid}/number/rotate` | 销毁并重新生成 QuantLab 群号 |
| `PUT` | `/api/collab/groups/{gid}/quota` | 给群组分配工作区共享模型额度（工作区管理员） |
| `POST` | `/api/collab/groups/{gid}/read` | 标记群消息已读到某条 |
| `GET` | `/api/collab/groups/{gid}/shared` | 在本群组内公开的策略与研究项目 |
| `GET` | `/api/collab/groups/{gid}/tasks` | 群组任务看板 |
| `POST` | `/api/collab/groups/{gid}/tasks` | 新建并指派任务（群成员） |
| `GET` | `/api/collab/groups/{gid}/usage` | 群组模型用量与限额状态（群组服务商、全群共享模型额度、成员用量） |
| `GET` | `/api/collab/lineage` | 策略血缘树：版本链、克隆派生、各版本的编辑者与回测 |
| `GET` | `/api/collab/members` | 工作区成员目录（选择群成员、指派任务、@ 提及、私聊用；q 按名字 / 用户名 / 邮箱搜索） |
| `DELETE` | `/api/collab/messages/{mid}` | 撤回消息（本人；群主 / 群管理员可删除任何消息） |
| `PUT` | `/api/collab/messages/{mid}` | 编辑自己的消息 |
| `POST` | `/api/collab/messages/{mid}/pin` | 置顶 / 取消置顶群消息（群主 / 群管理员） |
| `POST` | `/api/collab/messages/{mid}/qmoji` | 给群消息贴一个 Qmoji 反应；再次调用取消 |
| `POST` | `/api/collab/presence` | 在线心跳：我正在看 / 编辑这个对象；返回同时在场的其他成员 |
| `GET` | `/api/collab/refs/search` | 引用选择器：按类型搜索可引用的回测、策略、帖子、文件与任务 |
| `POST` | `/api/collab/share-to` | 把平台里的任意内容（回测、策略、报告、帖子、页面）分享到群组、私聊或策略广场 |
| `GET` | `/api/collab/shares/{object_type}/{object_id}` | 读取策略 / 研究项目的可见性与作者 |
| `PUT` | `/api/collab/shares/{object_type}/{object_id}` | 设置可见性：仅自己 / 工作区内公开 / 指定群组内公开 |
| `GET` | `/api/collab/tasks/mine` | 指派给我的任务（跨群组） |
| `DELETE` | `/api/collab/tasks/{tid}` | 删除任务（创建者或群主 / 群管理员） |
| `PUT` | `/api/collab/tasks/{tid}` | 修改任务：改状态、重新指派、调整优先级与截止时间 |
| `GET` | `/api/inbox` | 消息通知中心：我的通知（可只看未读、按类型筛选、向前翻页） |
| `POST` | `/api/inbox/clear` | 删除通知：指定 id，或清空全部已读 |
| `GET` | `/api/inbox/mutes` | 我的屏蔽列表（人、群组、通知类型、帖子） |
| `POST` | `/api/inbox/mutes` | 屏蔽：不再收到某人 / 某群组 / 某类 / 某帖子的通知；屏蔽的人的群消息会折叠 |
| `DELETE` | `/api/inbox/mutes/{target_type}/{target_id}` | 取消屏蔽 |
| `POST` | `/api/inbox/read` | 标记已读：指定 id、某一类型，或全部 |
| `GET` | `/api/inbox/summary` | 顶栏角标：未读通知数、我的待办任务数、待处理问题报告（管理员）与全站公告 |
| `GET` | `/api/issues` | 问题报告：成员看自己的；管理员看自己管理的工作区（部署管理员看全部） |
| `POST` | `/api/issues` | 提交问题报告（可附截图与文件） |
| `GET` | `/api/issues/{iid}` | 问题报告详情与回复 |
| `POST` | `/api/issues/{iid}/replies` | 回复问题报告（提交者补充，或管理员答复） |
| `PUT` | `/api/issues/{iid}/status` | 修改问题报告状态（管理员；提交者可以关闭自己的报告） |
| `POST` | `/api/uploads` | 上传文件（原始字节，Content-Type 为文件类型，?filename= 文件名；单个 10 MB） |
| `GET` | `/api/uploads/{fid}` | 读取上传的文件（图片直接显示，其他类型下载） |
| `GET` | `/api/uploads/{fid}/thumb` | 读取图片缩略图（列表与帖子卡使用，缺失时回退原图） |

## 因子库

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/factors` | 因子库列表（status 过滤） |
| `POST` | `/api/factors` | 保存因子到库（默认先评估，报告一并保存） |
| `POST` | `/api/factors/evaluate` | 评估一个因子表达式：IC、衰减、分组、多空、换手、样本外、库内相关与入库检查 |
| `GET` | `/api/factors/meta` | 因子研究元信息：字段、算子、标的池、类别、状态、入库门槛、目标指标、种子假设 |
| `GET` | `/api/factors/trials` | 试验台账：评估过的表达式（含失败的），用于多重检验与避免重复 |
| `POST` | `/api/factors/validate` | 校验因子表达式：返回规范写法、复杂度、用到的字段与最长回看 |
| `DELETE` | `/api/factors/{fid}` | 删除因子（试验台账保留） |
| `GET` | `/api/factors/{fid}` | 因子详情 |
| `PATCH` | `/api/factors/{fid}` | 修改因子名称、假设卡、标签、备注（表达式不可改，改了就是新因子） |
| `POST` | `/api/factors/{fid}/refresh` | 用最新数据重新评估因子，记录健康度（IC 是否衰减） |
| `GET` | `/api/factors/{fid}/report` | 因子最近一次完整评估报告 |
| `POST` | `/api/factors/{fid}/status` | 改变因子状态：候选 / 入库 / 衰减预警 / 退役 |

## 对话媒体

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/media/image` | 下载并缓存一张外链图片（对话里的图片自动下载） |

## 微实盘

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/microlive` | 微实盘状态：服务器与工作区开关、风险告知、急停、资金档位、运行中的实验 |
| `POST` | `/api/microlive/connections` | 接入实盘 Key：必须能交易、不能提现划转、绑定 IP 白名单 |
| `DELETE` | `/api/microlive/connections/{cid}` | 删除实盘 Key（平台保存的密文擦除） |
| `POST` | `/api/microlive/connections/{cid}/test` | 重新自检一个实盘 Key |
| `POST` | `/api/microlive/disable` | 关闭微实盘（不影响已在运行的实验，要停它们请用一键停止） |
| `POST` | `/api/microlive/enable` | 打开微实盘（管理员读完风险告知后确认） |
| `POST` | `/api/microlive/kill` | 一键停止：所有微实盘实验平仓并停止，之后拒绝新的启动 |
| `GET` | `/api/microlive/notice` | 风险告知原文与版本 |
| `POST` | `/api/microlive/prepare` | 生成启动摘要与 6 位确认码（5 分钟有效） |
| `POST` | `/api/microlive/rearm` | 解除急停（管理员） |
| `POST` | `/api/microlive/start` | 输入确认码启动微实盘 |
| `POST` | `/api/microlive/tiers/{slug}/lower` | 资金上限降一档 |
| `POST` | `/api/microlive/tiers/{slug}/raise` | 资金上限升一档（条件满足后由管理员确认） |

## 执行与监控

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/execution` | 执行方案库 |
| `POST` | `/api/execution` | 把一次仿真存为执行方案 |
| `POST` | `/api/execution/capacity` | 容量估计：资金规模越大冲击越大，算出成本吃掉一半 / 全部收益时的规模 |
| `GET` | `/api/execution/costs` | 成本模型列表（当前启用的那个用于容量估计与回测滑点） |
| `POST` | `/api/execution/costs/calibrate` | 用已导入的真实成交校准平方根冲击模型 |
| `DELETE` | `/api/execution/costs/{cid}` | 删除成本模型 |
| `GET` | `/api/execution/costs/{cid}` | 成本模型详情（含散点） |
| `POST` | `/api/execution/costs/{cid}/activate` | 启用这个成本模型 |
| `POST` | `/api/execution/evaluate` | 仿真一个执行方案：多个片段 + TWAP/市价基准，返回 TCA 报告（记一笔试验） |
| `GET` | `/api/execution/meta` | 算法菜单、默认方案、市场状态、目标指标、录制与成本模型 |
| `GET` | `/api/execution/radar` | 市场雷达：一组标的的波动、量能、价差与状态，异常给出提示 |
| `GET` | `/api/execution/recordings` | 盘口录制列表 |
| `POST` | `/api/execution/recordings` | 开始录制公开盘口与逐笔成交（只读行情，不需要密钥） |
| `DELETE` | `/api/execution/recordings/{rid}` | 删除录制 |
| `GET` | `/api/execution/recordings/{rid}` | 录制详情与盘口概况 |
| `POST` | `/api/execution/recordings/{rid}/stop` | 提前结束录制 |
| `POST` | `/api/execution/replay` | 回放一个执行片段：逐步的计划 / 完成进度、挂单价、市场状态、成交与提示 |
| `GET` | `/api/execution/runs` | 仿真试验记录 |
| `DELETE` | `/api/execution/runs/{rid}` | 删除仿真试验 |
| `GET` | `/api/execution/runs/{rid}` | 一次仿真的完整报告 |
| `DELETE` | `/api/execution/{sid}` | 删除执行方案 |
| `GET` | `/api/execution/{sid}` | 执行方案详情 |
| `PATCH` | `/api/execution/{sid}` | 修改执行方案的名字、状态、备注 |
| `GET` | `/api/execution/{sid}/report` | 执行方案的完整报告 |

## 扩展：技能 / 钩子 / MCP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/hooks` | 列出已注册 hooks 与触发点（只读；默认不启用、不执行外部命令） |
| `POST` | `/api/hooks/{hook_id}/toggle` | 启用/停用进程内 hook（仅本地项目或管理员；不能注册任意代码） |
| `GET` | `/api/mcp/servers` | MCP server 目录与健康检查（不启动进程、不建立连接） |
| `POST` | `/api/mcp/servers` | 保存一个 MCP server 配置（只存本地，不启动进程） |
| `DELETE` | `/api/mcp/servers/{name}` | 删除 MCP 配置 |
| `POST` | `/api/mcp/servers/{name}/call` | 调用 stdio MCP server 的一个工具（仅本地、显式开启） |
| `POST` | `/api/mcp/servers/{name}/probe` | 探测 stdio MCP server：initialize + tools/list（仅本地、显式开启） |
| `GET` | `/api/skills` | 列出项目 / 工作区技能（只读 .quantlab/skills/*.md） |
| `GET` | `/api/skills/{name}` | 查看一个技能的内容（只读） |
| `POST` | `/api/skills/{name}/attach` | 把一个技能追加到 Lab 项目的 instructions（可选注入） |

## 指挥台

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/missions` | 作战任务列表 |
| `POST` | `/api/missions` | 下达指令：创建作战任务并开始（单周期或流水线） |
| `POST` | `/api/missions/pipelines` | 把当前流水线存为模板（带 id 则覆盖已保存的模板） |
| `DELETE` | `/api/missions/pipelines/{pid}` | 删除已保存的流水线模板 |
| `GET` | `/api/missions/presets` | 流水线作业预设（内置 + 已保存模板）与可用团队、角色 |
| `DELETE` | `/api/missions/{mid}` | 删除作战任务 |
| `GET` | `/api/missions/{mid}` | 任务详情与增量发言（after=已收到的最大序号，since=上次的 server_time） |
| `POST` | `/api/missions/{mid}/approve` | 关卡批准：进入下一阶段 |
| `POST` | `/api/missions/{mid}/say` | 指挥官插话；指定 to_role 时该角色会回应 |
| `POST` | `/api/missions/{mid}/stop` | 叫停任务（在当前发言结束后停下） |

## 新手引导

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/onboarding` | 读取当前账号的新手引导进度 |
| `PUT` | `/api/onboarding` | 更新新手引导进度 |
| `POST` | `/api/onboarding/reset` | 重新开始新手引导 |

## 本地项目

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/local/artifacts/{hash}/path` | 解析本地项目中工件文件的真实路径，供桌面端打开文件夹 |
| `GET` | `/api/local/cache` | 查看本地缓存 |
| `POST` | `/api/local/cache/clear` | 清理本地缓存 |
| `GET` | `/api/local/files` | 浏览本地项目文件 |
| `POST` | `/api/local/files/document-preview` | 渲染文档页面预览 |
| `GET` | `/api/local/files/download` | 下载本地文档 |
| `GET` | `/api/local/files/image` | 读取本地图片 |
| `POST` | `/api/local/files/import` | 导入本地文件 |
| `GET` | `/api/local/files/preview` | 预览本地文件 |
| `POST` | `/api/local/files/relocate` | 移动本地文件 |
| `GET` | `/api/local/info` | 读取本地项目信息 |
| `GET` | `/api/local/library` | 查看本地成果库 |
| `POST` | `/api/local/library/import` | 从成果库导入模块 |
| `POST` | `/api/local/library/publish` | 发布模块到成果库 |
| `POST` | `/api/local/permissions` | 授予本地文件访问权限 |
| `POST` | `/api/local/permissions/revoke` | 撤销本地文件访问权限 |
| `GET` | `/api/local/preferences` | 读取本地项目偏好 |
| `POST` | `/api/local/preferences` | 保存本地项目偏好 |
| `GET` | `/api/local/project/download/{name}` | 下载项目导出文件 |
| `POST` | `/api/local/project/export` | 导出本地项目 |
| `POST` | `/api/local/project/open` | 打开本地项目 |
| `GET` | `/api/local/resources` | 读取本地资源 |
| `GET` | `/api/local/runtime` | 受管 Python 运行时探测（仅本地模式，只读诊断） |
| `POST` | `/api/local/runtime-artifacts/cleanup` | 清理过期交付快照缓存 |
| `GET` | `/api/local/runtime-artifacts/{effect_id}/{digest}` | 读取已提交文档交付快照 |
| `POST` | `/api/local/shutdown` | 关闭本地项目服务 |
| `GET` | `/api/local/tasks` | 读取本地任务 |

## 模型库

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/models` | 模型库列表（status 过滤） |
| `POST` | `/api/models` | 把一次实验存为模型（带模型卡） |
| `GET` | `/api/models/compare` | 两次实验对比：规格差异与指标差异 |
| `GET` | `/api/models/jobs/{jid}` | 训练进度 |
| `POST` | `/api/models/jobs/{jid}/cancel` | 取消训练（在下一个检查点停下） |
| `GET` | `/api/models/meta` | 模型研究元信息：模型菜单、标签、默认特征、后端是否可用、目标指标 |
| `GET` | `/api/models/runs` | 实验记录（每次训练一笔） |
| `DELETE` | `/api/models/runs/{rid}` | 删除实验记录（已存入模型库的模型不受影响） |
| `GET` | `/api/models/runs/{rid}` | 实验详情：完整报告 |
| `POST` | `/api/models/train` | 训练一个模型（一次实验）：滚动样本外评估、基线对照、泄漏检查。wait=false 时后台执行 |
| `DELETE` | `/api/models/{mid}` | 删除模型（实验记录保留） |
| `GET` | `/api/models/{mid}` | 模型详情（含模型卡） |
| `PATCH` | `/api/models/{mid}` | 修改名称、状态、备注；冻结后不能再被覆盖 |
| `POST` | `/api/models/{mid}/refresh` | 健康度：用冻结参数在最新数据上预测，记录训练区间之后的 RankIC 与特征漂移 |
| `GET` | `/api/models/{mid}/report` | 模型的完整训练报告 |

## 真实永续

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/realperp` | 真实永续状态、连接和运行中的实验 |
| `POST` | `/api/realperp/connections` | 接入真实永续专用 Key |
| `DELETE` | `/api/realperp/connections/{cid}` | 擦除真实永续 Key |
| `POST` | `/api/realperp/connections/{cid}/test` | 重新检查真实永续 Key 与期货账户 |
| `POST` | `/api/realperp/disable` | 关闭真实永续工作区开关 |
| `POST` | `/api/realperp/enable` | 确认真实永续风险并打开工作区开关 |
| `POST` | `/api/realperp/kill` | 一键平掉所有真实永续持仓并急停 |
| `GET` | `/api/realperp/notice` | 真实永续风险告知 |
| `POST` | `/api/realperp/prepare` | 生成真实永续启动摘要与确认码 |
| `POST` | `/api/realperp/rearm` | 解除真实永续急停 |
| `POST` | `/api/realperp/start` | 输入确认码启动真实永续 |

## 研究导师

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/mentor/course` | 研究导师课程与当前进度 |
| `PUT` | `/api/mentor/progress` | 更新研究导师课程完成进度 |

## 研究工作流

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/workflows` | 工作流列表与当前版本 |
| `POST` | `/api/workflows` | 保存可继续编辑的工作流草稿 |
| `GET` | `/api/workflows/artifacts` | 按研究、运行和关键词检索真实产物 |
| `GET` | `/api/workflows/artifacts/pins` | 列出当前工作区的固定成果 |
| `POST` | `/api/workflows/artifacts/pins` | 固定本工作区的一个真实运行产物 |
| `DELETE` | `/api/workflows/artifacts/pins/{pin_id}` | 取消固定成果 |
| `GET` | `/api/workflows/artifacts/versions` | 按版本键读取同一节点端口的历史产物 |
| `GET` | `/api/workflows/artifacts/{digest}` | 读取本工作区工件的原始JSON内容 |
| `GET` | `/api/workflows/artifacts/{digest}/download` | 下载经完整性验证的工件JSON或表格CSV |
| `GET` | `/api/workflows/artifacts/{digest}/versions` | 由工件hash反查同节点端口的历史版本 |
| `GET` | `/api/workflows/assets` | 浏览同一工作区的策略、因子、模型与组合执行资产 |
| `POST` | `/api/workflows/assets/snapshot` | 以冻结定义创建已有资产的研究模块 |
| `GET` | `/api/workflows/catalog` | 函数卡片、完整代码与研究模板 |
| `POST` | `/api/workflows/charts/preview` | 使用已冻结数据预览图表，不重跑上游研究 |
| `POST` | `/api/workflows/definitions` | 发布不可覆盖的自定义函数卡片版本 |
| `POST` | `/api/workflows/definitions/preview` | 静态检查代码并展示端口、参数及代码差异 |
| `GET` | `/api/workflows/definitions/{kind}/{version}/sources` | 查看真实实现包源码或指定运行的冻结源码 |
| `GET` | `/api/workflows/experiments` | 工作流实验与来源记录 |
| `POST` | `/api/workflows/experiments/compare` | 读取实验指标并保留失败和未完成结果 |
| `GET` | `/api/workflows/exports/{name}` | 下载含依赖安装包的独立运行研究包 |
| `GET` | `/api/workflows/files` | 工作区已导入的不可覆盖输入文件 |
| `POST` | `/api/workflows/files` | 流式导入研究文件，不加载整个文件到内存 |
| `GET` | `/api/workflows/files/download` | 下载本工作区数据或二进制研究工件 |
| `GET` | `/api/workflows/files/preview` | 分页预览生成的数据文件或文本内容 |
| `GET` | `/api/workflows/guide` | 查询工作流、组件图和运行工件规范 |
| `POST` | `/api/workflows/network/inspect` | 验证神经组件图并返回形状、参数量与代码 |
| `GET` | `/api/workflows/recent-result` | 当前工作区最近一次有工件证据的已完成研究 |
| `GET` | `/api/workflows/runs/{run_id}` | 查询运行状态、节点输入输出与日志 |
| `GET` | `/api/workflows/runs/{run_id}/children/{child_id}` | 读取本次实验内的子模块运行证据 |
| `POST` | `/api/workflows/runs/{run_id}/export` | 以运行时冻结的工作流、版本和实现包构建离线复现包 |
| `POST` | `/api/workflows/subflows/encapsulate` | 将选中卡片封装为具名输入输出的复用子流程 |
| `POST` | `/api/workflows/subflows/expand` | 展开子流程为可编辑卡片，保持输入输出连接 |
| `GET` | `/api/workflows/{wid}` | 读取工作流及历史快照 |
| `PUT` | `/api/workflows/{wid}` | 校验修订号并创建新工作流版本 |
| `GET` | `/api/workflows/{wid}/activity` | GUI与Agent共用的最近运行与进行中任务 |
| `POST` | `/api/workflows/{wid}/experiments/plan` | 预览参数网格或滚动时间窗口，不执行研究 |
| `POST` | `/api/workflows/{wid}/experiments/submit` | 按冻结修订提交独立实验，不修改画布 |
| `POST` | `/api/workflows/{wid}/export` | 后台构建含代码、数据及依赖wheel的完整离线包 |
| `GET` | `/api/workflows/{wid}/notes` | 研究结论、笔记与冻结运行引用 |
| `PUT` | `/api/workflows/{wid}/notes` | 保存带版本检查的研究笔记 |
| `POST` | `/api/workflows/{wid}/run` | 使用现有Worker执行完整或局部工作流 |
| `POST` | `/api/workflows/{wid}/validate` | 运行前自检，不执行卡片代码 |
| `GET` | `/api/workflows/{wid}/view` | 对话侧只读编排图与真实运行状态 |
| `GET` | `/api/workflows/{workflow_id}/module-view` | 逐层查看模块内部函数、参数和运行证据 |

## 研究执行环境

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/workflows/environment` | 查看研究执行环境 |
| `GET` | `/api/workflows/environment/discover` | 发现研究执行环境 |
| `GET` | `/api/workflows/environment/jobs/{job_id}` | 查看环境修复任务 |
| `POST` | `/api/workflows/environment/repair` | 修复研究执行环境依赖 |
| `POST` | `/api/workflows/environment/select` | 选择研究执行环境 |

## 研究插件

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/auth/research-plugins` | 列出研究插件 |
| `PUT` | `/api/auth/research-plugins/{plugin}` | 保存研究插件配置 |
| `GET` | `/api/auth/research-plugins/{plugin}/impact` | 查看研究插件影响范围 |

## 研究纪律与自动化

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/rd/assemble` | 积木组装：生成策略代码（create=true 时存成新策略） |
| `GET` | `/api/rd/assemble/options` | 组装可选项：标的池、因子、模型、配置方案、执行方案 |
| `GET` | `/api/rd/brief` | 最新一份晨报 |
| `POST` | `/api/rd/brief` | 生成一份研究晨报（可选推送到通知渠道） |
| `GET` | `/api/rd/brief/{bid}` | 某一份晨报 |
| `GET` | `/api/rd/holdout` | 紧封测试集：紧封线、开封记录、设置留痕 |
| `POST` | `/api/rd/holdout` | 设置（或调整）紧封线：之后的数据在研究里看不到 |
| `POST` | `/api/rd/holdout/remove` | 解除紧封（留痕）：之后研究会用到全部数据 |
| `POST` | `/api/rd/holdout/unseal` | 开封：用紧封测试集评估一个成果。每个成果只能开封一次，结果永久记录 |
| `GET` | `/api/rd/ledger` | 试验台账：各研究类型的试验次数，每个研究方向的最好成绩按试验次数修正（DSR / t 门槛） |
| `POST` | `/api/rd/ledger/pbo` | 过拟合概率（CSCV）：同一研究项目的各个版本，或同一策略的历次回测 |
| `GET` | `/api/rd/lineage` | 血缘图：信息源 → 因子 → 模型 → 组合 / 执行方案 → 策略 → 回测，以及需要重跑的提示 |
| `GET` | `/api/rd/nightshift` | 定时任务：预算设置、任务队列、正在执行的批次与历史 |
| `PUT` | `/api/rd/nightshift/config` | 修改定时任务预算与执行时间 |
| `POST` | `/api/rd/nightshift/run` | 立即执行一次定时任务（后台运行） |
| `GET` | `/api/rd/nightshift/shifts/{sid}` | 一次定时任务执行的详情 |
| `POST` | `/api/rd/nightshift/stop` | 停止正在执行的定时任务（当前项目保留到最近一版） |
| `POST` | `/api/rd/nightshift/tasks` | 往定时任务队列里加一个研究任务 |
| `DELETE` | `/api/rd/nightshift/tasks/{tid}` | 从队列里移除（正在跑的不能移除） |
| `GET` | `/api/rd/pipeline` | 总览页的研究流水线：因子、模型、组合、执行、定时任务与纪律各一格 |
| `GET` | `/api/rd/shadows` | 影子运行列表与对比结论 |
| `POST` | `/api/rd/shadows` | 开始影子运行：用和线上实验相同的设置并行模拟候选策略 |
| `GET` | `/api/rd/shadows/{sid}` | 影子运行详情：两条净值、收益差、t 值、替换条件 |
| `POST` | `/api/rd/shadows/{sid}/end` | 结束影子运行：停止候选实验，保留基准 |
| `POST` | `/api/rd/shadows/{sid}/promote` | 替换：停止基准实验，候选成为新的线上版本（仍是模拟，不真实下单） |

## 组合与风控

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/portfolio` | 方案库 |
| `POST` | `/api/portfolio` | 把一次试验存为方案（策略里用 optimize('名字') 调用） |
| `POST` | `/api/portfolio/evaluate` | 评估一份配置方案：滚动组合回测 + 风险分解 + 压力测试 + 有效前沿 |
| `GET` | `/api/portfolio/meta` | 组合与风控元信息：优化器、风险模型、预期收益来源、压力情景、目标指标 |
| `POST` | `/api/portfolio/risk` | 风险体检：给定权重的波动、VaR/CVaR、风险贡献与压力测试 |
| `GET` | `/api/portfolio/runs` | 试验记录 |
| `DELETE` | `/api/portfolio/runs/{rid}` | 删除试验记录 |
| `GET` | `/api/portfolio/runs/{rid}` | 试验详情 |
| `DELETE` | `/api/portfolio/{sid}` | 删除方案（试验记录保留） |
| `GET` | `/api/portfolio/{sid}` | 方案详情 |
| `PATCH` | `/api/portfolio/{sid}` | 修改名称、状态、备注 |
| `GET` | `/api/portfolio/{sid}/report` | 方案的完整评估报告 |

## 统一任务中心

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tasks/overview` | 统一任务中心：运行、AI 研究室与定时任务的活动读模型 |

## 统一审批

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/approvals` | 统一审批收件箱：AI 行动 + AI 研究室审批卡片 |
| `POST` | `/api/approvals/{approval_id}` | 批准/拒绝一条统一审批项 |

## 能力矩阵

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/capabilities` | 平台能力矩阵：现货/永续/杠杆边界 |
| `POST` | `/api/capabilities/contracts/verify-bills` | 用交易所账户账单核验 MMR / 资金费 / 强平假设 |
| `GET` | `/api/capabilities/contracts/{exchange}/{symbol}` | 读取交易所合约规格与研究假设 |

## 自适应与衰减

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/lifecycle` | 自适应与衰减总览：衰减判定、模型重训计划、市场状态与建议权重 |
| `POST` | `/api/lifecycle/check` | 立刻对运行中的实验做一次衰减判定 |
| `POST` | `/api/lifecycle/{session_id}/demote` | 按衰减监测的建议把策略退回上一阶段（需要人确认） |
| `POST` | `/api/models/{mid}/retrain` | 立刻重训一次（只用截至重训时点的数据） |
| `POST` | `/api/models/{mid}/retrain-plan` | 设置模型的定期重训计划 |

## 运行节点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/bot` | 聊天机器人列表与已绑定的聊天 |
| `POST` | `/api/bot` | 添加聊天机器人（飞书 / 企业微信 / Telegram） |
| `POST` | `/api/bot/bind-code` | 生成一次性绑定码 |
| `DELETE` | `/api/bot/bindings/{binding_id}` | 解除聊天绑定 |
| `POST` | `/api/bot/hook/feishu/{bot_id}` | 飞书事件回调 |
| `GET` | `/api/bot/hook/wecom/{bot_id}` | 企业微信回调地址校验 |
| `POST` | `/api/bot/hook/wecom/{bot_id}` | 企业微信消息回调 |
| `DELETE` | `/api/bot/{bot_id}` | 删除聊天机器人 |
| `GET` | `/api/node/board` | 手机看板：运行中的实验与节点状态（只读） |
| `GET` | `/api/node/health` | 节点健康：心跳、失联实验、时钟偏差、限频 |

## 部署运维

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/admin/announce` | 发布公告：给全部账号或某个工作区发站内通知（不受屏蔽影响），可同时挂顶栏横幅 |
| `GET` | `/api/admin/audit` | 全站审计日志（可按操作、账号、工作区筛选） |
| `GET` | `/api/admin/overview` | 服务器总览：版本、运行时长、数据库与磁盘、worker、运行队列、账号与模型用量 |
| `GET` | `/api/admin/settings` | 平台设置：注册方式、顶栏横幅 |
| `PUT` | `/api/admin/settings` | 修改平台设置 |
| `GET` | `/api/admin/usage` | 全站模型用量：按天、按工作区、按账号、按服务商 |
| `GET` | `/api/admin/users` | 全部账号：搜索、按状态筛选，附所在工作区数与近 30 天模型费用 |
| `POST` | `/api/admin/users` | 直接创建账号（临时密码只显示一次），可同时加入某个工作区 |
| `GET` | `/api/admin/users/{uid}` | 账号详情：所在工作区与角色、活跃会话、各工作区模型用量、最近操作 |
| `POST` | `/api/admin/users/{uid}/admin` | 授予 / 撤销部署管理员 |
| `POST` | `/api/admin/users/{uid}/ban` | 封禁账号：立即踢下线，之后无法登录（附原因，登录时提示） |
| `POST` | `/api/admin/users/{uid}/reset-password` | 重置密码：生成临时密码（只显示一次），并作废全部会话 |
| `POST` | `/api/admin/users/{uid}/sessions/revoke` | 强制下线：作废该账号的全部会话 |
| `POST` | `/api/admin/users/{uid}/unban` | 解封账号 |
| `GET` | `/api/admin/worker-config` | 读取 worker 槽位配置 |
| `PUT` | `/api/admin/worker-config` | 设置 worker 槽位；超出槽位的任务自动排队 |
| `GET` | `/api/admin/workspaces` | 全部工作区：所有者、成员数、群组数、近 30 天模型费用与运行数、状态 |
| `PUT` | `/api/admin/workspaces/{wid}/status` | 停用 / 恢复工作区（停用后成员无法进入，数据保留） |
