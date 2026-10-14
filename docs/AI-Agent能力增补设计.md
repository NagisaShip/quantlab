# AI Agent 能力增补设计（V1.0 增补）

> 本文是 `plan.md`（《加密货币量化交易仿真验证平台 产品与技术设计方案 V1.0》）的增补章节。
> 日期：2026-10-12　范围：在原有"交易所 API — 策略 — 仿真验证"闭环之上，增加 **LLM 配置、策略生成 Agent、Prompt 百科、长期 Agent（微信通知）、上下文管理、调研 API 与策略引导** 六项能力。
>
> 定位：让平台从"研究员手动写策略"升级为"**研究员 + AI Agent 协作**"，同时不破坏原方案的核心纪律——**策略只能生成信号/目标仓位，成交来源与时钟由适配器切换，一切结果可追溯**。

---

## 0. 设计总原则

1. **AI 是副驾驶，不是下单者。** LLM 只产出：代码草案、解释、报告、调研摘要、参数建议。任何生成物必须经"测试 → 版本审核 → 回测"三道闸门才能进入实时模拟；实盘权限与 LLM 完全隔离。
2. **复用原平台抽象。** Agent 不新造一套交易逻辑，而是复用 `StrategyVersion / Run / OrderIntent / Ledger` 等对象，只是把"人写代码"变成"人 + Agent 写代码"。
3. **可复现优先。** 每次 Agent 运行记录：模型 ID、提示词版本、温度、工具调用轨迹（trace）、上下文快照、代码哈希。这与原方案"运行 manifest 冻结一切"的精神一致。
4. **凭证分层。** LLM API Key、交易所只读 Key、模拟盘 Key、实盘 Key 分级存放，客户端与日志不可见，实盘与研究网络分离（沿用原方案第 9 章）。
5. **大陆合规边界不变。** 一期以研究、回测、仿真为主；Agent 的"通知""调研"不得被包装成投资建议或收益承诺。

---

## 1. 平台分层总览（增补后）

```
┌──────────────────────────────────────────────────────────────┐
│  交互层    Web 工作台  |  CLI(类似 cc/codex)  |  微信推送出口      │
├──────────────────────────────────────────────────────────────┤
│  Agent 层  策略生成 Agent | 调研 Agent | 报告/复盘 Agent          │
│            长期 Agent(调度+记忆) | 上下文管理器 | 工具注册表        │
├──────────────────────────────────────────────────────────────┤
│  LLM 层    Provider 抽象(OpenAI/Anthropic/DeepSeek/本地)          │
│            密钥管理 | 路由与降级 | 用量与成本统计 | 提示词模板库     │
├──────────────────────────────────────────────────────────────┤
│  原有平台层 交易所网关 | 数据平台 | 策略运行器 | 组合风控 | 撮合账本  │
└──────────────────────────────────────────────────────────────┘
```

新增的四个模块（Agent 层 / LLM 层 / 通知出口 / 提示词库）都挂在原有平台的"控制面"，**不进入运行平面的关键路径**。即：Agent 挂掉不影响行情采集、回测、模拟交易。

---

## 2. 大模型 API 配置（参照 codex / Claude Code）

### 2.1 配置文件

平台提供 `agent.config.toml`（项目级）与 `~/.quant_agent/config.toml`（用户级），后者覆盖前者。示例：

```toml
# ---- 模型 Provider ----
[providers.deepseek]
type      = "openai_compatible"          # openai_compatible | anthropic | local
base_url  = "https://api.deepseek.com/v1"
api_key   = "${DEEPSEEK_API_KEY}"        # 从环境变量读取，不落盘明文
models    = ["deepseek-chat", "deepseek-reasoner"]

[providers.openai]
type      = "openai_compatible"
base_url  = "https://api.openai.com/v1"
api_key   = "${OPENAI_API_KEY}"
models    = ["gpt-4o", "gpt-4o-mini"]

[providers.anthropic]
type      = "anthropic"
base_url  = "https://api.anthropic.com"
api_key   = "${ANTHROPIC_API_KEY}"
models    = ["claude-*"]

[providers.local]
type      = "openai_compatible"          # vLLM / Ollama / LM Studio
base_url  = "http://127.0.0.1:11434/v1"
api_key   = "not-needed"
models    = ["qwen2.5-coder:14b"]

# ---- 角色路由：不同任务用不同模型，控成本 ----
[routing]
default        = "deepseek/deepseek-chat"
code_gen       = "deepseek/deepseek-chat"      # 策略代码生成
reasoning      = "deepseek/deepseek-reasoner"  # 复杂调研/复盘
report         = "openai/gpt-4o-mini"          # 长文报告
fallback       = ["openai/gpt-4o-mini", "local/qwen2.5-coder:14b"]

# ---- 预算与安全 ----
[limits]
max_tokens_per_run   = 200000
max_cost_per_day_cny = 50
allowed_for_code     = true          # 是否允许该 provider 参与代码生成

[trace]
store_prompts = true                 # 保存提示词与响应，便于复现
redact_keys   = true                 # 日志中脱敏密钥
```

