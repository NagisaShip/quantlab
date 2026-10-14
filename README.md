# 加密货币量化研究与仿真验证平台（v2.0）

> 对标聚宽（JoinQuant）的研究体验：**写策略 → 回测 → 参数稳健性检验 → 实时模拟 → 与真实成交对照 → 社区复核**，
> AI 作为贯穿全流程的副驾驶。v2.0 按 [`plan.md`](plan.md)（2026-10-12 版）把单机单用户工具升级为**多用户、可部署、
> 账务精确、任务可恢复**的平台。**平台不托管资金、没有真实下单路径；仅为研究结果，非投资建议。**

文档索引：

| 文档 | 内容 |
| --- | --- |
| [docs/v2.0差距对照与验收.md](docs/v2.0差距对照与验收.md) | plan v2.0 的每条要求 → 实现位置 → 守护测试 |
| [docs/策略协议与数据口径.md](docs/策略协议与数据口径.md) | 事件、Signal / TargetPosition、风险层、撮合、记账、指标口径、研究纪律 |
| [docs/运维与恢复手册.md](docs/运维与恢复手册.md) | 部署、配置项、日常检查、状态机与恢复、备份、密钥轮换 |
| [docs/个人账户接入教程.md](docs/个人账户接入教程.md) | CSV 导入与只读 API Key 接入（币安 / OKX） |
| [docs/测试报告.md](docs/测试报告.md) | 407 项测试、黄金样例、PostgreSQL 复跑、浏览器走查 |
| [docs/VibeCoding任务包.md](docs/VibeCoding任务包.md) | 12 张可独立领取的后续开发任务卡 |
| [docs/平台设计说明.md](docs/平台设计说明.md) | **平台完整设计讲解 + 本机运行截图**(面向评审/接手) |
| [docs/API参考.md](docs/API参考.md) · [docs/openapi.json](docs/openapi.json) | 195 个端点（自动生成） |

---

## 快速开始

### 生产 / 多人部署：Docker Compose（PostgreSQL + Redis + API + Worker）

```bash
mkdir -p secrets
python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" > secrets/secret.key
cp .env.example .env          # 修改 POSTGRES_PASSWORD
docker compose up -d --build
```

打开 <http://127.0.0.1:8000>，**第一个注册的账号成为部署管理员**，之后凭邀请码加入。详见运维手册。

### 单机开发（SQLite，无需 Docker）

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1     # Windows
```

```bash
bash scripts/dev.sh                                          # Linux / macOS
```

脚本会安装锁定版本的依赖、构建前端、执行迁移，并**同时启动 API 与 worker**
（API 只受理请求，回测 / 优化 / 实时模拟都由 worker 执行，只开 API 的话任务会一直排队）。

手动方式：

```bash
pip install -r requirements-dev.lock && pip install --no-deps -e .
cd web && npm ci && npm run build && cd ..
qagent migrate
qagent worker &          # 执行面
qagent serve             # 控制面 http://127.0.0.1:8000 ，API 文档 /docs
```

**没有任何模型密钥、没有外网也能完整跑通**：行情回退到明确标注的合成数据；AI 功能走模板与规则降级，界面写明“降级”。

### 配置大模型（任选其一）

- 界面：「模型配置」页（部署管理员）登记服务与密钥；密钥用主密钥加密保存，接口只回显掩码。
- 环境变量：`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DASHSCOPE_API_KEY` 等。
- 配置文件：复制 `agent.config.example.toml` 为 `agent.config.toml`。

支持 DeepSeek、通义千问、Kimi、智谱、OpenAI 兼容接口、Anthropic，以及本地 Ollama / vLLM。

---

## v2.0 架构

```
浏览器 ─▶ API（qagent serve：鉴权、工作区隔离、幂等、只入队）─▶ PostgreSQL ◀─ Worker（qagent worker）
                    │                                                          ├─ 回测 / 参数优化 / 模板实验 / Agent 任务
                    └──── Redis（只做唤醒，可缺省）────────────────────────────▶├─ 实时模拟（常驻驱动 + 每 bar 检查点）
                                                                               ├─ 只读账户同步 / 实盘对照
                                                                               └─ Python 策略子进程（隔离 + 资源上限）
