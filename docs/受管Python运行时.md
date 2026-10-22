# 受管 Python 运行时（探测与诊断）

本地 / sidecar 模式需要一个明确答案：**当前后端进程到底在用哪个 Python、关键依赖装没装**。
本功能只做这件事——只读探测 + 诊断快照，不是完整的运行时打包或安装器。

## 定位与边界

做：

- 报告当前后端进程的解释器：可执行路径、Python 版本、实现（CPython 等）、平台、是否虚拟环境；
- 报告受管依赖能否在当前进程导入，以及导入后的版本；
- 生成带格式版本号的诊断快照（manifest），可被桌面端、诊断脚本或支持包直接落盘；
- 本地模式下通过 `GET /api/local/runtime` 暴露，并把摘要挂到已有的 `GET /api/local/info` 上。

不做（重要）：

- 不创建 / 切换虚拟环境，不安装、升级或降级依赖，不下载解释器；
- 不保证桌面壳与后端使用不同进程（sidecar 若独立启动，以该进程自己的探测结果为准）；
- 不做版本钉住、离线 wheelhouse 或依赖完整性校验——这些仍由部署 / 打包流程负责；
- 不读取环境变量、密钥或用户目录内容，响应里只有解释器路径、版本与依赖状态。

## 接口

### `GET /api/local/runtime`（仅本地模式）

只读诊断；失败或非本地部署一律 `404`（见下文）。查询参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `refresh` | `false` | `1` / `true` 时跳过依赖探测缓存，重新导入并读取版本 |

响应（节选）：

```json
{
  "mode": "local",
  "manifest": {
    "format": "quantlab.local-runtime/1",
    "generated_at": 1760000000.0,
    "mode": "local",
    "managed": true,
    "required": ["python-docx", "python-pptx", "Pillow", "numpy", "pandas"],
    "limitations": ["只读探测：不创建虚拟环境、不安装/升级依赖、不下载解释器，也不修改当前进程。", "..."]
  },
  "interpreter": {
    "executable": "C:\\Python314\\python.exe",
    "resolved_executable": "C:\\Python314\\python.exe",
    "version": "3.14.7",
    "version_info": [3, 14, 7],
    "version_text": "3.14.7 (main, ...)",
    "implementation": "CPython",
    "compiler": "MSC v.1944 64 bit",
    "platform": "win32",
    "platform_detail": "Windows-11-10.0.26100-SP0",
    "architecture": "AMD64",
    "is_64bit": true,
    "prefix": "C:\\Python314",
    "base_prefix": "C:\\Python314",
    "venv": false,
    "frozen": false
  },
  "dependencies": {
    "python-docx": {"module": "docx", "available": true, "version": "1.1.2", "error": ""},
    "python-pptx": {"module": "pptx", "available": true, "version": "1.0.2", "error": ""},
    "Pillow": {"module": "PIL", "available": true, "version": "11.0.0", "error": ""},
    "numpy": {"module": "numpy", "available": true, "version": "2.1.0", "error": ""},
    "pandas": {"module": "pandas", "available": false, "version": "", "error": "ModuleNotFoundError: No module named 'pandas'"}
  },
  "summary": {
    "ok": false,
    "executable": "C:\\Python314\\python.exe",
    "version": "3.14.7",
    "implementation": "CPython",
    "platform": "win32",
    "venv": false,
    "available": ["python-docx", "python-pptx", "Pillow", "numpy"],
    "missing": ["pandas"],
    "dependencies": {"python-docx": {"available": true, "version": "1.1.2"}, "...": {}}
  }
}
```

说明：

- `dependencies` 的键是**分发名**（pip 名），`module` 是实际导入名；`python-docx` 的模块名是 `docx`、`Pillow` 是 `PIL`，不要按分发名 `import`。
- 依赖"可用"的判定是**真实 import** 成功；失败时 `error` 给出异常类型与信息（截断 200 字符）。能导入 ≠ 版本兼容或原生库完整，详见下文限制。
- `summary.ok` 表示 5 个受管依赖是否全部可用；`missing` 列出不可用项。
- 响应只含解释器与依赖信息，不含环境变量、密钥或用户数据。

### `GET /api/local/info` 中的 `runtime` 摘要

`info` 原有字段全部保留，新增 `runtime`：