### 2.2 Provider 抽象

统一接口（见 `src/quant_agent/llm/base.py`）：

```python
class LLMProvider(Protocol):
    name: str
    def chat(self, messages: list[Message], *, model: str,
             temperature: float = 0.2, tools: list[Tool] | None = None,
             stream: bool = False) -> ChatResult: ...
    def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...
```

- OpenAI 兼容层覆盖 DeepSeek、通义、Kimi、vLLM、Ollama 等绝大多数国产/本地模型。
- Anthropic 层单独适配（messages + tool_use）。
- **降级链**：主模型失败或超额 → 按 `routing.fallback` 依次尝试，全失败则任务标记 `BLOCKED_LLM` 而非静默失败。

### 2.3 CLI（对标 Claude Code / codex 的使用体验）

```
qagent init                 # 初始化配置，交互式选 provider、填 key
qagent chat                 # 进入对话式策略助手
qagent strategy new         # 生成策略骨架（交互问答）
qagent strategy review <v>  # 审查一个策略版本
qagent research "BTC 现货动量策略近一年表现"  # 调研任务
qagent backtest run <run>   # 触发回测
qagent agent run daily      # 手动触发长期 Agent
qagent config set routing.code_gen deepseek/deepseek-chat
```

CLI 首次运行 `qagent init` 时生成配置，密钥写入系统钥匙串或 `.env`（`.gitignore` 强制忽略）。

---

## 3. 策略生成 Agent

### 3.1 目标

让用户用自然语言描述交易想法，Agent 产出一份**符合平台策略 SDK 契约**的 Python 代码 + 参数 schema + 风险约束 + 解释文档，直接进入"已测试 → 已回测"流程。

### 3.2 工作流（多步 + 工具调用）

```
用户: "我想做一个 BTCUSDT 1小时均线突破策略，回撤控制优先"
  │
  ▼
[1] 澄清 Agent    → 补齐缺失参数(周期、均线长度、止损、仓位上限、成本假设)
  │                 工具: ask_user
  ▼
[2] 检索 Agent    → 查平台策略模板库 + 历史相关实验，给出相似策略基线
  │                 工具: search_templates, query_runs
  ▼
[3] 生成 Agent    → 输出 Strategy 代码(实现 initialize/on_bar/on_fill/snapshot)
  │                 工具: read_sdk_docs, write_file
  ▼
[4] 校验 Agent    → 静态检查 + 单元测试 + 契约测试(是否只返回目标仓位)
  │                 工具: run_sandbox_tests, lint_strategy
  ▼
[5] 回测 Agent    → 提交向量化回测 → 事件回测，读指标
  │                 工具: submit_backtest, get_metrics
  ▼
[6] 解释 Agent    → 生成人类可读报告: 逻辑、假设、指标、风险、局限
```

### 3.3 策略 SDK 契约（Agent 必须遵守）

```python
class Strategy:
    def initialize(self, ctx: Context) -> None:
        """只允许通过 ctx 访问时钟、数据、状态；禁止网络/文件/交易所调用。"""
    def on_bar(self, bar: Bar) -> None:
        """只能用已收盘 bar；输出 target_position，不直接下单。"""
        ctx.set_target_position(0.0)   # 目标仓位: -1..1 或按名义金额
    def on_market_event(self, ev: MarketEvent) -> None: ...
    def on_fill(self, fill: Fill) -> None: ...
    def on_timer(self, ts: int) -> None: ...
    def snapshot(self) -> dict: ...
    def restore(self, state: dict) -> None: ...
```

