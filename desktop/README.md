# QuantLab 桌面端（Tauri 2）

桌面外壳已从 pywebview 迁移到 **Tauri 2**，继续复用：

```
Tauri 2（窗口 / 托盘 / 更新 / 文件对话框 / 拖放 / Python 后端进程管理）
├─ 现有 React 前端：web/dist（页面由本机 Python 后端提供）
└─ 现有 Python 后端：quant_agent（Agent、数据、因子、回测、组合）
```

## 它与旧版的关系

- 旧版 `packaging/desktop/`（pywebview + 嵌入式 CPython + C# 启动器）**保留但已弃用**，
  只作为回退方案；新开发都走 `desktop/src-tauri`。
- 页面来源不变：Tauri 窗口加载 `http://127.0.0.1:<port>/#/home`，与后端同源，
  所以前端所有相对 `/api/*` 请求不需要改地址，也不需要 CORS。
- 前端新增了 `web/src/native.ts` 适配层：Tauri 命令 / 旧 pywebview / 纯浏览器三种环境统一。

## 运行

```powershell
# 1) 先构建前端（改了 React 代码时需要）
cd web; npm run build

# 2) 开发运行（debug 版，带控制台日志）
powershell -ExecutionPolicy Bypass -File desktop/run.ps1 -Project "D:\Research\ProjectA"

# 不传 -Project 时，首次会弹系统文件夹选择器；选择结果会记录到
# %APPDATA%\com.quantlab.desktop\desktop.json
```

`run.ps1` 会自动设置：

- `QUANTLAB_PYTHON`：Python 解释器（默认 `python`，也可用 `-Python` 指定）
- `QUANTLAB_SRC`：仓库的 `src` 目录（让后端以源码方式启动）

也可直接运行编译好的程序：

```powershell
desktop\src-tauri\target\debug\quantlab-desktop.exe --project "D:\Research\ProjectA" --python python --src "C:\path\to\QuantLab\src"
```

## 构建

```powershell
powershell -ExecutionPolicy Bypass -File desktop/build.ps1            # release 可执行文件
# 产物：desktop/src-tauri/target/release/quantlab-desktop.exe
```

打包安装包需要 Tauri CLI + 网络下载 NSIS（`tauri build`），见下方「更新与打包」。

## 桌面壳职责

| 能力 | 实现 |
|---|---|
| 窗口 | 无边框（`decorations: false`）+ 自绘标题栏，加载本机后端页面；标题 `QuantLab · 本地研究`；系统边缘缩放 |
| 标题栏 | 拖动、双击最大化、最小化/最大化/关闭；关闭走项目关窗偏好 |
| 后端进程 | 复用 `.quantlab/instance.json` 中存活的服务；否则用空闲端口启动 `python -m quant_agent.local.launcher --serve`，轮询 `/api/local/info` 就绪 |
| 托盘 | 打开窗口 / 检查更新 / 退出；左键点击托盘显示窗口 |
| 关闭窗口 | 按 `/api/local/info` 的 `preferences.close_action`：询问 / 隐藏到托盘 / 退出；有未完成任务时退出会被后端拒绝 |
| 文件操作 | 系统文件夹、文件选择器（`native_choose_folder` / `native_choose_files`） |
| 拖放 | 系统文件拖入 → Rust 事件 `quantlab://files-dropped` → 前端识别为本机路径（可预览/引用/复制） |
| 新项目窗口 | `native_open_project` 启动一个新的桌面实例；不同项目各自一个后端 |
| 更新 | 托盘「检查更新」打开 GitHub Releases，用户下载安装 preview 版本 |

配置文件：`%APPDATA%\com.quantlab.desktop\desktop.json`
（项目目录、Python 解释器、源码目录；不含密钥）。

后端启动日志：项目目录 `.quantlab/tauri-launcher.log`；桌面壳日志：`.quantlab/tauri.log`。

## 更新与打包

发布版本以 GitHub Releases 为准。推送 `v0.1.0-preview.*` 标签后，Windows CI 构建 sidecar 与 NSIS 安装包，
启动冻结后端验收 Agent Runtime、内置网页与文档依赖，并上传安装包、便携 sidecar ZIP 和各自的 SHA-256。
发布前检查草稿中的资产，随后发布预览版。托盘「检查更新」打开发布页。

## 环境要求

- Windows 10/11 x64，WebView2 Runtime
- Rust stable（MSVC toolchain）+ Visual Studio Build Tools（C++ 链接器）
- Python 3.10+，并安装 `.[research]` 依赖（与旧桌面端一致）
- 前端构建：Node 18+ / npm

本机开发环境里 Rust 装在仓库上级目录 `.tmp\rustup` 与 `.tmp\cargo`（不动系统 PATH），
`build.ps1` / `run.ps1` 会自动指向它；换机器时按常规 `rustup` 安装即可。

## 排查用环境变量

| 变量 | 作用 |
|---|---|
| `QUANTLAB_PYTHON` | Python 解释器（等价于 `--python`） |
| `QUANTLAB_SRC` | 后端源码目录（等价于 `--src`） |
| `QUANTLAB_WEBVIEW_DATA_DIR` | 覆盖 WebView2 用户数据目录；默认按项目分目录，避免多实例冲突 |
| `QUANTLAB_WEBVIEW_DEBUG_PORT` | 给 WebView2 加 `--remote-debugging-port`，可用 CDP/DevTools 连窗口排查 |

托盘启动日志中的 `visible=Ok` 表示图标已向 Shell 注册；Windows 11 的折叠区可能让 `rect=None`，
工具提示更新也可能短暂返回权限错误，这两种情况不再阻止“隐藏到托盘”。若 `visible` 本身失败，
窗口会保留并提示改为最小化。

## 已知限制

- 同一项目**不要同时开两个桌面实例**：后端会复用，但 WebView2 同目录并发创建会失败；
  不同项目并行没有问题。
- 安装版优先使用随包分发的 `resources/backend/quantlab-backend/quantlab-backend.exe`。设置 `QUANTLAB_PYTHON` 时使用指定解释器。
- `.github/workflows/desktop-release.yml` 在 Windows 构建前端与 sidecar，启动冻结后端验收 API 和内置网页，再生成 NSIS 安装包与 SHA-256。手动运行可下载安装包；推送 `v0.1.0-preview.*` 标签会创建 GitHub 草稿发布，供检查后发布。
- sidecar 内置后端和 Word/PPT 导出依赖。研究卡片的独立子进程仍需在「研究环境」选择 Python 3.11+；不能把后端可执行文件选作解释器。可用 `QUANTLAB_RESEARCH_PYTHON` 设置初始解释器。
- 真实 Office 页面预览需安装 LibreOffice；PDF 页面渲染由随包 PyMuPDF 提供。缺少 LibreOffice 时文件保留，并明确提示尚未检查排版。
- 目前使用 GitHub Release 手动下载安装。
