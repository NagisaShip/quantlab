# dsh-quantlab

DeepSeek Harness（DSH）插件：把 **QuantLab 量化研究台** 接进桌面的侧栏。

宿主半端（`lib/`）负责三件事：

1. **管本地后端**：自动起 `python -m quant_agent.local.launcher --serve`，轮询健康检查，
   退出时停进程（`lib/backend.js`）；
2. **OpenAI 兼容模型桥**：把 QuantLab 的 AI 研究室 / Agent 调用翻译成 DSH 的
   `ctx.llm.stream`，平台不用再配外部模型 key（`lib/llm-bridge.js`）；
3. **给 DSH 注册量化工具**：`quantlab_status` / `quantlab_market_bars` /
   `quantlab_search_stock` / `quantlab_api`（`lib/tools.js`）。

浏览器半端 `client.js`（另一个包/另一侧实现）通过 `/dsh-quantlab/*` HTTP 和本插件通信：
侧栏 `QuantLab` 面板用 iframe 内嵌本地后端页面（`status.url`），顶部状态条提供启动/停止/刷新/日志。

> 接口契约见仓库文档 `docs/DSH插件迁移与股票市场.md` §4.1–4.5；本文只写「怎么用 + 实测结果」。

---

## 1. 安装

### 1.1 源码链接安装（开发用，推荐）

```powershell
# 装进 desktop profile（会写 profile 的 package.json + pnpm install，幂等）
pwsh -File <QuantLab>\scripts\install-dsh-plugin.ps1

# 只改 package.json / 不跑 pnpm（调试）
pwsh -File <QuantLab>\scripts\install-dsh-plugin.ps1 -SkipInstall
```

脚本等价于：

```powershell
dsh plugin --profile desktop add link:<QuantLab>\dsh-quantlab
```

装完 **重启 DSH**（或重载插件），侧栏会出现 `QuantLab` 图标。

### 1.2 手动安装

在 `%USERPROFILE%\.dsh\profiles\desktop\package.json` 里：

```json
{
  "dependencies": { "dsh-quantlab": "link:C:/path/to/QuantLab/dsh-quantlab" },
  "dsh": { "profile": { "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "dsh-quantlab"] } }
}
```

然后 `pnpm install`（DSH 自带 pnpm：`resources/runtime/cli` 同目录树里找）。

### 1.3 前置条件

| 依赖 | 说明 |
|---|---|
| Python 3.11+ | 能 `import quant_agent`；QuantLab 的依赖已装（`pip install -r requirements.lock`） |
| QuantLab 仓库 | 插件默认把 `<仓库>/.dsh-project` 当项目目录、`<仓库>/src` 当 `PYTHONPATH` |
| 端口 | 后端只监听 `127.0.0.1`，端口由 OS 随机分配（写进 `<项目>/.quantlab/instance.json`） |

---

## 2. 配置

配置文件：`$DSH_HOME/quantlab/config.json`（默认 `~/.dsh/quantlab/config.json`），
首次启动自动写默认值。设置页「QuantLab」卡片改的就是它（`POST /dsh-quantlab/config`）。

| 字段 | 默认值 | 说明 |
|---|---|---|
| `projectDir` | `<QuantLab 仓库>/.dsh-project` | QuantLab 项目目录（数据、`.quantlab/` 落这里） |
| `python` | `python` | Python 可执行文件 |
| `srcDir` | 自动探测 `<仓库>/src` | 作为 `PYTHONPATH` 传给后端 |
| `autoStart` | `true` | DSH 启动后是否自动拉起后端 |
| `llmBridge` | `true` | 是否启用 OpenAI 兼容模型桥 + 自动登记平台模型服务 |
| `providerName` | `DSH · Harness` | 登记到平台的 provider 显示名（label） |
| `model` | `""` | 固定模型：`provider/model` 或只写 `model`；空 = 用 DSH 当前默认模型 |
| `pollMs` | `5000` | 客户端状态轮询间隔（客户端用，宿主不读） |

环境变量覆盖（不落盘）：`QUANTLAB_PROJECT`、`QUANTLAB_PYTHON`、`QUANTLAB_SRC`。

后端日志：`<projectDir>/.quantlab/dsh-quantlab.log`（追加；`GET /dsh-quantlab/logs` 读尾部）。
插件诊断日志：`$DSH_HOME/quantlab/diag.log`（超过 200KB 轮转到 `diag.log.1`）。