**沙箱红线**：策略进程无外网、无交易凭证、CPU/内存/时长受限（沿用原方案第 5 章）。Agent 生成的代码在沙箱里跑，天然无法"偷偷下单"。

### 3.4 生成物登记

每次生成为 `StrategyVersion` 打标：

| 字段 | 值 |
| --- | --- |
| `created_by` | `agent:<model_id>` 或 `human:<user>` |
| `prompt_hash` | 提示词模板 + 用户输入哈希 |
| `trace_id` | 关联完整 Agent 轨迹 |
| `review_status` | `draft → tested → backtested → ...`（原生命周期）|
| `human_approved` | 布尔；**agent 生成的版本必须 human_approved=true 才能进实时模拟** |

### 3.5 契约检查（生成代码的守门人）

生成的代码不经过人工肉眼审查就直接入库是不负责任的。平台在入库前跑一次静态契约检查：

- 是否出现 `place_order` / `cancel_order` / `withdraw` 等被禁用的方法定义；
- 是否导入 `requests` / `socket` / `http` / `urllib` / `aiohttp` 等网络库；
- 是否存在 `set_target_position`（没有它，策略就没有合法出口）；
- 是否实现 `on_bar` 或 `on_market_event` 回调。

检查不通过时版本仍会入库，但标记为「契约检查未通过」，前端醒目提示，且不能进入实时模拟。
这样既不丢掉 Agent 的产出供人参考，也不让有问题的代码混进运行链路。

---

## 3A. Agent 团队调研（新增需求）

### 3A.1 角色（Role）

角色是团队的最小单元。一张角色卡包含：

| 字段 | 说明 |
| --- | --- |
| `name` / `title` | 角色名与一句话定位 |
| `description` | 在团队里负责什么 |
| `system_prompt` | 完整的系统提示词：职责、必须检查的清单、硬性约束、输出要求 |
| `model` | `provider/model`，**留空则跟随全局路由** |
| `temperature` | 越偏分析越低，越偏质疑越高 |
| `tools` | 可调用的平台只读工具（行情概览、历史实验、模板检索、指标、契约检查、风控校验） |
| `output_format` | 期望的输出结构 |
| `tags` | 用于筛选与自动编排（如 `debate` 模式会优先挑带「质疑」「风控」标签的角色） |

**三类来源**：

1. **预设角色（9 个）**——覆盖一支量化研究团队的主要职能：

   | 角色 | 定位 |
   | --- | --- |
   | 市场分析师 | 行情结构 / 波动 / 市场阶段 |
   | 策略设计师 | 把交易想法翻译成策略规格（假设、信号、参数、失效条件） |
   | 量化工程师 | 按 SDK 契约实现代码，保证可测试、可复现、不越界 |
   | 回测分析师 | 指标解读与可信度评估（样本量、过拟合、压力结果） |
   | 风控官 | 账户/策略/订单/系统四层风控，默认立场是「先假设会出事」 |
   | 反方质疑者 | 专门攻击核心假设、构造反例 |
   | 合规官 | 表述边界，禁止收益承诺与越界表述 |
   | 执行与成本专家 | 撮合假设、滑点、手续费、容量与延迟 |
   | 研究组长 | 汇总共识、标注分歧、给出下一步行动项 |

   预设角色**不可修改、不可删除**，但可以「复制一份」再改——这样升级平台时不会被用户改动冲掉。

2. **自定义角色**——用户手写全部字段。

3. **AI 辅助创建**——用户给一句自然语言需求，平台用一个结构化的「角色卡设计师」提示词让模型
   产出 JSON 角色卡（名称、定位、系统提示词、温度、工具、输出格式、标签）。产出的角色卡以
   `ai_draft` 标记入库，人工确认后才用于正式调研。
   **LLM 不可用时退回模板角色卡**，接口不会因此 500。

**共同红线**：角色被平台统一前置一段「团队共同红线」，包含禁止调用交易所、禁止使用未来数据、
禁止收益承诺、区分事实与推测。用户不必在每个角色提示词里重复写这些。

### 3A.2 团队与编排模式

团队 = 若干角色 + 一种编排模式 + 一个汇总角色。

