# CI 与视觉回归（QuantLab）

本文记录仓库 CI（`.github/workflows/ci.yml`）的覆盖面、本地等价命令，以及新增的视觉回归基础检查
`scripts/visual_check.mjs` 的用法。目标：提交前能快速发现「后端关键链路坏了 / 前端类型或构建坏了 /
i18n 漏翻 / 页面 pageerror / 横向溢出」这几类回归，且不把截图等大文件带进仓库。

## 1. CI 覆盖（GitHub Actions）

| Job | 触发 | 内容 |
|---|---|---|
| `backend` | push / PR | Python 3.11–3.13 矩阵：ruff check + ruff format check、API 快照与代码一致、**关键子集（仅 3.13）**、全量 `pytest -q` |
| `backend-postgres` | push / PR | PostgreSQL + Redis 下全量 pytest（每个用例独立 schema） |
| `frontend` | push / PR | `npx tsc -b`、**i18n extractor missing 必须为 0**、`npm run build`，并上传 `web/dist` |
| `e2e` | push / PR | 研究流程覆盖、Mermaid 自检、E2E 类型检查、Playwright E2E（SQLite / PostgreSQL 两套存储） |
| `visual` | **手动 `workflow_dispatch`** | 起 8399 后端 + 单端口交付 `web/dist`，用 Chromium 跑 `scripts/visual_check.mjs`，上传 `.tmp/visual` 截图与报告 |
| `compose` | push / PR | `docker compose config` 与 build |

关键子集（快速失败，`backend` job 中先于全量执行）：

```
tests/test_workflows_api.py
tests/test_artifact_pins.py
tests/test_local_projects.py
tests/test_local_agent_deliverables.py
tests/test_ai_tools.py
```

这些用例覆盖工作流 API、成果固定、本地项目/授权、本地 Agent 交付工具与平台工具目录；它们都不依赖
研究环境里的 pandas/pyarrow 等重依赖，因此在最小安装（`pip install -e ".[dev,server]"`）下可稳定运行。
全量 pytest 仍然在 3 个 Python 版本上执行，关键子集只是让明显回归更早失败。

视觉回归 job 只在手动触发时运行（`workflow_dispatch`），避免每次 PR 都拉起真实浏览器：

```
gh workflow run ci.yml            # 或 GitHub Actions 页面点 Run workflow
```

## 2. 本地等价命令

```powershell
# 后端：静态检查 + 关键子集 + 全量
cd QuantLab
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m pytest -q tests/test_workflows_api.py tests/test_artifact_pins.py tests/test_local_projects.py tests/test_local_agent_deliverables.py tests/test_ai_tools.py
python -m pytest -q

# 前端：类型检查 + i18n 提取 + 构建
cd QuantLab\web
node node_modules\typescript\bin\tsc -b
node scripts\i18n-extract.cjs                     # 期望输出里 missing 全为 0
npm.cmd run build

# i18n 机器校验（CI 同款；missing 非 0 时退出码 1）
node scripts\i18n-extract.cjs "$env:TEMP\i18n-missing.json"
node -e "const r=require(process.env.TEMP+'/i18n-missing.json');const n=r.plain.length+r.templates.length;if(n){console.error(n);process.exit(1)}"
```

macOS / Linux（CI 同款）：

```bash
cd QuantLab/web
npx tsc -b --pretty false
node scripts/i18n-extract.cjs "$RUNNER_TEMP/i18n-missing.json" 2>/dev/null || node scripts/i18n-extract.cjs /tmp/i18n-missing.json
node -e "const fs=require('fs');const r=JSON.parse(fs.readFileSync((process.env.RUNNER_TEMP||'/tmp')+'/i18n-missing.json','utf8'));const n=r.plain.length+r.templates.length;if(n){console.error(n);process.exit(1)}"
npm run build
```

`node scripts/i18n-extract.cjs` 输出示例（正常状态）：

```
plain 7164 (missing 0), templates 691 (missing 0)
```

### i18n 说明

- extractor 用 TypeScript 编译器解析 `web/src/**/*.ts(x)`，提取中文界面文案，与 `web/src/i18n/en.json`、
  `web/src/i18n/landing.en.json`、`web/src/i18n.en.ts` 的模式表比对；已翻译的不计入 missing。
- CI 把结果写到 `$RUNNER_TEMP`（不进仓库）；missing 非 0 时打印前 20 条并失败，便于直接补词条。
- `npm run i18n:check` 是另一个检查（真实浏览器逐页查英文界面中文残留，需要已启动的后端），
  与 extractor 互补：extractor 查「源码里没词条」，浏览器检查查「渲染后仍有中文」。

## 3. 视觉回归检查 `scripts/visual_check.mjs`

### 做什么

- 复用已启动的后端（默认 `http://127.0.0.1:8399`），不自动启动、不自动安装依赖/浏览器；
- 用 Playwright 启动**系统 Chrome**（`channel: 'chrome'`，CI 可改 `--channel chromium`）；
- 依次打开 `#/`（首页）、`#/approvals`（待审批）、`#/lab`（AI 研究室）、`#/context-pack`（上下文包），
  检查 `pageerror` 与横向溢出（`document.documentElement/body scrollWidth > window.innerWidth`，容差 2px），
  横向溢出时尽量定位前 5 个越界元素；
