# MCP、技能（skills）与钩子（hooks）

本文说明 QuantLab 后端「扩展能力」的最小闭环：**技能只读加载**、**hooks 只登记**、
**MCP 只保存配置与静态健康检查**。三者都不执行外部命令，也不启动任意进程。

> 这不是完整的第三方 MCP 执行器，也不是任意脚本插件系统。遇到需要执行外部程序的能力，
> 必须走显式、受信任的后续实现，不能通过本页接口绕过审批直接运行。

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
  **默认 `enabled=false`**，当前不会在工具调用时运行；
- HTTP 只提供只读列表，没有注册/启用/删除接口；只有后端受信任代码可以调用
  `quant_agent.agents.hooks.register_hook()` 做进程内登记；
- 不读配置文件里的命令行，不启动子进程，不访问网络。

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
  "execution": "not_implemented",
  "external_commands": false,
  "note": "hooks 当前只做进程内注册与只读展示：默认全部关闭，不执行外部命令……"
}
```

其它方法（POST/DELETE 等）返回 `405`。

## 三、MCP（配置 / 目录 / 健康检查）

MCP 部分**只做配置管理**：

- 保存 server 名称、传输方式、命令 / 参数 / URL、描述、启用标记；
- 读取配置目录并给出静态健康状态；
- **未实现完整 MCP 调用与传输**：不会启动 stdio 进程、不会连接 HTTP 端点、不会握手、
  不会拉取工具列表。

### 配置存储

配置保存在当前工作区的本地 JSON 集合 `mcp_servers`（`<工作区数据目录>/store/mcp_servers.json`），
不写系统配置、不改 PATH、不生成可执行文件。最多 50 条。

### 接口

#### `GET /api/mcp/servers`

```json
{
  "servers": [
    {
      "id": "demo-local",
      "name": "demo-local",
      "transport": "stdio",
      "command": "python -m demo_mcp",
      "args": ["--stdio"],
      "url": "",
      "description": "仅配置",
      "enabled": false,
      "created_at": 1760000000.0,
      "updated_at": 1760000000.0,
      "health": {
        "status": "disabled",
        "started": false,
        "implemented": false,
        "detail": "MCP 目前只做配置 / 目录 / 健康检查：未实现完整 MCP 调用与传输，不启动任意进程、不建立连接。"
      },
      "executes_processes": false
    }
  ],
  "count": 1,
  "max_servers": 50,
  "transports": [
    {"id": "stdio", "label": "本地进程（仅保存配置）", "implemented": false},
    {"id": "http", "label": "HTTP 端点（仅保存配置）", "implemented": false}
  ],
  "implemented": false,
  "executes_processes": false,
  "note": "MCP 目前只做配置 / 目录 / 健康检查：未实现完整 MCP 调用与传输，不启动任意进程、不建立连接。"
}
```

健康状态取值：

| status | 含义 |
| --- | --- |
| `disabled` | 配置未启用（默认），从未探测 |
| `not_probed` | 已启用配置，但当前版本不做真实探测 |

`started` 恒为 `false`：后端没有启动任何 MCP 进程。

#### `POST /api/mcp/servers`

请求体：

```json
{
  "name": "demo-local",
  "transport": "stdio",
  "command": "python -m demo_mcp",
  "args": ["--stdio"],
  "url": "",
  "description": "仅配置",
  "enabled": false
}
```

校验：

- `name`：`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`；
- `transport`：`stdio` / `http`；
- `stdio` 必须有 `command`；`http` 的 `url` 必须以 `http://` / `https://` 开头；
- `args` 最多 32 项、单项 ≤200 字符；`command` ≤500；`url` ≤1000；描述 ≤500；
- 不允许空字符；超过 50 条配置返回 `400`。

保存只写 JSON，返回 `{"server": {...}, "note": "未实现完整 MCP 调用……"}`。
重复提交同名配置为更新（`created_at` 保留、`updated_at` 刷新）。

### 明确未实现

- 不启动 stdio 子进程、不执行 `command` / `args`；
- 不建立 HTTP/SSE 连接，不做 MCP initialize / tools/list / tools/call；
- 不加载第三方 MCP SDK，不运行任何远程代码；
- 健康检查是静态的：不会因为保存配置而产生网络或进程副作用。

后续若要实现完整调用，需要单独设计：受信任 server 白名单、显式启用、进程生命周期与
停止清理、超时与输出上限、审计与审批。当前接口通过 `implemented=false`、
`executes_processes=false`、`health.started=false` 与 `note` 字段显式标注这些限制。

## 测试

```bash
python -m pytest tests/test_skills_hooks_mcp.py -q
```

覆盖：技能空目录 / 列表 / 详情 / 路径穿越与非法名 / 超大文件（413）/ 账号模式工作区级目录 /
技能注入项目 instructions / hooks 只读且默认关闭 / MCP 空配置 / 保存配置不执行命令（marker 文件不存在）/
配置校验 / 健康状态静态标注。

## MCP stdio probe/call（新增）

设置 QUANTLAB_ALLOW_MCP=1 并重启本地服务后，已启用（enabled=true）的 stdio server 可：

- POST /api/mcp/servers/{name}/probe：启动子进程，走 initialize + 	ools/list，返回 tools 后立即关闭；
- POST /api/mcp/servers/{name}/call：body {tool, arguments}，返回 	ools/call 结果。

安全限制：

- 仅本地项目模式；账号模式 403；
- 命令 basename 只允许 python/python3/node/uvx/uv，不使用 shell；
- 每次调用短生命周期，不常驻；HTTP JSON/SSE 已支持，其他传输仍未实现；
- 未设置 allow 时，probe/call 返回 403 并提示环境变量。

## hooks 执行（新增）

- 内置 `builtin.audit_before_tool` / `builtin.audit_after_tool` 默认关闭；`POST /api/hooks/{id}/toggle` 仅本地项目或管理员可切换。
- Lab 工具执行器在每次工具调用前后运行已启用 hook；hook 异常只记录，不影响工具。
- `GET /api/hooks` 返回 `execution: "in_process"`、`enabled` 与最近 `events`；不启动进程、不执行外部命令。

