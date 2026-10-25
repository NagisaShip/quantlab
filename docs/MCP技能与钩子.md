# MCP、技能（skills）与钩子（hooks）

本文说明 QuantLab 的「扩展能力」边界：**技能只读加载**、**hooks 仅运行进程内受信 handler**、
**MCP 配置可管理，并在本地模式显式授权后探测和调用**。

> 这不是任意脚本插件系统。MCP stdio 只能启动白名单可执行文件，HTTP 不自动跟随重定向，
> 两者都要求本地模式、`QUANTLAB_ALLOW_MCP=1` 与服务已启用；账号部署不能调用成员机器上的进程。

## 一、技能（skills）

### 目录

技能是带 Markdown 正文的只读文件，按部署模式放在：

| 模式 | 目录 |
| --- | --- |
| 本地模式 | `<项目>/.quantlab/skills/*.md` |
| 账号模式 | `<数据目录>/workspaces/<工作区 id>/skills/*.md` |

目录不要求预先存在；不存在时列表返回空，不报错，也不会自动创建目录（保持只读语义）。

### 文件规则

- 只看目录**第一层**，不递归子目录；
- 只接受普通 `.md` 文件；符号链接、目录、其它后缀一律跳过，原因写入 `skipped`；
- 技能名 = 文件名去掉 `.md`，必须匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`；
- 单文件上限 **64 KB**（`MAX_SKILL_BYTES`），超过的不读取正文，列表标记 `too_large`，详情返回 `413`；
- 最多列出 **200** 个（`MAX_SKILLS`），多余的标记 `too_many`；
- 不执行、不解析技能内容里的任何指令或代码。

### 接口

#### `GET /api/skills`

```json
{
  "root": "C:\\projects\\demo\\.quantlab\\skills",
  "exists": true,
  "max_bytes": 64000,
  "count": 1,
  "skills": [
    {
      "name": "rsi-playbook",
      "title": "RSI 研究手册",
      "description": "先看超卖，再用成交量确认。",
      "size": 842,
      "modified_at": 1760000000.0,
      "file": "rsi-playbook.md"
    }
  ],
  "skipped": [{"name": "notes.txt", "reason": "not_markdown"}],
  "note": "只读项目/工作区 skills 目录第一层的 .md 文件；不执行技能内容。"
}
```

#### `GET /api/skills/{name}`

返回技能全文（`content`）、标题、描述、大小、修改时间。错误：

- `400` 技能名不合法；
- `404` 不存在 / 不是目录内的普通文件（含符号链接逃逸、路径穿越）；
- `413` 文件超过 64 KB。

#### `POST /api/skills/{name}/attach`（可选注入）

把技能正文追加到某个 Lab 项目的 `instructions`：

```json
{"project_id": "a1b2c3d4e5f6"}
```

返回 `{ok, skill, project_id, chars, truncated, note}`。注入受项目级规则 20k 字符上限约束，
超出时截断并 `truncated=true`。项目不可见 / 不存在返回 `404`。技能内容只是文本，
不会被当作指令自动执行。

### 安全边界

- 技能名先做白名单校验，再拼路径；解析后要求父目录仍是技能目录（挡住符号链接逃逸）；
- 只读：没有任何写入技能目录的接口；
- 路径穿越（`../`、`..\\`、编码后的 `%2e%2e`、多级路径）在路由/名字校验层被拒绝，
  不会读到技能目录之外的文件；
- 列表里的 `root` 是诊断用绝对路径（与本地文件接口一致），不包含文件内容。

## 二、钩子（hooks）

钩子是**进程内注册表**，只有元数据，没有可执行命令：

- 触发点：`before_tool`（工具调用前）、`after_tool`（工具调用后）；
- 内置两条审计类 hook（`builtin.audit_before_tool`、`builtin.audit_after_tool`），
  **默认 `enabled=false`**；启用后由 Lab 工具执行器在调用前后运行；
- HTTP 只允许列出和切换内置 hook，没有注册任意 handler / 删除接口；只有后端受信任代码可以调用
  `quant_agent.agents.hooks.register_hook()` 做进程内登记；
- 不读配置文件里的命令行，不启动子进程，不访问网络；handler 异常只记录，不影响工具结果。

### `GET /api/hooks`

```json
{
  "hooks": [
    {
      "id": "builtin.audit_before_tool",
      "name": "工具调用审计（前）",
      "trigger": "before_tool",
      "enabled": false,
      "source": "builtin",
      "description": "预留：记录工具名与参数摘要；默认关闭，当前不执行。",
      "executes_external_commands": false,
      "execution": "not_implemented",
      "created_at": 1760000000.0
    }
  ],
  "count": 2,
  "enabled": [],
  "triggers": {
    "before_tool": {"label": "工具调用前", "description": "..."},
    "after_tool": {"label": "工具调用后", "description": "..."}
  },
  "execution": "in_process",
  "external_commands": false,
  "events": [],
  "note": "hooks 只运行进程内受信 handler：默认关闭，不启动进程、不执行外部命令。"
}
```

`POST /api/hooks/{id}/toggle` 的 body 为 `{"enabled": true|false}`，仅本地项目或工作区管理员可切换。

## 三、MCP（配置 / 探测 / 调用）

MCP 设置页支持完整的本地管理工作流：

- 新增、编辑、删除和快速启停 server；
- 测试连接并查看 `tools/list` 返回的工具名、说明和 input schema；
- 从工具列表打开调用面板，以 JSON 对象填写参数并查看 `tools/call` 原始结果；
- 所有写操作仍受本地模式 / 工作区管理员权限约束；账号模式只能管理配置，不能调用本机进程。

配置保存在当前工作区的本地 JSON 集合 `mcp_servers`（`<工作区数据目录>/store/mcp_servers.json`），
最多 50 条。保存配置本身不会启动进程或连接网络；探测和调用必须由用户显式点击或在 API 中显式请求。

### `GET /api/mcp/servers`

```json
{
  "servers": [
    {
      "id": "demo-local",
      "name": "demo-local",
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "demo_mcp"],
      "url": "",
      "description": "本地演示服务",
      "enabled": true,
      "health": {
        "status": "not_probed",
        "started": false,
        "implemented": true,
        "detail": "每次 probe 结束后会关闭子进程，不保持常驻连接。"
      },
      "executes_processes": false
    }
  ],
  "count": 1,
  "calls_allowed": true,
  "max_servers": 50,
  "transports": [
    {"id": "stdio", "label": "本地 stdio 进程", "implemented": true},
    {"id": "http", "label": "HTTP JSON / SSE 端点", "implemented": true}
  ],
  "implemented": true,
  "executes_processes": false,
  "note": "默认只保存 MCP 配置，不启动进程；设置 QUANTLAB_ALLOW_MCP=1 且 server enabled 后，可用 /probe 与 /call。"
}
```

`calls_allowed` 只有在本地模式且 `QUANTLAB_ALLOW_MCP=1` 时为 `true`。健康状态：

| status | 含义 |
| --- | --- |
| `disabled` | 配置未启用，尚未探测 |
| `not_probed` | 已启用，但本次进程启动后尚未探测 |

`started=false` 表示没有常驻连接；探测子进程在请求结束前会被清理。

### `POST /api/mcp/servers`

请求体：

```json
{
  "name": "demo-local",
  "transport": "stdio",
  "command": "python",
  "args": ["-m", "demo_mcp"],
  "url": "",
  "description": "本地演示服务",
  "enabled": true
}
```

校验规则：

- `name`：`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`；
- `transport`：`stdio` / `http`；
- `stdio` 必须有 `command`；`http` 的 `url` 必须以 `http://` / `https://` 开头；
- `args` 最多 32 项、单项 ≤200 字符；`command` ≤500；`url` ≤1000；描述 ≤500；
- 不允许空字符；超过 50 条配置返回 `400`。同名提交为更新（`created_at` 保留）。