| 模式 | 执行方式 | 适用 |
| --- | --- | --- |
| `sequential` 流水线 | 角色按顺序接力，后面的角色能看到前面所有产出 | 有依赖的流程：分析 → 设计 → 回测解读 → 风控 → 汇总 |
| `parallel` 并行评审 | 所有角色独立看同一份材料，再整合 | 多视角评审，避免后发言者被前发言者锚定 |
| `debate` 对抗质疑 | 先并行出观点，再由带「质疑/风控」标签的角色针对性反驳，最后汇总 | 高风险决策，专门暴露隐藏假设 |

**预设团队 4 支**：默认调研团队（流水线）、策略落地团队（流水线，面向把想法变成可回测策略）、
多视角评审团（并行）、对抗审查组（对抗质疑）。

**输入材料**：调研主题 + 背景说明 + 平台数据（可自动附上市场概览与指定策略的回测指标）。
材料以结构化 JSON 注入，角色拿到的是可核对的数字，而不是「你猜」。

**失败隔离**：单个角色失败不中断整支团队，该角色记为 `error` 并附上原因（如「未配置 API Key」），
其余角色继续；只要有一个角色成功，整次运行标记为 `done`，否则 `failed`。

### 3A.3 与模型配置的关系

团队调研是「角色级模型配置」最直接的使用场景：让市场分析师用便宜的快模型、回测分析师与质疑者用
强推理模型、汇总角色用表达力好的模型——这是配置层面的能力，不需要改代码。

调用链：`角色.model`（若给定）→ 否则 `routing[角色用途]` → 否则 `routing.default` → 失败按
`routing.fallback` 依次降级。未配置密钥的 Provider 会被**直接跳过**（而不是逐个等超时），
所以整条链的失败是秒级的。

---

## 4. Prompt 提示词百科

独立交付物 `Prompt提示词百科.md`，面向**不懂提示工程的量化研究员**。结构：

1. 提示词基础：角色、任务、约束、输出格式、示例（few-shot）
2. 结构化写法：XML 标签 / Markdown 分隔 / 变量占位 `{{}}`
3. 量化场景模板库：策略生成、策略审查、调研、回测解读、风控、复盘、通知摘要
4. 反模式与排错：幻觉代码、过度拟合暗示、模糊约束
5. 与平台集成：模板如何被 Agent 自动套用，如何自定义

平台侧把百科里的模板落成 `prompts/*.md`，Agent 通过 `prompt_id` 引用，实现"提示词版本化"。

---

## 5. 长期 Agent（对标"小龙虾 / OpenClaw"式常驻）

### 5.1 概念

长期 Agent 是**带记忆、带调度、能主动出活的常驻进程**，与原平台的"实时模拟至少连续观察 30 天"天然契合。

### 5.2 两类长期 Agent

**A. 通知型（Daily Digest）** — 类比小龙虾定时汇报
- 频率：每日/每周（cron）
- 数据源：实时模拟净值、回撤、异常、未匹配对账、任务失败
- 动作：汇总 → 生成中文摘要 → 推送微信/企业微信群
- 示例产物："【每日策略简报】BTC 动量策略 7 日净值 +1.2%，最大回撤 0.8%，无异常订单。"

**B. 高级策略型（Strategy Maintainer）** — 定期更新策略
- 频率：每周/每月
- 数据源：滚动前推验证窗口、参数稳定性、市场阶段
- 动作：提议参数微调或新版本 → **不自动上线**，只生成 `candidate` 版本 + 差异报告 → 推送给人审核
- 红线：**长期 Agent 永远不能直接启用实盘**；它只产出候选与建议。

### 5.3 调度与状态

| 组件 | 说明 |
| --- | --- |
| 调度器 | cron 表达式（本地时区）；复用原方案任务队列 |
| 记忆 | 短期(本次会话) / 长期(向量库,存历史摘要与决策) |
| 幂等 | 每次运行带 `run_key = agent_id + date`，重复触发不重复推送 |
| 失败处理 | LLM 失败 → 降级；推送失败 → 入待发队列重试（原方案要求保留待处理队列）|
| 审计 | 每次运行落 `agent_run` 表：输入、输出、推送目标、状态 |

### 5.4 微信推送通道

大陆可用方案（按合规与稳定性排序）：