---

## 3. 控制路由

全部注册在 DSH 的 `ctx.webServer`（与 DSH 同源，浏览器/客户端直接 fetch）：

| 方法 | 路径 | 返回 |
|---|---|---|
| GET | `/dsh-quantlab/status` | `{ok,state,url,port,projectDir,pid,startedAt,error,reused,logTail,llmBridge,providerName,provider,model,savedModel,resolvedTarget?,configPath,providerRegistration}` |
| POST | `/dsh-quantlab/start` | 启动/复用后端，返回最新 status；running 后会尝试登记平台模型服务 |
| POST | `/dsh-quantlab/stop` | 停后端（先 `POST /api/local/shutdown`，超时 `taskkill /T /F` 兜底） |
| POST | `/dsh-quantlab/restart` | 重启 |
| GET | `/dsh-quantlab/config` | `{ok,config,status}` |
| POST | `/dsh-quantlab/config` | 合并保存配置；改了 `projectDir/python/srcDir/autoStart` 会重启后端；返回 `{ok,config,...status}` |
| GET | `/dsh-quantlab/logs?lines=80` | `{ok,lines,logTail:[...],text:"…"}`（`text` 是拼好的多行字符串，客户端可直接显示） |

`state` 取值：`starting` / `running` / `stopped` / `error`（异常退出时 `error` 带原因 + 日志尾）。

**实测**（真起 Python 后端，项目目录 `QuantLab/.dsh-project`）：

```json
{
  "ok": true,
  "state": "running",
  "url": "http://127.0.0.1:56255",
  "port": 56255,
  "pid": 9724,
  "projectDir": "C:\\Users\\Xiao\\Desktop\\本科\\区块链金融\\QuantLab\\.dsh-project",
  "startedAt": 1793299002039,
  "error": "",
  "reused": false,
  "logTail": "[2026-10-30 02:36:39] [dsh-quantlab] 启动后端：python -m quant_agent.local.launcher --serve --project … --port 56255（project=…）\n[2026-10-30 02:36:42] [dsh-quantlab] 后端就绪：http://127.0.0.1:56255（pid=9724）",
  "llmBridge": "ready",
  "providerName": "DSH · Harness",
  "provider": "dsh-harness",
  "model": "dsh-default",
  "savedModel": "",
  "configPath": "C:\\Users\\Xiao\\.dsh\\quantlab\\config.json",
  "providerRegistration": { "ok": true, "ready": true, "baseUrl": "http://127.0.0.1:56389/dsh-quantlab/llm/v1", "at": 1793299002125 }
}
```

冷启动到 `running` 实测 **约 2.4–3s**（Python 依赖已装、项目已有 `.quantlab/project.json`）。

---

## 4. 模型桥（OpenAI 兼容）