- 每个页面截图 + 汇总 `report.json`，默认写到 `.tmp/visual/`（不提交；脚本会在 `.tmp/` 写入自忽略的
  `.gitignore`，因此 `git status` 保持干净）；
- 退出码：`0` 全通过；`1` 后端不可用 / 浏览器缺失 / pageerror / 横向溢出；`2` 参数错误。

### 用法

```powershell
# 1) 先启动后端（账号模式；需要 web/dist 才能把前端一起交付）
cd QuantLab
cd web; npm.cmd run build; cd ..
python -m quant_agent.cli serve --port 8399

# 2) 另开终端跑视觉检查
cd QuantLab
node scripts/visual_check.mjs
node scripts/visual_check.mjs --help
node scripts/visual_check.mjs --base http://127.0.0.1:8399 --headed
node scripts/visual_check.mjs --pages home,lab --width 1100 --height 800
node scripts/visual_check.mjs --channel chromium      # CI：先 npx playwright install chromium
```

本地桌面模式也可以起 8399 后端（项目目录换成实际项目）：

```powershell
python -m quant_agent.cli serve --local --project C:\path\to\项目 --port 8399
```

### 常用选项

| 选项 | 默认 | 说明 |
|---|---|---|
| `--base <url>` | `QUANTLAB_BASE` 或 `http://127.0.0.1:8399` | 后端地址 |
| `--out <dir>` | `.tmp/visual` | 截图与 `report.json` 输出目录 |
| `--pages <keys>` | `home,approvals,lab,context-pack` | 要检查的页面 |
| `--channel <name>` | `chrome` | Playwright 浏览器通道；CI 用 `chromium` |
| `--width/--height` | `1440/900` | 视口，用于 1100 断点等检查 |
| `--timeout <ms>` | `20000` | 后端探测与页面超时 |
| `--headed` | 关 | 显示浏览器窗口 |
| `--no-login` | 关 | 不尝试注册/登录 |
| `--allow-overflow` | 关 | 只记录横向溢出，不判失败 |

登录：脚本先用 `/api/auth/me` 检查会话；未登录时用 `QUANTLAB_VISUAL_EMAIL` /
`QUANTLAB_VISUAL_PASSWORD`（默认 `qa-admin@example.com` / `Passw0rd!`）尝试注册、再登录。
**用 QA 账号注册/登录成功后**，脚本会把该账号的 onboarding 标为 `skipped`，避免「首次见面」引导遮住截图；
已有会话（例如本地桌面模式）不会改动用户状态，若该用户还没做完引导，截图会包含引导浮层。
账号模式下如果注册被限制，可显式设置这两个环境变量。

### 输出

```
.tmp/visual/
  home-1440x900.png
  approvals-1440x900.png
  lab-1440x900.png
  context-pack-1440x900.png
  report.json
```

`report.json` 关键字段：

```json
{
  "format": "quantlab.visual-check/1",
  "base": "http://127.0.0.1:8399",
  "viewport": { "width": 1440, "height": 900 },
  "pages": [
    {
      "key": "lab",
      "page_errors": [],
      "console_errors": [],
      "horizontal_overflow": { "innerWidth": 1440, "scrollWidth": 1440, "overflow": false, "offenders": [] },
      "screenshot": ".tmp/visual/lab-1440x900.png",
      "ok": true
    }
  ],
  "summary": { "pages": 3, "failed": 0, "page_errors": 0, "overflow": 0, "console_errors": 0 },
  "ok": true
}
```

`console_errors` 只记录不判失败（例如第三方资源 404），失败判据是 pageerror、横向溢出与导航失败。

## 4. 排障

| 现象 | 处理 |
|---|---|
| `✖ 无法连接后端：GET .../api/healthz` | 先启动 8399 后端（见上文），或用 `--base` 指向已有实例；脚本会打印启动命令提示并以 1 退出。`/api/healthz` 是快速存活探针；`/api/health` 会探测交易所连通性，可能耗时几十秒，脚本只把它作为旧版本兜底 |
| `无法启动浏览器（channel: chrome）` | 安装 Google Chrome；或 `--channel chromium` 并先 `npx playwright install chromium` |
| `未找到 Playwright 依赖` | 在仓库根执行 `npm.cmd ci`（Windows）/ `npm ci` |
| `lab` 页 pageerror | 打开 `report.json` 的 `page_errors`，按栈定位；这是真实前端异常，不是截图 diff |
| 横向溢出 | 看 `horizontal_overflow.offenders`（tag/class/right/width），检查 1440 与 1100 两个宽度 |
| 截图想提交/对比 | 不要提交 `.tmp/`；需要基线时把结论或压缩图放到 `docs/images/` 并单独评审 |
| i18n missing 非 0 | 按输出词条补 `web/src/i18n/en.json` / `i18n.en.ts`；不要靠改 extractor 绕过 |
