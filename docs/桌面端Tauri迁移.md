# 桌面端迁移：从 pywebview 到 Tauri 2

状态：**外壳已实现并在本机完成启动验收**。旧的 pywebview 方案保留但已弃用。

> 下一阶段的研究工作区、成果阅读与 Agent 上下文方案见
> [桌面端研究工作区方案](桌面端研究工作区方案.md)。

## 目标结构

```
Tauri 2（Windows 窗口 / 托盘 / 更新 / 文件对话框 / 拖放 / Python 后端进程管理）
├─ 现有 React 前端（web/dist，由本机 Python 后端同源提供）
└─ 现有 Python 后端（Agent、数据、因子、回测、组合，完全不动）
```

关键决策：

1. **页面仍由 Python 后端提供**：窗口加载 `http://127.0.0.1:<动态端口>/#/home`，与旧 pywebview
   一样同源。这样 React 端所有相对 `/api/*`、SSE、媒体地址、Cookie/CSRF 语义都不用改，
   也不需要给后端加 CORS。Tauri 只作为 WebView 宿主与原生能力桥。
2. **Python 后端作为子进程由 Rust 管理**：复用 `.quantlab/instance.json` 已存活的服务；
   没有则选空闲端口启动 `python -m quant_agent.local.launcher --serve --project … --port …`，
   轮询 `/api/local/info` 就绪后建窗口；退出前 POST `/api/local/shutdown`，有未完成任务时拒绝退出并提示。
3. **前端新增 `web/src/native.ts` 适配层**：Tauri 命令（`native_choose_folder` /
   `native_choose_files` / `native_open_project`）/ 旧 pywebview / 纯浏览器三态统一，
   原有调用点只改这一处；`window.pywebview` 分支保留给旧外壳回退。
4. **拖放**：Tauri 默认拦截系统文件拖放（页面拿不到 File），由 Rust 在
   `WindowEvent::DragDrop` 把绝对路径通过 `quantlab://files-dropped` 事件转发，
   前端按「本机文件引用」接住（可预览、引用或复制），与旧 pywebview 行为一致。
5. **多实例**：沿用旧行为，一个项目一个项目服务；`native_open_project` 用当前 exe
   带 `--project` 再启动一个桌面实例。不同目录互不影响，同一目录复用已有服务。
6. **更新**：接入 `tauri-plugin-updater`，托盘「检查更新」已可用；发布前需要配置
   签名公钥与 endpoints（`src-tauri/tauri.conf.json` → `plugins.updater`）。未配置时明确提示。

## 目录与启动方式

- 工程：`desktop/src-tauri/`（Rust 外壳、配置文件、图标、capabilities）
- 运行：`desktop/run.ps1`（debug 可执行文件；自动设置 `QUANTLAB_PYTHON` / `QUANTLAB_SRC`）
- 构建：`desktop/build.ps1`（release 可执行文件，产物约 14 MB，首次全量编译约 3 分钟）
- 配置：`%APPDATA%\com.quantlab.desktop\desktop.json`（项目目录、Python、源码目录）
- 日志：项目目录 `.quantlab/tauri.log`、`.quantlab/tauri-launcher.log`

## 已完成的实现

- 窗口：**无边框**（`decorations: false`）+ 自绘标题栏，动态端口就绪后创建，标题「QuantLab · 本地研究」，1380×920 / 最小 900×640；保留系统边缘缩放。
- 标题栏：拖动移动、双击最大化/还原、最小化、最大化/还原、关闭；关闭仍走项目关窗行为（询问 / 托盘 / 退出）。
- 启动落点：首次空项目进 `#/local`（设置 → 本地项目）做初始化；已有项目直接进 `#/workflows`。
- 托盘：打开窗口、检查更新、退出；左键托盘显示窗口；窗口关闭按项目偏好（询问 / 后台 / 退出）。
- 文件：系统文件夹 / 多文件选择器；系统拖放路径桥接。
- 新项目：`--project` 启动新窗口；项目选择结果持久化。
- 后端：存活实例复用、空闲端口、健康检查、启动失败提示、退出前 `shutdown` 与任务检查。
- 图标：由仓库 `Assets/quantlab-bot-logo.png` 生成 Tauri 所需 PNG/ICO。

## 无边框窗口与自绘标题栏

- 前端组件 `web/src/components/DesktopTitleBar.tsx`：只在 `desktopKind() === 'tauri'` 时渲染；
  标题栏用 `data-tauri-drag-region` 拖动，按钮通过 `getCurrentWindow()` 调 `minimize` /
  `toggleMaximize` / `close`。
- 布局让位：`web/src/stylesDesktop.css` 给 `html[data-desktop-frame="tauri"]` 设置
  `--dtb-h: 38px`，body 增加顶部留白，`.sidebar` / `.topbar` / `.tape` 的 `top` 同步下移；
  标题栏已经显示品牌，桌面模式下隐藏侧栏里重复的 logo 行。
