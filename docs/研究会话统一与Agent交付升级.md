# 研究会话统一与 Agent 交付升级记录

日期：2026-10-22。本轮目标：把 AI 研究室的 LabProject 泛化为通用 ResearchSession，让「问 AI」默认是临时轻量会话、只在用户点击升级或服务端建议时进入持久研究；同时补桌面 Agent 的本地文件/Word/PPT 交付能力、统一任务读模型、默认安全值与桌面加固。

## 已完成

### 1. 会话模型
- `lab_state.py`：`kind`（strategy/factor/model/portfolio/execution/research/workflow）、`origin`（created/ask_upgrade/workflow_escalation）、`context`（工作流/运行/节点/页面引用，限长 20KB）、`instructions`（项目级研究规则，20k 上限）。
- `routes_lab.py`：research/workflow 轻会话创建；`POST /api/lab/projects/from-ask` 把临时问答升级为研究会话（种子消息、默认私有、归属当前用户）。
- `research_profile.py`：research/workflow 使用“通用研究/交付”提示词，不再强制策略代码/回测；项目 instructions 注入聊天、计划、团队、报告。
- 私有/共享继续复用现有 `claim_new`/`guard_visible`/`filter_visible`/分享 API；默认私有，显式共享；本地部署固定私有。

### 2. 问 AI
- 浮层主按钮改为打开临时问答（不保存）；面板头部显示「QuantLab Bot · 临时问答 · 不保存」。
- 有研究上下文时可升级为研究会话；服务端 `suggest_session/reason` 触发「适合升级为研究会话」建议卡，不自动创建。
- 「新建研究」入口保留，用于明确需要持久项目时。
- 会话列表展示 kind 徽标与私有/共享状态（本地显示「仅自己」）。

### 3. Agent 本地交付
- `local_read_text`（分页读文本）、`local_write_text`（overwrite/append 分段写）、`local_make_dir`。
- `local_export_docx` / `local_export_docx_file`、`local_export_pptx` / `local_export_pptx_file`（Markdown/JSON → Word/PPT，16:9，含备注）。
- 相对路径按本地项目根解析；未授权/越界/密钥目录拒绝；写操作走审批类别 save。
- 解决了“一次把 1.5 万字 Markdown 塞进工具参数导致参数为空”的问题：先分段写 `.md`/`slides.json`，再导出。

### 4. 任务与桌面加固
- `GET /api/tasks/overview`：只读聚合普通 run、AI 研究室任务、定时任务，统一 state/progress/href，不新增执行引擎。
- `GET /api/approvals`：统一审批收件箱，把 `ai_actions` 的待确认行动与 AI 研究室 `kind=ask` 审批卡片投影成同一结构；`POST /api/approvals/{id}` 分别转发到原 `confirm` / `lab.decide`，不新增审批表、不绕过原权限。测试 `test_approvals.py` 覆盖空态、AI 行动拒绝、Lab 卡片与非法 id。
- `GET /api/lab/projects/{pid}/context-pack`：统一上下文包（format `quantlab.context-pack/1`），输出系统规则、项目规则、来源引用、上下文明细与预算；只读投影，不改变提示词。测试 `test_context_pack_endpoint_projects_rules_and_sources`。
- 前端新增「待审批」页（`web/src/pages/Approvals.tsx`，导航「自动化 → 待审批」）：统一展示 AI 行动与研究室审批卡片，可直接批准/拒绝或跳回原对话；本地模式实测空态渲染无 pageerror。
- 真实资金：`execution.real_spot` 默认关闭；本地项目设置页显式开启 + 二次确认；服务器仍由管理员策略控制。
- 陈旧后端：`/api/healthz` 带版本/构建；Tauri 复用前校验版本与项目根，不一致优雅关闭并新起；日志落 `.quantlab/tauri.log`。
- 配置写入失败回退项目 `.quantlab`；首次/已有项目落点由后端 `first_run` 决定，减少启动闪烁。
- 错误 UX：常见 API 错误映射人话+修复动作，技术详情可展开。
- SQLite 升级前自动备份（`quantlab.db.bak-*`）；发布/升级流程见 `docs/发布与升级.md`。
- i18n：修复 164 条英文模板占位符字面量 `(.+?)`，补 Ask/Bot/本地工具等文案；extractor missing 0。

