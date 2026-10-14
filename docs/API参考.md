# API 参考

平台共 **405 个端点**。本文件由 `scripts/gen_api_doc.py` 从 FastAPI 的 OpenAPI 规范生成，
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
| `POST` | `/api/auth/login` | 登录，建立服务端会话 |
| `POST` | `/api/auth/logout` | 退出登录，作废会话 |
| `GET` | `/api/auth/me` | 当前用户、可访问的工作区与角色 |
| `POST` | `/api/auth/password` | 修改密码（作废全部会话） |
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
| `POST` | `/api/workspace/invites` | 创建邀请码（管理员） |
| `DELETE` | `/api/workspace/invites/{invite_id}` | 作废邀请码（管理员） |
| `GET` | `/api/workspace/members` | 成员与邀请 |
| `DELETE` | `/api/workspace/members/{user_id}` | 移除成员（管理员） |
| `PUT` | `/api/workspace/members/{user_id}` | 修改成员角色（管理员） |
| `PUT` | `/api/workspace/settings` | 修改工作区名称、时区与运行额度（管理员） |
| `GET` | `/api/workspaces` | 我所在的工作区 |
| `POST` | `/api/workspaces` | 新建工作区 |
| `POST` | `/api/workspaces/join` | 凭邀请码加入工作区 |

## 运行与任务

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/runs` | 运行列表（回测、优化、实时模拟等所有长任务） |
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
| `GET` | `/api/research/backtests` | 回测记录列表 |
| `POST` | `/api/research/backtests` | 运行回测（persist=false 即编译运行） |
| `POST` | `/api/research/backtests/compare` | 多次回测对比 |
| `DELETE` | `/api/research/backtests/{bt_id}` | 删除回测记录 |
| `GET` | `/api/research/backtests/{bt_id}` | 回测详情（收益概述、交易、持仓、日志、清单） |
| `GET` | `/api/research/compare-batches` | 策略对比批次列表 |
| `POST` | `/api/research/compare-batches` | 创建策略对比批次：冻结标的、区间、资金与成本 |
| `DELETE` | `/api/research/compare-batches/{batch_id}` | 删除策略对比批次（回测记录保留） |
| `GET` | `/api/research/compare-batches/{batch_id}` | 策略对比批次详情：曲线与指标并排，含压力回测 |
| `POST` | `/api/research/compare-batches/{batch_id}/results` | 登记对比批次里某个策略的回测结果 |
| `GET` | `/api/research/jobs/{job_id}` | 后台任务进度 |
| `GET` | `/api/research/optimizations` | 参数优化记录列表 |
| `GET` | `/api/research/optimizations/{opt_id}` | 参数优化结果与过拟合诊断 |
| `POST` | `/api/research/optimize` | 启动参数优化（样本内 / 样本外，后台任务） |
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
| `GET` | `/api/market/network` | 部署地网络实测状态（72 小时；不足时显示待验证） |
| `POST` | `/api/market/network/probe` | 立即探测一次行情接口可达性 |
| `GET` | `/api/market/quality` | 数据质量报告（缺口 / 重复 / 未收盘） |
| `GET` | `/api/market/summary` | 标的市场概览（价格、波动、区间） |
| `GET` | `/api/market/symbols` | 支持的标的与周期 |
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
| `GET` | `/api/community/posts` | 帖子列表（分类、标签、搜索、排序） |
| `POST` | `/api/community/posts` | 发帖（可附策略与回测快照） |
| `DELETE` | `/api/community/posts/{post_id}` | 删除自己的帖子 |
| `GET` | `/api/community/posts/{post_id}` | 帖子详情与评论 |
| `PUT` | `/api/community/posts/{post_id}` | 编辑自己的帖子 |
| `POST` | `/api/community/posts/{post_id}/accept/{comment_id}` | 问答采纳回答 |
| `POST` | `/api/community/posts/{post_id}/ai-review` | 请 AI 审稿人审阅 |
| `POST` | `/api/community/posts/{post_id}/clone` | 克隆帖子里的策略到我的策略 |
| `POST` | `/api/community/posts/{post_id}/comments` | 评论或回复 |
| `POST` | `/api/community/posts/{post_id}/favorite` | 收藏 / 取消 |
| `POST` | `/api/community/posts/{post_id}/feature` | 设为 / 取消精华 |
| `POST` | `/api/community/posts/{post_id}/like` | 点赞 / 取消 |
| `POST` | `/api/community/posts/{post_id}/pin` | 置顶 / 取消 |
| `GET` | `/api/community/profile` | 本机用户资料 |
| `PUT` | `/api/community/profile` | 修改作者名与简介 |
| `GET` | `/api/community/stats` | 社区统计与贡献榜 |
| `GET` | `/api/community/tags` | 热门标签 |

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
| `GET` | `/api/config/models` | 列出所有可用的 provider/model 组合 |
| `GET` | `/api/config/presets` | 常见模型服务商预设 |
| `POST` | `/api/config/providers` | 登记或更新一个 Provider |
| `DELETE` | `/api/config/providers/{name}` | 删除界面登记的 Provider 与密钥 |
| `PUT` | `/api/config/routing` | 按用途设置模型路由与降级链 |
| `GET` | `/api/config/search` | 联网搜索配置状态（密钥只回显脱敏值） |
| `PUT` | `/api/config/search` | 设置联网搜索密钥（部署级，加密存放；留空删除） |
| `GET` | `/api/config/status` | 各用途是否有可用模型（决定 AI 功能是否降级） |
| `POST` | `/api/config/test` | 真实调用一次模型，验证密钥与连通性 |

## AI 策略室

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/lab/goal/check` | 检查目标是否合理：返回结论与字段级建议值（可选让模型复核） |
| `POST` | `/api/lab/goal/parse` | 解析 /goal 文本：把“夏普>1.2 回撤<15% 4h 近 2 年”转成结构化目标（未出现的字段沿用项目现值） |
| `GET` | `/api/lab/meta` | 策略室元信息：可选模型、团队、默认验收目标、模型是否就绪 |
| `GET` | `/api/lab/projects` | 策略项目列表 |
| `POST` | `/api/lab/projects` | 新建策略项目（可带初始代码、模板或钉住的上下文） |
| `DELETE` | `/api/lab/projects/{pid}` | 删除策略项目（已保存的平台策略不受影响） |
| `GET` | `/api/lab/projects/{pid}` | 项目详情与增量消息（after=已收到的最大序号，since=上次的 server_time） |
| `PATCH` | `/api/lab/projects/{pid}` | 修改项目：标题、模型、验收目标、标的、归档 |
| `POST` | `/api/lab/projects/{pid}/autopilot` | 自动迭代：调研 → 生成 → 回测 → 对照目标改进，结束后询问是否满意 |
| `POST` | `/api/lab/projects/{pid}/backtest` | 用平台回测跑当前版本（cost_scale>1 为成本压力测试） |
| `POST` | `/api/lab/projects/{pid}/code` | 保存手动编辑的代码为新版本（先做沙箱试跑） |
| `POST` | `/api/lab/projects/{pid}/compact` | 压缩对话历史：把之前的对话总结成摘要钉进上下文，之后不再发原文 |
| `POST` | `/api/lab/projects/{pid}/decision` | 回答 AI 的提问：满意（保存为策略）/ 继续优化 / 换个方向 |
| `GET` | `/api/lab/projects/{pid}/events` | 实时推送（SSE）：增量消息与项目状态，回合结束后自动关闭 |
| `POST` | `/api/lab/projects/{pid}/goal` | 设定项目目标（/goal）：写入条件与回测范围，做合理性检查；可选立即开始自动迭代 |
| `POST` | `/api/lab/projects/{pid}/messages` | 发送一条消息：模型带工具协作（/回测 /解释 /团队 /迭代 为快捷指令） |
| `POST` | `/api/lab/projects/{pid}/pins` | 把一段材料钉进模型上下文（调研结论、笔记、回测解读） |
| `DELETE` | `/api/lab/projects/{pid}/pins/{pin_id}` | 把材料移出模型上下文 |
| `POST` | `/api/lab/projects/{pid}/save` | 把当前版本保存为平台策略草稿（可在编辑器、回测、模拟中继续使用） |
| `POST` | `/api/lab/projects/{pid}/stop` | 请求停止正在运行的回合（在下一个检查点停下） |
| `POST` | `/api/lab/projects/{pid}/versions/{v}/restore` | 切换到历史版本（不删除任何版本） |
| `POST` | `/api/lab/projects/{pid}/walkforward` | 分段回测：把回测区间等分几段分别回测，检查策略是否只在某一段有效 |