```

| 能力 | v2.0 做法 |
| --- | --- |
| 用户与工作区 | 注册 / 登录 / 邀请码；管理员、成员、审阅者（只读）；所有数据按工作区隔离；配额；审计日志 |
| 账务 | `Decimal` 双分录账本，金额在 JSON 中为十进制字符串；黄金样例 `899.9 + 110 − 0.11 = 1009.79` 逐位精确 |
| 错误与幂等 | 统一 `{code, message, trace_id, details}`；写请求支持 `Idempotency-Key` |
| 长任务 | 控制面不跑长任务；单租约认领 + fencing；`QUEUED/RUNNING/PAUSED/COMPLETED/FAILED/CANCELED/RECOVERING`；检查点恢复不重复成交 |
| 存储 | PostgreSQL（或 SQLite）+ Redis 唤醒 + Parquet（或 JSON）K 线缓存；版本化迁移 |
| 策略 | 规则 DSL v2（零代码，表单 / JSON 双视图）与聚宽式 Python SDK；版本 `DRAFT→VALIDATED→FROZEN→ARCHIVED` 不可改写 |
| 输出协议 | 确定性 `signal_id` 的 Signal → TargetPosition → 独立风险层 → 撮合；为个人执行器预留目标仓位信封 |
| 撮合口径 | 下一事件成交；资金不足默认拒绝；保守限价（触价不成交）；止损优先；手续费币种；未平仓不算完成交易 |
| 指标 | 有出入金时用单位净值（TWR）；回撤峰谷与恢复；最差单日、持仓时长、总手续费；报告附口径声明 |
| 研究纪律 | 锁定测试区间、purge / embargo、walk-forward |
| 真实结果 | `imported_live`（CSV）/ `readonly_live`（只读 Key）/ `testnet`；没有真实成交时显示“未接入真实成交”，不用模拟曲线补位 |
| 个人账户 | 三步只读接入向导；时间、身份、权限、IP 检测；非只读 Key 拒绝且不保存；独立加密；撤销即删除密文 |
| 事件审计 | 每根 K 线带 `event_ts / receive_ts / sequence / source / instrument_id / raw_ref` |
| 网络可用性 | 部署地 72 小时实测，未满 72 小时显示“网络可用性待验证” |
| 安全 | CSV 公式注入防护（导入拒绝、导出中和）；Python 策略独立进程、清空环境变量、CPU/内存/文件上限、降权、可选断网 |

---

## 一次完整的研究流程

1. **我的策略 → 新建策略**：从 10 个模板中选一个（双均线、RSI、布林带、海龟、网格、定投、多币动量轮动、逆波动率配置……），
   或点「用一句话生成策略」让 AI 写。
2. **策略编辑器**：带语法高亮的 Python 编辑器；`Ctrl+Enter` 编译运行、`Ctrl+S` 保存。右侧面板有运行结果、日志、
   AI 助手（修改代码会先展示差异再应用）、参数、历史版本、回测记录。
3. **运行回测 → 回测详情**：聚宽式收益概述（策略/基准/超额收益、Alpha、Beta、夏普、索提诺、信息比率、波动率、
   最大回撤、胜率、日胜率、盈亏比……）、策略 vs 基准曲线、超额、回撤、日收益、`record()` 自定义曲线；
   交易详情、每日持仓&收益、日志、源码快照、运行清单（代码哈希 + 数据摘要 + 成本模型）、AI 解读。
4. **参数优化**：网格搜索，强制切分**样本内 / 样本外**，给出热力图、内外散点、秩相关与过拟合诊断，可一键写回代码。
5. **冻结版本 → 实时模拟**：只有 `FROZEN` 的版本能进入实时模拟。worker 每 30 秒采集，策略在每根新收盘 bar 上运行，
   与回测共用同一套撮合代码；可暂停 / 恢复 / 停止（不平仓）/ 模拟平仓并结束；7 天 / 30 天窗口必须真实经过。
6. **实盘对照**（可选）：导入成交 CSV 或绑定只读 Key，把真实成交逐笔归因到策略，查看滑点、延迟与置信度。
7. **分享到策略广场**：帖子附**冻结快照**（代码哈希、参数、回测曲线与清单），读者一键克隆复核；可请 AI 审稿人公开挑刺。

---

## 与聚宽的对照

| 聚宽 | 本平台 | 说明 |
| --- | --- | --- |
| 我的策略 / 策略编辑器 | 我的策略 / 策略编辑器 | 同样的 `initialize / run_daily / history / order_target_percent / g / log / record` 写法 |
| 编译运行 / 运行回测 | 编译运行 / 运行回测 | 编译运行不入库；运行回测生成完整记录 |
| 回测详情（收益概述、交易详情、每日持仓、日志） | 回测详情 | 指标口径一致；另加运行清单与 AI 解读 |
| 参数调优 | 参数优化 | 额外强制样本内 / 样本外与过拟合诊断 |
| 模拟交易 | 模拟交易 | 与回测同一撮合代码；停机不补算 |
| 社区（文章 / 问答 / 精华 / 标签 / 克隆） | 策略广场 | 分享内容冻结；AI 审稿人 |
| API 文档 | API 文档 | 应用内 `#/docs` |
| — | AI 助手 | 生成 → 沙箱检查 → 合成数据试跑 → 自动修复（最多 2 轮） |