### `POST /api/mcp/servers/{name}/probe`

执行 `initialize`、`notifications/initialized` 和 `tools/list`，返回：

```json
{"ok": true, "server": "demo-local", "tool_count": 1, "tools": [{"name": "echo", "inputSchema": {}}]}
```

### `POST /api/mcp/servers/{name}/call`

```json
{"tool": "echo", "arguments": {"text": "hello"}}
```

返回 `{"ok": true, "server": "demo-local", "tool": "echo", "result": {...}}`。

### 安全边界

- 仅本地项目模式可 probe/call；账号模式返回 `403`；
- 必须同时满足 `QUANTLAB_ALLOW_MCP=1`、server 已启用、传输方式为 `stdio` / `http`；
- stdio 命令 basename 只允许 `python` / `python3` / `node` / `uvx` / `uv`，使用参数数组且 `shell=False`；
- stdio 每次 probe/call 都是短生命周期子进程，带超时、输出行数和 stderr 上限；
- HTTP 只接受 `http(s)`，不跟随重定向，支持 JSON 与 `text/event-stream` 的 `data:` 行；
- 探测和调用结果按不可信外部输入处理，不自动执行返回内容。

## 测试

```bash
python -m pytest tests/test_skills_hooks_mcp.py -q
```

覆盖技能目录、路径穿越、大小限制、工作区隔离、instructions 注入、hooks 切换与执行、
MCP 配置校验、未授权拒绝、stdio/HTTP probe/call 和保存配置不执行命令。