1. **企业微信群机器人 Webhook**（推荐）——稳定、格式好、额度宽松
2. **Server 酱 / PushPlus / 钉钉机器人**——个人号兜底
3. **个人微信**——仅通过官方/授权方式，不做爬虫登录，避免封号与合规风险

统一通知接口（见 `src/quant_agent/notify/`）：

```python
class Notifier(Protocol):
    def send(self, title: str, markdown_body: str, *, channel: str) -> bool: ...
```

支持 `wecom`（企业微信）、`serverchan`、`pushplus`、`console`（调试）多通道，配置在 `agent.config.toml`。

---

## 6. 上下文管理

### 6.1 分层

| 层 | 内容 | 生命周期 | 存储 |
| --- | --- | --- | --- |
| 会话上下文 | 当前对话消息 | 单次会话 | 内存 + 可选持久化 |
| 项目上下文 | 策略代码、参数、实验记录 | 项目期 | 复用原平台 DB |
| 长期记忆 | 决策、结论、复盘摘要 | 跨会话 | 向量库(如 pgvector/Chroma) |
| 运行上下文 | 单次 Run 的 manifest | 运行期 | 冻结快照 |

### 6.2 关键机制

- **检索增强**：Agent 需要"之前那个网格策略怎么配的"时，从长期记忆 + 实验库检索，而非塞满上下文。
- **压缩**：超长对话按 token 预算摘要；保留决策与结论，丢弃过程噪声。
- **隔离**：不同账户/策略的会话上下文不共享可变状态（原方案第 5 章要求）。
- **可追溯**：每次 Agent 运行冻结上下文快照哈希，保证"同样输入 + 同样上下文 → 同样输出（在温度=0 且模型不变的前提下）"。

### 6.3 上下文预算表（示例）

| 用途 | 预算 |
| --- | --- |
| 系统提示 + SDK 文档 | ~4k tokens |
| 检索到的历史实验 | ≤ 8k tokens |
| 对话历史（压缩后） | ≤ 16k tokens |
| 当前任务 | 余量 |

---

## 7. 调研 API 与策略引导

### 7.1 调研 API

平台内置一组"研究工具箱"，既是 Agent 的工具，也可被外部调用：

```
GET  /research/market/{symbol}/summary     # 行情概览、波动、阶段
POST /research/strategy/{id}/analyze       # 策略诊断(逻辑/过拟合/成本敏感)
POST /research/compare                     # 多策略仿真对比
POST /research/report                      # 生成可复现验证报告
POST /agent/run                            # 触发一次 Agent 任务
GET  /agent/runs/{id}                      # 查 Agent 轨迹
```

所有接口带 `idempotency_key` 与审计主体，长任务返回 `run_id`（沿用原方案第 11 章）。

### 7.2 策略引导（预置引导语）

平台预置几套"入门引导"，帮助用户从零到一：

- **趋势引导**：选标的 → 选周期 → 均线/通道 → 止损 → 仓位上限 → 成本假设
- **均值回归引导**：偏离阈值 → 持有时限 → 止损 → 震荡/趋势失效场景
- **网格引导**：价格边界 → 库存上限 → 暂停条件 → 手续费敏感性
- **定投再平衡引导**：投入流水 → 再平衡阈值 → 成本
- **基线引导**：买入持有 / 全现金，作为对照基准

引导 = 一组结构化问题 + 默认值 + 对应提示词模板，用户答完即生成可回测的候选策略。

---

## 8. 安全、合规与边界（继承并强化）

- LLM 生成的任何代码**不得**包含交易所下单调用；契约测试会拒绝。
- 实盘启用需人工批准 + 独立权限，与研究/LLM 网络隔离。
- 通知与调研文案禁止出现"保证收益""稳赚"等表述；统一附"仅为研究结果，非投资建议"。
- 密钥：LLM key 与交易所 key 分级，日志脱敏，实盘 key 不入 Agent 环境。
- 大陆边界：一期保持研究/仿真；数据源按原方案第 16 章验收。

---

## 9. 与 GitHub 的关系

所有设计、提示词库、代码骨架、配置样例进仓库。建议结构见交付根目录 `README.md`。仓库需包含：`.gitignore`（含 `.env`、`*.key`、`data/`）、`LICENSE`、`CONTRIBUTING.md`、CI（lint + 单元测试 + 契约测试 + 前端构建）。