与聚宽的主要差异：加密货币 7×24 交易、现货只做多、默认**下一根 bar 开盘价成交**（不会用收盘价决策又按收盘价成交）、
更严格的策略沙箱。

---

## 功能清单

| 模块 | 内容 |
| --- | --- |
| 策略 SDK | 聚宽式 API；`PARAMS` 参数声明与覆盖；多标的；基准；`run_daily/weekly/monthly`；`record`、`log`、`g` |
| 撮合与成本 | 下一根开盘成交（可切换当根收盘）；taker 费率 + 半价差 + 滑点；成交量占比上限；调仓容差；资金约束 |
| 指标 | 累计/年化收益、基准收益、超额、Alpha、Beta、信息比率、跟踪误差、夏普、索提诺、Calmar、波动率、最大回撤与持续时间、胜率、日胜率、盈亏比、利润因子、换手、手续费 |
| 沙箱 | AST 审查（import 白名单、禁用 eval/exec/open/getattr、禁止下划线属性、禁止文件 IO）+ 受限 `__builtins__` + 独立子进程资源上限 |
| 数据 | Binance / OKX 公共行情；本地 K 线缓存只补缺口；按起止日期取数；来源可选 自动 / 只用真实 / 合成；数据质量报告 |
| 回测记录 | 冻结代码、参数、数据摘要与成本模型；详情、对比、导出 JSON / CSV |
| 参数优化 | 网格（≤150 组）、worker 执行与断点续跑、锁定测试区间、purge / embargo、walk-forward、热力图、写回代码 |
| 实时模拟 | 真实行情 / 演示行情；worker 常驻驱动；暂停 / 恢复 / 停止 / 模拟平仓并结束；7/30 天窗口；检查点恢复 |
| 实盘对照 | CSV 导入（全有或全无、公式注入防护）；只读 Key 同步；逐笔归因（订单标签 → 映射 → 订单号 → 启发式 + 置信度） |
| 规则 DSL | SMA / EMA / RSI / ATR / 通道 / 区间收益；比较、交叉、连续满足、逻辑组合；权重或固定数量；表单与 JSON 双视图 |
| 平台 | 登录注册、工作区与角色、邀请码、配额、审计日志、运行列表与命令、worker 状态、网络实测 |
| AI 助手 | 生成、修改、修复、解释、问答、回测解读、审稿；全部有确定性降级；调用留痕（模式、模型、提示词哈希） |
| 策略广场 | 文章 / 问答 / 策略分享 / 公告；推荐 / 最新 / 回复 / 最热排序；标签、搜索、精华、置顶；点赞、收藏、评论回复、采纳；克隆；贡献榜 |
| 统一赛道（实验三） | 同一行情、资金与成本下多策略公平比较；7/30 天固定窗口；JSON 规则策略；CSV 导入 |
| Agent 协作 | 9 个预设角色 + 自定义；流水线 / 并行评审 / 对抗质疑；长期 Agent 与微信类推送 |
| 模型配置 | 界面登记服务与密钥（持久化）；按用途路由与降级链；连通性测试 |

---

## 目录结构