- 权限：`capabilities/main.json` 增加 `core:window:allow-start-dragging`、
  `allow-internal-toggle-maximize`（双击最大化）、`allow-minimize`、`allow-toggle-maximize`、
  `allow-close`、`allow-is-maximized`、`allow-unminimize` 等，保证远程来源页面能调用窗口 API。
- 窗口缩放仍由系统负责（WM_NCHITTEST 在四边返回 HTLEFT/HTTOP/HTBOTTOM/HTRIGHT），不需要自绘缩放热区。

## 中英文界面（i18n）

- 现有「中文原文 → 英文词典 + DOM 翻译」方案保留：中文是源语言，英文按需加载
  `i18n/en.json` 与 `i18n.en.ts` 的 `EN_PATTERNS`，查不到时原样显示中文。
- 设置 → 研究偏好新增「界面语言」卡片（简体中文 / English），切换后整页刷新并立即生效；
  账号菜单里的语言入口保留。默认侧栏宽度在英文下加宽到 252px，避免导航文字被截断。
- 本轮把未完成的词典补齐：`node web/scripts/i18n-extract.cjs` 从 `plain 6925 (missing 0),
  templates 664 (missing 0)`；新增 1204 条普通文案、164 条带变量模板，并补充后端返回的
  插件目录 / 模块库 / 环境诊断等中文文案 126 条 + 1 条环境错误模板。
- 真实浏览器（WebView2）逐页扫描英文界面：首页、数据、研究工作流、策略/因子/模型/组合/执行、
  回测、模拟盘、设置各分区等 36 个页面，残留中文 0（仅项目绝对路径含中文目录名，属数据不翻译）。
- 回归脚本：`web/scripts/i18n-extract.cjs` 现在同时合并 `landing.en.json`，后续新增文案会准确报缺。

## 本机验收记录（2026-10-21 / 10-22）

在 Windows 11 / WebView2 155 上，用 `desktop/src-tauri/target/debug/quantlab-desktop.exe` 实测：

| 场景 | 结果 |
|---|---|
| 首次空项目启动 | 后端随机端口启动，窗口落在 `#/settings/local`，页面正常渲染 |
| 已有项目启动 | 复用 `.quantlab/instance.json` 存活服务（版本握手一致才复用），窗口落在 `#/workflows`，行情/关注卡片正常 |
| 设置页关闭按钮 × | 回到 `#/home`，不再弹回设置页 |
| 无边框标题栏 | 自绘标题栏显示；拖动/双击最大化/最小化/关闭可用；侧栏和顶栏正确让位 |
| 窗口边缘缩放 | 四边命中 HTLEFT/HTTOP/HTBOTTOM/HTRIGHT，可拖动缩放 |
| 中英文切换 | 设置 → 研究偏好 → 界面语言：中文 ↔ English 整页刷新生效；标题栏按钮、页面标题一起切换 |
| 英文界面覆盖 | 36 个页面 DOM 扫描残留中文 0；i18n-extract missing 0 / templates 0 |
| Tauri IPC | `window.__TAURI__.core.invoke('native_desktop_info')` 返回 baseUrl/project/python，远程来源 ACL 正常 |
| 系统文件选择器 | `native_choose_files` 弹出原生对话框，取消返回 `[]` |
| 拖放桥接 | `quantlab://files-dropped` 事件把绝对路径写入「本机数据路径」，可预览/引用/复制 |
| 关窗行为 = 隐藏到托盘 | 窗口隐藏，进程与后端继续运行 |
| 关窗行为 = 询问 | 弹原生对话框「隐藏到托盘 / 退出」 |
| 关窗行为 = 退出 | 调用 `/api/local/shutdown`，后端退出，桌面进程退出，日志记录 `quit` |
| 多项目并行 | 两个不同项目各自一个后端与窗口，互不影响；同项目第二个实例复用后端 |
| release 构建 | `cargo build --release` 产物 14 MB；直接启动加载 `#/home`，关注卡片/7 天走势正常，IPC 正常 |

回归：`python -m pytest tests/test_local_projects.py -q` → 11 passed, 1 skipped。

## 尚未完成 / 后续

1. **Python 运行时打包（sidecar）**：当前使用本机 Python（`--python` / 环境变量 / 配置）。
   发布版应把后端打成 PyInstaller（先 onedir）sidecar，随安装包分发，不再依赖用户装 Python；
   同时修正冻结后的 `sys.executable -m …` 子进程调用与 multiprocessing 支持。
2. **安装包**：`cargo tauri build` 生成 NSIS/MSI；需要安装 Tauri CLI 并联网下载 NSIS，
   安装包内保留 sidecar 可执行文件与依赖目录的布局。