---

## 10. 实现落地状态（V0.2）

本文的设计已落地为可运行的实现，不只是骨架。对应关系：

| 设计章节 | 实现位置 |
| --- | --- |
| 第 2 章 大模型 API 配置 | `src/quant_agent/llm/`（Provider 抽象、[OI]兼容/Anthropic、按用途路由与降级）、前端「模型配置」页 |
| 第 3 章 策略生成 Agent | `src/quant_agent/agents/strategy_gen.py` + 契约检查（`api/deps.py::check_strategy_code`） |
| 第 3A 章 角色与团队 | `src/quant_agent/agents/roles.py`、`agents/team.py`、前端「角色库」「团队调研」页 |
| 第 4 章 提示词百科 | `docs/Prompt提示词百科.md` + `prompts/*.md`（10 个任务模板）+ 角色提示词（从角色库同步） |
| 第 5 章 长期 Agent | `src/quant_agent/agents/long_running.py` + `api/routes_ops.py`（cron 调度、待发队列） |
| 第 5.4 节 微信推送 | `src/quant_agent/notify/channels.py`（企业微信 / Server酱 / PushPlus / 控制台） |
| 第 6 章 上下文管理 | `src/quant_agent/context/manager.py`（四层、token 预算、压缩、长期记忆、快照哈希） |
| 第 7 章 调研 API 与策略引导 | `api/routes_market.py`、`agents/guidance.py`（5 套引导） |
| 原方案第 6/7 章 回测与指标 | `src/quant_agent/engine/`（事件驱动回测、成本模型、压力场景、完整指标） |
| 原方案第 11 章 界面 | `web/`（9 个页面的 React 工作台） |

测试覆盖：41 个测试，含引擎正确性、上下文压缩与快照稳定性、通知通道失败与重试、
适配器边界（未收盘标记、模拟盘请求头、不支持功能显式拒绝）、路由降级链、以及全部 API 端点的端到端调用。

**在没有配置任何大模型密钥的情况下**：行情走合成数据并明确标注、Agent 走模板降级、
团队调研逐角色记录「缺密钥」原因、长期 Agent 用确定性模板产出简报。
配好密钥后同一套流程自动切换到真实模型——这是刻意的设计，目的是让交付物在第一分钟就能被打开看，
而不是先卡在配置上。

---

## 10. 落地节奏（在原 12–16 周基础上叠加）

| 阶段 | 增量内容 |
| --- | --- |
| 第 6–9 周 | LLM Provider 层、配置系统、Prompt 百科、基础策略生成 Agent |
| 第 10–12 周 | 上下文管理、长期 Agent 骨架、微信通知、调研 API |
| 第 13–16 周 | 策略引导、Agent 审计与可复现性验证、端到端演练 |

Agent 能力**不阻塞**核心仿真平台验收；即使 LLM 全下线，平台仍按原方案的验收用例独立成立。

## 11. 实现落地状态（V0.3）

| 能力 | 落地情况 |
| --- | --- |
| 策略生成 | `agents/copilot.py`：自然语言 → 平台 SDK 代码 → 沙箱检查 → 合成数据试跑 → 失败回灌模型自动修复（最多 2 轮）；无密钥时按关键词匹配模板并抽取数字参数 |
| 代码修改 / 修复 | 返回完整新代码，前端以差异视图展示，确认后才写入编辑器；修复在无模型时给出规则诊断 |
| 解释 / 回测解读 / 审稿 | 有模型走 LLM；无模型走静态分析与指标规则，界面标注"降级" |
| 模型配置 | 界面登记 Provider 与密钥（本机 secrets.json，接口脱敏）、按用途路由、降级链；`${ENV}` 占位符只在环境变量存在时视为已配置 |
| 调用留痕 | 每次调用记录动作、模式、模型、提示词哈希、试跑结果与耗时（`/api/ai/history`） |
| 红线 | AI 产物一律是草稿；批准绑定版本；模拟交易只接受已批准版本；AI 接触不到任何交易凭证 |

旧的 `StrategyGenAgent`（面向早期 `class Strategy / set_target_position` 接口）仍保留给「规则与模板策略」页使用；
新的研究流程全部走 `StrategyCopilot` 与聚宽式 SDK。