```json
{
  "mode": "local",
  "root": "...",
  "runtime": {
    "ok": false,
    "executable": "C:\\Python314\\python.exe",
    "version": "3.14.7",
    "implementation": "CPython",
    "platform": "win32",
    "venv": false,
    "available": ["python-docx", "python-pptx", "Pillow", "numpy"],
    "missing": ["pandas"],
    "dependencies": {"python-docx": {"available": true, "version": "1.1.2"}}
  }
}
```

这是摘要（不含 manifest 与完整解释器字段）。依赖探测结果有缓存，`info` 高频调用不会反复导入 numpy / pandas；安装依赖后刷新页面，或调用 `/api/local/runtime?refresh=1` 重新探测。

### 非本地部署

账号 / 云端部署没有"本机受管解释器"的概念：

- `GET /api/local/runtime` 在非本地模式一律 `404`，且由最外层中间件在鉴权前处理（未登录也是 404，不会先返回 401 暴露接口存在）。
- 其它 `/api/local/*` 接口在账号模式的行为保持不变（例如 `/api/local/info` 仍是原有错误码）。

## 探测口径

| 受管依赖（分发名） | 导入名 | 版本来源 |
| --- | --- | --- |
| python-docx | `docx` | `docx.__version__` |
| python-pptx | `pptx` | `pptx.__version__` |
| Pillow | `PIL` | `PIL.__version__` |
| numpy | `numpy` | `numpy.__version__` |
| pandas | `pandas` | `pandas.__version__` |

- 解释器信息来自 `sys` / `platform`：`sys.executable`、`platform.python_version()`、`platform.python_implementation()`、`sys.platform`、`sys.prefix` / `sys.base_prefix`（`venv` 据此判断）、`getattr(sys, "frozen", False)`。
- 版本只做字符串化（tuple 会拼成 `x.y.z`），不做语义化比较。
- 探测是**进程内**行为：如果桌面壳另外起了一个 sidecar 进程，这里报告的是"响应请求的这个后端进程"，不是壳进程。

## 格式版本

`manifest.format = "quantlab.local-runtime/1"`：

- `format` 变化表示字段语义有破坏性调整；
- `generated_at` 是本次探测时间（Unix 秒）；
- `required` 是受管依赖清单（与 `MANAGED_DEPENDENCIES` 一致）；
- `limitations` 是机器可读的人类提示，桌面端可原样展示。

## 已知限制与排查

1. **不是完整打包**：本接口不解决"解释器从哪来、依赖怎么装"。离线部署请用 wheelhouse / 预装环境，打包与版本钉住由发布流程负责。
2. **import 判定有副作用**：某些模块导入时会执行初始化代码；这里只做一次（并缓存），但极少数模块可能较慢或产生警告。
3. **首次调用偏慢**：numpy / pandas 首次导入可能需要数百毫秒；之后走缓存。`/api/local/runtime?refresh=1` 会主动重新导入。
4. **缓存会过期**：运行中安装依赖后，旧进程的缓存不会自动更新；刷新页面或带 `refresh=1` 重新探测，必要时重启后端。
5. **可用性 ≠ 兼容性**：报告的是"当前进程能 import"，不是"某个版本的 API 一定兼容"；业务代码仍需按自身依赖声明约束版本。
6. **多进程差异**：桌面壳 / 独立 sidecar / 后端可能使用不同解释器，排障时以实际报错进程的探测结果为准。

## 测试

```bash
python -m pytest tests/test_local_runtime.py -q
```

覆盖：本地模式真实探测字段与依赖可用性、`info` 原字段保留 + runtime 摘要、非本地 404（含未登录）、只读（POST 405）、缓存与刷新、响应不泄露环境密钥、JSON 可序列化。

## sidecar 打包入口（新增）

- scripts/build_sidecar.py --check：检查 PyInstaller / web/dist，打印将执行的完整命令，不下载、不安装。
- scripts/build_sidecar.py --clean --output desktop/src-tauri/resources/backend：PyInstaller onedir 构建 quantlab-backend，入口为 scripts/sidecar_entry.py，并收集 web/dist。
- 桌面壳启动顺序：显式 --python / QUANTLAB_PYTHON → 安装包
esources/backend/quantlab-backend(.exe) → 系统 Python。Set QUANTLAB_SIDECAR 可显式覆盖路径。
- 已在 Python 3.14 + PyInstaller 6.22 实际构建 quantlab-backend.exe（onedir，约 25MB exe，_internal 依赖目录），启动 --serve --project ... --port ... 冒烟通过 /api/healthz；产物在 desktop/src-tauri/resources/backend/，已加入 .gitignore，不入库。