3. **更新源**：生成并长期保存签名密钥、开启 `bundle.createUpdaterArtifacts`、构建时注入
   `TAURI_SIGNING_PRIVATE_KEY`、配置 HTTPS endpoints 与 `latest.json`；托盘更新才真正生效。
   更新交互仍需补「展示说明/进度 → 检查运行任务 → 保存状态并停后端 → 安装」。
4. **旧方案下线**：确认 Tauri 验收通过后，移除 `packaging/desktop` 与
   `scripts/build_desktop.py` 的默认路径，更新 README 与发布脚本。
5. **自动化**：Windows CI 加桌面启动冒烟（启动 10s 内窗口未 hung、后端 `/api/local/info` 200、
   退出后子进程回收），并把 `i18n-extract` + 英文页面扫描纳入检查。

## 风险与兼容

- 旧 pywebview 桌面端仍可运行（`quant_agent.local.launcher`），但不再作为默认交付路径。
- 本机 Rust 工具链装在仓库上级 `.tmp/rustup`、`.tmp/cargo`（不污染系统 PATH）；
  换机器按常规 `rustup` + VS Build Tools 安装即可，脚本会自动识别系统 cargo。
- Tauri 窗口加载的 `http://127.0.0.1:*` 页面通过 capabilities 的 `remote.urls` 授权自定义命令；
  收紧/放宽权限都在 `desktop/src-tauri/capabilities/main.json`。
- **同一项目不要同时开两个桌面实例**：两个实例会复用同一个后端，但 WebView2 默认按项目分配
  用户数据目录，同目录并发会创建失败；不同项目并行没有问题。
- 受限/测试环境可用 `QUANTLAB_WEBVIEW_DATA_DIR` 覆盖 WebView2 数据目录、
  `QUANTLAB_CREDENTIALS` 覆盖后端密钥目录（后端既有能力）、
  `QUANTLAB_WEBVIEW_DEBUG_PORT` 打开 WebView2 远程调试端口（配合 CDP 排查页面）。
- 托盘图标、系统拖放的真实 OLE 拖拽建议在人工验收时再补一次；代码路径已通过事件桥接验证。

## 桌面/平台加固（2026-10）

1. **真实资金默认关闭**：`execution.real_spot` 默认值改为 `False`。本地管理员在「设置 → 本地项目 →
   真实交易能力」显式开启（二次确认 + 风险说明）；账号部署仍由服务器管理员
   `PUT /api/workspace/features` 控制。微实盘启动接口继续用 `features.require("execution.real_spot")`
   在服务端强制校验，关掉时直接拒绝。
2. **陈旧后端握手**：`/api/healthz` 与 `/api/local/info` 返回 `version` / `build` / `desktop_version` /
   `first_run`（`quant_agent.platform.buildinfo`）。Tauri 启动先读 `.quantlab/instance.json`：
   项目根一致且 `desktop_version == CARGO_PKG_VERSION` 才复用；版本不一致或没有握手的旧进程先调
   `/api/local/shutdown` 优雅退出、删实例文件再新起。旧进程仍有任务时（409）拒绝强杀并提示先处理，
   避免丢运行证据。日志在 `.quantlab/tauri.log`：`reuse backend …` / `stale backend …` /
   `removed stale instance …`。
3. **配置写入兜底**：`desktop.json` / `window.json` 写主位置失败时回退到项目 `.quantlab/` 同名文件，
   并写 `… fallback saved to …` 日志；下次启动从兜底副本恢复解释器/源码路径与窗口状态
   （项目路径仍以 `--project`、`QUANTLAB_PROJECT` 或主配置为准）。
4. **启动路由**：首次打开空项目仍落在 `#/local`（设置 → 本地项目）；`first_run` 改由后端在初始化前
   记录（`/api/local/info.first_run`），壳层不再猜 `.quantlab/project.json` 是否存在。前端把
   `#/local` 在解析路由的同一帧用 `replaceState` 归一到 `#/settings/local`，不再先渲染空页再跳转。
5. **错误 UX**：`web/src/http.ts` 的 `explainError()` 把 `INVALID_PARAMETER` / `PERMISSION_DENIED` /
   依赖缺失 / `NOT_FOUND` / `CONFLICT` / 限流 / 5xx / 网络错误映射成「人话标题 + 修复动作」；
   本地项目页用 `ErrorAlert` 展示，技术细节（code / HTTP / trace_id / 后端原文）折叠可展开。

验证：`cargo build --release`、`tsc -b`、`npm run build:windows`、`node web/scripts/i18n-extract.cjs`
（plain 0 / templates 0）、`pytest tests/test_local_projects.py tests/test_microlive.py`。