```
.
├── plan.md                         平台设计方案（v2.0）
├── Dockerfile · docker-compose.yml · deploy/   部署（API 与 worker 同镜像）
├── requirements.lock · requirements-dev.lock   依赖锁定
├── docs/                           协议口径 · 运维手册 · 账户接入 · 测试报告 · 任务包 · API 参考
├── src/quant_agent/
│   ├── platform/     db.py 数据库与迁移 · auth.py 用户工作区 · runs.py 运行队列与租约 · worker.py · isolation.py 策略隔离
│   │                 versions.py 策略版本 · netprobe.py 网络实测 · csvsafe.py · errors.py · secretbox.py
│   ├── sdk/          ledger.py Decimal 账本 · runtimes.py 撮合 / 信号 / 风险层 · api.py 聚宽式 API · sandbox.py
│   ├── strategies/   dsl.py 规则 DSL v2 · sdk_templates.py 模板库 · repo.py 策略仓库
│   ├── live/         imports.py CSV 导入 · attribution.py 归因 · accounts.py 只读账户 · envelope.py 目标仓位信封
│   ├── engine/       research.py 回测与优化 · live.py 实时模拟 · metrics.py 指标口径
│   ├── data/ · exchanges/   行情服务、K 线缓存（Parquet / JSON）、Binance / OKX 适配器
│   ├── agents/ · llm/       AI 副驾驶、团队编排、模型路由与降级
│   └── api/          FastAPI 路由；tasks.py 是 worker 执行的任务处理器
├── web/src/          React + TypeScript：登录、DSL 编辑器、回测详情、实时模拟、实盘对照、个人账户、设置与审计……
├── scripts/          dev.ps1 / dev.sh / gen_api_doc.py / mock_llm_server.py
├── tests/            407 个测试（SQLite 与 PostgreSQL 均通过）
└── 实验三/           课程实验三的独立原型，保持原样
```

---

## 设计要点

**AI 是副驾驶，不是下单者。** AI 只产出代码草案、解释与报告。生成的策略是草稿，经过回测与人工批准后才能进入模拟交易；
**批准绑定版本**——代码或参数一改，批准自动失效。实盘权限与 AI 完全隔离。

**策略只表达目标仓位。** 下单函数与 DSL 都产出 Signal + TargetPosition，经独立风险层后由平台撮合；策略拿不到网络、文件与任何凭证。

**可复现优先。** 每次回测生成运行清单：代码 SHA-256、参数、数据摘要、成本与成交模型、平台版本。AI 调用记录模式、模型与提示词哈希。

**数据来源必须可见。** 每个页面写明数据来自真实接口还是合成数据；合成数据的结论会被明确标为"不能作为策略结论"。
模拟成交、官方测试成交与真实成交三类结果不合并展示。

**不伪造停机。** 实时模拟每根 bar 写检查点，worker 崩溃后从检查点恢复、不重复成交；停机期间的 bar 只推进数据，不补算成交。

**模拟与真实分开。** 全局横幅始终显示当前模式；没有真实成交数据时实盘面板显示“未接入真实成交”。测试网成交永远标为“测试”。

---

## 测试与质量

```bash
pytest -q                                                   # 407 个测试（SQLite）
QAGENT_TEST_PG=postgresql://user@host:5432/db pytest -q     # 同一套测试跑在 PostgreSQL 上
ruff check src tests scripts                                # 静态检查
cd web && npm run build                                     # 类型检查 + 构建
python scripts/gen_api_doc.py                               # 重新生成 docs/API参考.md 与 docs/openapi.json
```

`tests/test_acceptance.py` 逐条对应 plan v2.0 的 10 项必验：未来数据不可见、收盘信号下一事件成交、重复事件不重复成交、
手续费与出入金、版本不可改写、跨工作区不可读、数据中断不下单、重启一致、无真实数据不显示实盘收益、只读模式无下单路径。

---

## 已知局限

- **没有真实下单**：`live_enabled=false`，只读接入只用于对照。目标仓位信封为将来的个人执行器预留。
- 撮合基于 K 线，不模拟盘口队列与市场冲击；限价单用保守模型（触价不成交），会低估限价成交率。
- 策略子进程“断网”需要 Linux 网络命名空间：容器内需叠加 `deploy/docker-compose.isolation.yml`（授予 worker `SYS_ADMIN`），
  Windows 单机开发没有该项与降权，其余（独立进程、清空环境变量）照常。界面会如实显示隔离状态。
- 只读同步当前单次最多回溯 1000（币安）/ 100（OKX）条成交，分页增量同步见任务包 T01。
- 本仓库在无 Docker Hub 访问的环境中开发：`docker compose config` 已校验，镜像构建未在本环境执行；
  同一拓扑（PostgreSQL + Redis + 独立 API 与 worker 进程）已用本地进程完整验证。

---

## 许可

内部 / 课程项目，暂不对外开源。