平台侧把插件当「一个 OpenAI 兼容服务商」，走本机 HTTP：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/dsh-quantlab/llm/v1/chat/completions` | 支持 `model`、`messages`（system/user/assistant/tool + `tool_calls`）、`tools`（function calling）、`stream`、`max_tokens`、`temperature` |
| GET | `/dsh-quantlab/llm/v1/models` | `{object:"list", data:[{id:"dsh-default",…}]}` |

- `stream=true`：`text/event-stream`，逐块 `data: {...}`，末尾 `data: [DONE]`；
  chunk 映射：`text-delta→delta.content`、`reasoning-delta→delta.reasoning_content`、
  `tool-call-delta→delta.tool_calls[]`、`finish.reason.kind→finish_reason`
  （`tool-calls→tool_calls`、`stop→stop`、`max-tokens→length`、`aborted/error→error`）、
  `usage→{prompt_tokens,completion_tokens,total_tokens}`。
- `stream=false`：标准 `choices[0].message`（含 `tool_calls` / `usage`）。
- **provider/model 解析顺序**：插件配置 `model`（`provider/model`）→
  DSH `ctx.agentDefaultModel.currentSelection()` → `ctx.llm.listProviders()` 第一个可用
  provider 的 `listModels()` 第一个 → 都不行回 **502** `{error:{message,code:"no_model"}}`。
- `ctx.llm` 不可用时整条桥回 **503** `{error:{code:"llm_unavailable"}}`，平台仍可运行
  （只是 AI 研究室显示「DSH 模型不可用」）；状态里 `llmBridge:"unavailable"`。
- 每次请求生成唯一 `sessionId`（`dsh-quantlab-<uuid>`）；模型清单缓存 60s。
- 请求里带 `tools` 时会转成 DSH `ToolSchema[]` 交给模型（平台 Agent 可以「调工具」，
  但工具的实际执行仍在平台侧）；带 `temperature`/`max_tokens` 时透传给 `ctx.llm.stream`。

**实测**（本仓库端到端脚本：真 HTTP + 真 Python 后端 + 假 llm；假 llm 用的 provider/model 与
本机 DSH desktop profile 一致）：

```
POST /dsh-quantlab/llm/v1/chat/completions {"stream":true, messages:[{role:"user",content:"你好"}]}
→ HTTP 200 content-type: text/event-stream; charset=utf-8
data: {"id":"chatcmpl-…","object":"chat.completion.chunk",…,"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}
data: {"…","choices":[{"index":0,"delta":{"content":"你好，我是 "}}]}
data: {"…","choices":[{"index":0,"delta":{"content":"DSH 模型桥"}}]}
data: {…"usage":{"prompt_tokens":9,"completion_tokens":6,"total_tokens":15}}
data: {"…","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]

POST 非流式 → {"object":"chat.completion","choices":[{"message":{"role":"assistant","content":"你好，我是 DSH 模型桥"},"finish_reason":"stop"}],"usage":{…}}
GET  /dsh-quantlab/llm/v1/models → {"object":"list","data":[{"id":"dsh-default","object":"model","owned_by":"dsh-harness"}]}
```

本机 DSH desktop profile 的默认模型（供排查参考，插件会自动解析，不用手填）：

```
provider = opencode-go
model    = deepseek-v4.1-flash
（见 ~/.dsh/profiles/desktop/cordis.patch.yml 的 agent-default-model 配置）
```

> 真 DSH 里的 llm 只能等插件装进 profile 后由 Lead 侧 curl 验证；上面是本仓库内
> 用 `lib/index.js` 起的等价 HTTP 服务 + 假 llm 的实测输出。

---

## 5. 平台模型服务自动登记

后端 `running` 且模型桥可用时，插件会把模型桥登记成平台的一个模型服务商，
让 QuantLab 的 AI 功能直接用 DSH 的模型（本地模式写接口必须带
`X-Requested-With: quantlab-desktop`，否则 403「缺少本地写请求标识」）：

1. 读：`GET /api/config` 的 `providers`（若不可用再退 `GET /api/config/providers`）；
   已有 `name=dsh-harness` 且 `base_url` 相同 → 跳过；
2. 写：`POST /api/config/providers`，body：

   ```json
   {
     "name": "dsh-harness",
     "label": "DSH · Harness",
     "type": "openai_compatible",
     "base_url": "http://127.0.0.1:<DSH端口>/dsh-quantlab/llm/v1",
     "api_key": "dsh-harness",
     "models": ["dsh-default"],
     "scope": "workspace"
   }
   ```

3. 路由：`PUT /api/config/routing?scope=workspace`，body `{"default":"dsh-harness/dsh-default"}`——
   **只在 `routing.default` 为空或指向不存在的 provider 时才覆盖**，不动用户已有选择；
4. 校验：`GET /api/config/models` 里 `dsh-harness/dsh-default` 的 `ready` 应为 `true`
   （`key_source` 显示「已加密保存」，说明占位 key 被平台收下了）。

失败重试 3 次、只记 `diag.log`，不阻塞插件启动。DSH 端口每次变化（GUI 重启换端口）时，
`base_url` 不同会触发一次 upsert 更新（name 相同）。

**实测**（上面同一轮端到端脚本，真 Python 后端）：

```
GET /api/config/models → [{"provider":"dsh-harness","model":"dsh-default","target":"dsh-harness/dsh-default","ready":true,…}]
routing.default = "dsh-harness/dsh-default"
GET /api/config → {"name":"dsh-harness","label":"DSH · Harness","scope":"workspace","type":"openai_compatible",
                   "base_url":"http://127.0.0.1:56254/dsh-quantlab/llm/v1","models":["dsh-default"],"has_key":true,
                   "key_masked":"dsh-******ness","key_source":"已加密保存",…}
```

### 真实 DSH 实例里的一次实测（2026-10-30）

插件被装进一个隔离 profile（`DSH_HOME=QuantLab/.tmp/dsh-home-test`，`dsh --profile verify --port 18098`）后：

```
GET  /dsh-quantlab/status              → state=running, llmBridge=ready,
                                         providerRegistration={ok:true, skipped:"已登记（base_url 一致）"},
                                         resolvedTarget={provider:"opencode-go", model:"deepseek-v4.1-flash"}
GET  /dsh-quantlab/llm/v1/models       → {"object":"list","data":[{"id":"dsh-default",…}]}

POST /dsh-quantlab/llm/v1/chat/completions {"messages":[{"role":"user","content":"只回复四个字：模型桥正常"}],"max_tokens":512}
  → HTTP 200
  {"choices":[{"message":{"role":"assistant","content":"模型桥正常",
                          "reasoning_content":"The user wants just four Chinese characters: 模型桥正常"},
               "finish_reason":"stop"}],
   "usage":{"prompt_tokens":38,"completion_tokens":19,"total_tokens":57}}
```

这是**真 DSH 模型**的返回（`ctx.llm.stream` → `opencode-go / deepseek-v4.1-flash`），
`reasoning_content` 也是从 DSH 的 `reasoning-delta` chunk 转出来的。

同一次会话里 profile 的默认 provider 被改过/热更新过，此时会看到：

```
POST … → HTTP 502 {"error":{"message":"llm-deepseek: no API key for provider route \"deepseek-official\"…",
                            "code":"MISSING_CREDENTIAL"}}
```

这不是插件坏了：隔离 profile 没配 `deepseek-official` 的 key，DSH 的
`finish{kind:"error", failure.code}` 被如实翻译成 502 + 原 code。
若拓扑变化后原 provider **整个没注册适配器**（`NO_ADAPTER`），桥会在写出任何 SSE 字节前
清缓存、换一个可用 provider 重试一次；仍不行才把错误交回平台。
想看到成功路径：给该实例配一个可用 provider（或注入 `OPENCODE_GO_API_KEY` 到它的 credentials）。
状态里的 `resolvedTarget: {provider, model}` 显示模型桥实际用的模型（解析成功后才出现）。

---

## 6. DSH 工具

注册到 `ctx.tools`（`defineTool`），模型可以直接调用：

| 工具 | 参数 | 说明 |
|---|---|---|
| `quantlab_status` | — | 服务状态 / 端口 / 项目目录 / 模型桥 / 日志尾 |
| `quantlab_market_bars` | `symbol`(必填), `timeframe`, `exchange`, `limit` | 行情 K 线摘要：末 3 根 + 首末收盘、涨跌幅、最高最低、均量；`exchange="stock"` 即股票（A 股 `600519.SH`、美股 `AAPL.US`） |
| `quantlab_search_stock` | `q`(必填) | 股票搜索（名称/代码） |
| `quantlab_api` | `method`(GET/POST), `path`, `body?` | 通用平台 API，白名单 + 8KB 截断（见下） |

`quantlab_api` 白名单：

- 方法：只有 **GET / POST**；
- 路径前缀：`/api/market/`、`/api/strategies/`、`/api/backtests/`、`/api/research/`、
  `/api/lab/`、`/api/factors/`、`/api/data/`、`/api/models/`；
- 明确禁止：`/api/local/shutdown`、`/api/auth*`、`/api/admin*`；
- 拒绝绝对 URL / `..` / 反斜杠；响应 JSON 截断到 **8KB**（`quantlab_api` 返回
  `{ok:true,truncated:true,json:"<截断后的 JSON 文本>",note:"…"}`）。

服务没启动时所有工具返回：

```json
{ "ok": false, "error": "QuantLab 服务未启动（打开侧栏 QuantLab 面板启动）" }
```

---

## 7. 目录结构

```
dsh-quantlab/
  package.json          # dsh.bundle.patch / dsh.client（客户端 bundle 声明）
  cordis.patch.yml      # - insert: [{id: quantlab, name: dsh-quantlab, config: {}}]
  config.defaults.json  # 默认配置的同款副本（给人看；lib/index.js 里有兜底默认值）
  lib/index.js          # cordis 插件：配置、路由、工具、平台模型登记、diag、dispose
  lib/backend.js        # 后端进程管理（纯 Node，可单测）
  lib/llm-bridge.js     # OpenAI ↔ ctx.llm（可单测）
  lib/tools.js          # quantlab_* 工具 + quantlab_api 白名单
  test/*.test.mjs       # node --test，零第三方依赖
```

宿主可注入点（单测用）：`spawn` / `fetch` / `sleep` / `getFreePort` / `isPidAlive` / `killPid`
（`lib/backend.js`），`getLlm` / `getDefaultModel` / `now`（`lib/llm-bridge.js`），
`home` / `configFile` / `backend` / `bridge` / `defineTool` / `fetch`（`lib/index.js` 的 `setup()`）。

---

## 8. 测试

```powershell
cd <QuantLab>\dsh-quantlab
node --test test/*.test.mjs
```

零第三方依赖（`@deepseek-ai/dsh-tools` 在测试里用最小 shim 代替）。覆盖：

- `test/backend.test.mjs`：状态机、复用 `instance.json`（root 校验）、健康检查超时、
  子进程早退、shutdown 失败 kill 兜底、restart、dispose、日志尾部；
- `test/llm-bridge.test.mjs`：OpenAI messages/tools 转换、chunk 映射、
  provider/model 解析优先级与缓存、流式 SSE、非流式、503/502/400、
  `NO_ADAPTER` 拓扑变化后换 provider 重试；
- `test/tools.test.mjs`：`quantlab_api` 白名单（方法/前缀/危险端点/路径穿越）、
  未启动文案、行情摘要、8KB 截断；
- `test/index.test.mjs`：配置读写默认值 + 环境变量覆盖、控制路由、模型桥路由、
  平台模型登记（含 `X-Requested-With` 头与 routing 逻辑）、工具注册、
  `llm/adapters-updated` 清缓存、dispose 清理。

当前：**69 passed / 0 failed**。

另外用 **真实** `@deepseek-ai/dsh-tools`（从 DSH 安装包 `app.asar` 解包 + 依赖链接）跑过一遍
`defineTool` 校验：4 个工具的参数 DSL 都能编译成合法 JSON Schema，缺必填参数会抛
`ToolArgsError`（例如 `quantlab_api` 缺 `method`/`path`），`render` 返回
`[{type:'text',text}]` 符合约定。参数 DSL 用错（例如把整块 JSON Schema 当成 `parameters`）
会在这里当场报错，所以以后改工具时建议复跑一次。

---

## 9. 已知限制

- **模型桥只桥文本**：OpenAI `image_url` part 会被折成 `[图片]` 占位；DSH 侧纯文本模型
  也不会收到图片。需要视觉请直接用 DSH 对话。
- **`ctx.llm` 是硬依赖**：插件 `inject = ['webServer','tools','llm']`，profile 里没有 llm
  服务时插件不会加载；加载了但服务为空时模型桥回 503，其余功能（面板/工具/后端）正常。
- **一个项目一个实例**：QuantLab 的 launcher 用 `.quantlab/instance.lock` 独占；同项目
  已在跑时插件会**复用** `instance.json`（先校验 `/api/local/info` 的 `root` 一致），
  不会起第二个。非本插件起的实例（例如桌面版 QuantLab 正开着同一项目）也会被复用。
- **DSH 崩溃可能留下后端**：spawn 的 Python 是独立进程；插件 `dispose` 会停它，真机上
  正常退出/重载都会清理；进程级 `exit` 钩子会 taskkill 兜底。极端强杀 DSH 时后端可能
  残留，下次启动会自动复用（不会起两份）。
- **改 `projectDir` 会重启后端**，正在跑的任务会被中断（平台界面会提示恢复）。
- **平台模型登记会写 workspace 级配置**：用占位 key `dsh-harness`（不是真凭据）；
  它只在「模型桥 + 后端都就绪」时执行，失败只记 diag。
- **`quantlab_api` 是白名单不是沙箱**：白名单内的写接口（回测/研究/策略）能真正改平台
  数据；只在本地模式使用，DSH 工具本身受 DSH 的审批/权限体系约束。
- **Windows 用 `taskkill /PID /T /F`**：会连同 Python 子进程树一起结束；其它平台对进程组
  发 `SIGTERM`。
- **股票行情依赖后端实现**：插件只是转发 `/api/market/*`；股票适配器由平台侧（Python）
  提供，插件不合成行情数据。