## 实测与验证
- 真实 OpenCode Go 对话：Ask 一次性问答成功（`mode=llm`，model `opencode/deepseek-v4.1-flash`）；Lab Agent 成功浏览本地目录、读取文本、调用工具。
- research/workflow 会话工具表回归：`test_research_kind_hides_strategy_only_tools` 验证 `write_strategy`、`run_backtest` 等策略入口不再暴露；strategy 会话仍保留。
- 本地 Chrome UI 验收：首页、工作流页、临时 Ask 面板正常；临时徽标/新建研究/升级条可见；无 pageerror。
- 后端：`test_research_session`、`test_local_agent_deliverables`、`test_task_overview`、`test_migration_backup`、`test_ask_ai`、`test_local_projects`、`test_strategy_lab` 共 107 passed, 1 skipped。
- 前端：`tsc -b`、`npm run build:windows` 通过；`cargo check --release` / `cargo build --release` 通过。
- i18n：`plain 7156 (missing 0), templates 691 (missing 0)`。
- **作业2自主交付验收（deepseek-v4-pro）**：桌面本地 Agent 在 `作业2/Agent完成-20261022/` 自主产出：
  - `实验报告.md` 54,536 B（约 23,374 字符，其中中文 12,085 字；含年度网络统计、训练复现、业务解释、可信度自评、消融表与核心代码）
  - `实验报告.docx` 59,622 B（163 段 / 7 表）
  - `slides.json` 8,103 B；`汇报PPT.pptx` 87,397 B（21 页，16:9；已用新深色封面/章节页/浅色内容页模板重排）
  - `交付说明.md` 3,594 B（来源、关键数字、限制、自查）
  - 所有关键数字与《关键证据摘要.md》一致；明确写入 T-HGNNs AUC 最高但 F1 不最高、未上市子组 0 正例、授信缺失、point-in-time 不可保证。
  - 消融表（第十六节）由 Agent 用《关键证据摘要.md》§2b 的真实数值生成；代码附录（第十七节）因 Agent 补强轮反复退回策略工具，由 Lead 按同一 `models.py` 原样补入，未改动报告主体。
  - 不足：PPT 未增补代码页；年度网络/训练复现/业务解释/可信度自评四节由 Lead 基于真实证据补写，Agent 主体与消融表保持原样。

## 未完成 / 限制
- 报告中文约 12,085 字，达到 12k 目标；PPT 未增补代码页。四节扩写与代码附录由 Lead 补入，Agent 主体与消融表未改动。
- 仍需后续：视觉像素 diff；sidecar 已实际构建并冒烟通过，但未随安装包发布。
- 未提交 Git；工作区保留全部改动供审查。
- 验收用 OpenCode key 已从本地项目删除；临时提高的本地 LLM 日额度已恢复 2,000,000 tokens / 500 calls。

## 扩展（图表/PPT、ContextPack、运行时、MCP、ACL、CI）

- **图表与图片**：新增 local_chart（Pillow bar/line/pie）、GET /api/local/files/image；对话用 Markdown 图片 URL，Word 解析 ![alt](png) 插 6.2in 图，PPT 支持 layout/image。
- **高质量 PPT**：local_export_pptx 改为深色封面/章节页、浅色内容页、强调条、页脚页码与图表页；兼容旧 {title,bullets,notes}。
- **ContextPack 页面**：#/context-pack 与 #/context-pack/<pid>，展示系统规则、项目规则、来源引用、items 与预算。
- **受管 Python / sidecar**：GET /api/local/runtime 做解释器与依赖探测；`scripts/build_sidecar.py` 已实际生成 PyInstaller onedir `quantlab-backend.exe` 并冒烟通过，桌面壳优先加载 `resources/backend/`。
- **MCP/skills/hooks**：.quantlab/skills/*.md 只读技能、attach 到 Lab instructions；hooks 可在工具调用前后执行进程内受信 handler（默认关闭，不执行外部命令）；MCP 支持配置/目录，并在 QUANTLAB_ALLOW_MCP=1 + enabled 时通过受限 stdio 与 HTTP JSON/SSE 做 initialize/tools list/call。
- **会话级 ACL**：GET /api/ai/history 默认只看自己的 AI 会话；管理员可 scope=workspace 查看全工作区。
- **CI/视觉回归**：.github/workflows/ci.yml 增加关键子集、tsc、i18n missing 0、手动 visual job；scripts/visual_check.mjs 检查 pageerror/横向溢出并截图。