## 上线验证

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/validation/calibration` | 各交易所校准过的回测成本（回测表单里可以一键套用） |
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
| `POST` | `/api/demo/connections/{cid}/test` | 重新测试模拟盘连接 |
| `GET` | `/api/demo/environments` | 支持的交易所模拟环境 |
| `GET` | `/api/demo/live` | 实时行情：正在形成的 K 线与推送状态 |

## 信息源

| 方法 | 路径 | 说明 |
| --- | --- | --- |
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
| `GET` | `/api/rd/nightshift` | 夜班：预算设置、任务队列、正在跑的班次与历史 |
| `PUT` | `/api/rd/nightshift/config` | 修改夜班预算与时间 |
| `POST` | `/api/rd/nightshift/run` | 立即开始一个班次（后台运行） |
| `GET` | `/api/rd/nightshift/shifts/{sid}` | 一个班次的详情 |
| `POST` | `/api/rd/nightshift/stop` | 停止正在跑的班次（当前项目保留到最近一版） |
| `POST` | `/api/rd/nightshift/tasks` | 往夜班队列里加一个研究任务 |
| `DELETE` | `/api/rd/nightshift/tasks/{tid}` | 从队列里移除（正在跑的不能移除） |
| `GET` | `/api/rd/pipeline` | 总览页的研究流水线：因子、模型、组合、执行、夜班与纪律各一格 |
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
