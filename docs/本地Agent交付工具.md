# 本地 Agent 交付工具（文件读写 + Markdown→Word/PPT）

面向桌面「本地项目」模式：让 Agent 能把作业/报告真正落到项目文件夹，而不是只在对话里返回文本。
实现位于 `src/quant_agent/workflows/agent.py` 的 `local_*` 工具段，作为平台工具（scene=workflow）注册。

## 工具与签名

| 工具 | 签名 | 风险/审批 |
|---|---|---|
| `local_write_text` | `(path, text, overwrite=False)` | risk=confirm，审批类别 save |
| `local_make_dir` | `(path)` | risk=confirm，审批类别 save |
| `local_read_text` | `(path, offset=0, limit=20000)` | risk=confirm，审批类别 save |
| `local_export_docx` | `(markdown, path, title="")` | risk=confirm，审批类别 save |
| `local_export_pptx` | `(slides, path, title="")` | risk=confirm，审批类别 save |

- `slides = [{title, bullets, notes}]`，最多 80 页；生成 16:9（13.333in × 7.5in）。
- `local_export_docx` 支持简单 Markdown：标题（`#`）、正文、无序/有序列表、管道表格、围栏代码块、引用；
  生成后用 `python-docx` 可正常打开。
- 读取/写入均按字符/字节给出 `truncated`、`next_offset`、`bytes` 等分页字段。

## 相对路径与文本预览（现场问题修复）

- **相对路径按本地项目根解析**：`LocalProject.resolve_path` 先把相对路径拼到 `self.root` 再做
  `is_relative_to` 授权校验，绝对路径保持原样。Agent 传 `验收输入/关键证据摘要.md` 不会再落到后端 cwd。
  `authorize` / `browse` / `import_file` / `grant` / `revoke` 共用同一解析。
- **`workflow_local_files(action="preview")` 支持文本**：`.md` / `.txt` / `.json` / `.py` / `.log` 等直接按文本读取
  （与 `local_read_text` 同一实现，默认 20k 字符、支持 `offset`），不再走「只支持 CSV/TSV/XLSX/Parquet/JSONL」的表格预览；
  CSV/TSV/XLSX/XLSM/Parquet/JSONL 仍按表格预览；docx/pdf/png 等二进制/办公文件给出可行动报错
  （提示用系统应用打开、文本可用 `local_read_text`）。GUI 的 `GET /api/local/files/preview` 也使用同一分支，
  文本直接返回 `{kind:"text", text, has_more, next_offset}`。
- **错误提示可行动**：未授权时返回解析后的绝对路径，并提示「改用绝对路径，或先在『本地项目 → 文件与目录权限』授权」，
  读取场景附带 `local_read_text`、写入场景附带 `local_write_text` 的用法。
- **一次读够**：`local_read_text` 默认 20k 字符，返回 `has_more`、`next_offset` 与 `more_hint`
  （`还有更多内容：继续调用 local_read_text(offset=...)` / `已读到文件结尾`），避免 Agent 因分页失败浪费工具调用次数。

## 边界

- **只在本地项目可用**：`services.local` 为空（账号模式）时调用会明确报「仅本地项目可用」。
- **只写已授权可写目录**：写操作走 `LocalProject.authorize(write=True)`；默认授权是项目目录，
  用户可在「本地项目 → 文件与目录权限」里追加可写输出目录。相对路径按项目根解析。
- **读写都拒绝平台目录**：`.quantlab` 数据目录、凭证密钥目录（`QUANTLAB_CREDENTIALS` 或系统默认位置）、
  模块库目录一律拒绝，避免 Agent 读密钥或改平台状态。
- **不提供 shell / Python 执行**：工具只做文件与文档生成，不是通用代码执行入口。
- **审批**：tool 的 `risk=confirm`、`category=save`。团队/角色 Agent 本来就不会拿到 confirm 类工具；
  研究室（Lab）在 `manual` / `accept_edits` 权限下先弹审批卡片，`auto` 模式按既有权限规则执行。
- 单文件文本上限 2MB；`overwrite=False` 时已存在即报错，覆盖必须显式 `overwrite=True`。
- `local_read_text` 对 docx/pdf/xlsx/png 等二进制与办公文件明确报错，并做 NUL 字节嗅探，不假装读出文本。

## 可选依赖

`pyproject.toml` 新增 `[project.optional-dependencies].deliverables = ["python-docx>=1.1,<2", "python-pptx>=1.0,<2"]`。
缺少库时工具返回可执行的安装提示（含当前解释器路径与 `pip install -e ".[deliverables]"`），不静默失败；
不会自动安装依赖，也不影响其它工作流功能。

## 测试

- `tests/test_local_agent_deliverables.py`：授权写/读、2MB 限制、覆盖保护、越界与私有目录拒绝、账号模式拒绝、
  审批类别与平台桥接、依赖缺失提示；docx/pptx 用例在环境无库时 `pytest.skip`。
- docx/pptx 生成逻辑另用带 `python-docx 1.2.0` / `python-pptx 1.0.2` 的 Python 做了实测：
  重新打开文档校验标题、段落、表格单元格、幻灯片数量、16:9 比例与备注，中文正常。

## 图表与图片（新增）

- local_chart(kind, title, labels, values, path, width=1200, height=675)：Pillow 生成 PNG（bar/line/pie），返回 path 与 url（/api/local/files/image?path=...）。不依赖 matplotlib。
- 对话：Agent 把 ![标题](url) 写入回复，前端 Markdown 渲染为图片。
- Word：![标题](本地 png 路径) 或 ![标题](/api/local/files/image?path=...)，local_export_docx 会插入 6.2in 宽图片并加图注。
- PPT：slides 每页支持 layout（bullets/section/image/closing）与 image（本地 png 路径）；image 页自动按比例居中。
- GET /api/local/files/image：本地模式只读授权目录图片，PNG/JPEG/GIF/WebP/SVG，单张 ≤20MB。

## PPT 版式

local_export_pptx 不再使用默认模板，而是生成：深色封面、章节页、浅色内容页（顶部强调条、编号、标题、要点）、图表页、页脚页码；字体优先 Microsoft YaHei，强调色与 QuantLab 品牌一致。缺少 image 时按普通要点页处理。

