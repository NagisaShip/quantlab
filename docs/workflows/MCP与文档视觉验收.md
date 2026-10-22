# MCP、文档预览与发布验收

设置 → MCP 服务支持新增、编辑、启停、删除，以及连接测试与工具 schema 查看。保存配置不会启动服务。连接测试仍要求本地模式、`QUANTLAB_ALLOW_MCP=1` 与服务启用；账号部署由工作区管理员管理配置，不能调用本机进程。

Word 导出采用 A4、中文字体、标题层级、行距、页码、重复表头和基本强调样式；PPT 的长要点自动拆页。超出输入上限会明确报错，不再静默截断要点。

Agent 的 `local_preview_document(path, start=1, limit=4)` 渲染真实 DOCX/PPTX/PDF 页面，返回 PNG、页数、后续页码和内容 hash。截图会作为用户图片消息进入研究室、通用 Agent 和问 AI 的下一轮模型上下文；需要支持图片的模型。返回 `visual_review=pending` 表示已渲染，不能据此声称视觉验收完成。Agent 应查看所有页面、修正问题、重新导出并再次预览。

用户也可在本地项目选择文档后点击「查看文档页面」或下载文件。Office 转 PDF 依赖 LibreOffice，PDF 转 PNG 依赖 PyMuPDF。预览缓存按文件内容隔离，源文档更新后不会沿用旧截图。源文件和输出缓存都经过目录授权与平台私有目录检查。

本地像素回归示例（先启动隔离后端）：

```powershell
# 首次生成候选基准，检查截图后再使用
node scripts/visual_check.mjs --pages mcp --baseline .tmp/visual-baseline --update-baseline
# 后续比较，不会自动覆盖基准
node scripts/visual_check.mjs --pages mcp --baseline .tmp/visual-baseline
node --test tests/visual_diff.test.mjs
```

比较会生成实际截图、差异 PNG 和 `report.json`。默认允许变化像素比例为 0.001（`--max-diff-ratio` 可调整）；缺少基准或尺寸不同都失败。比较时应使用相同操作系统、浏览器、字体、视口与固定数据。工作台未加载成功也会失败。

CI 手动运行时，先勾选 `visual_update_baseline` 生成候选基准，检查 `visual-baseline` 截图；后续运行填写该次 `visual_baseline_run_id`，从已检查的运行下载基准进行 MCP 页面像素比较。其余页面继续检查运行错误和横向溢出。不会自动把变更后的截图批准为新基准。

`documents` CI 作业安装 LibreOffice、中文字体与交付物依赖，执行真实 Word/PPT 渲染及模型图片消息回传测试。本机缺少 LibreOffice 时该真实转换用例跳过，PDF 渲染、授权、缓存失效、缺依赖反馈与导出测试仍可独立运行。

桌面发布步骤：构建网页 → PyInstaller sidecar → 从独立临时项目启动二进制，验收本地 API 和内置网页 → Tauri NSIS → SHA-256 → 上传安装产物；`v0.1.0-preview.*` 标签额外创建草稿发布。当前通过 GitHub Release 手动下载安装更新。
