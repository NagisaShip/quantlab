# Word 与 PPT 图表交付修复

本轮修复文件内没有图表、图片被静默忽略、PPT 页数不符、图注分页及 Office 临时目录兼容问题。版本号和 Release 未改动。

## 行为变化

- Word 支持 `chart` JSON 围栏，直接生成嵌入图像，保留单位与来源图注。普通图片支持空格路径、尖括号包裹路径及正文中的图片；找不到或越过授权目录的图片会使导出失败，不再用占位文字冒充交付。
- PPT 支持页面 `chart` 字段，生成原生可编辑图表及内嵌 XLSX 数据，支持柱图、折线图和饼图、多系列及负数。修复实际渲染中发现的 LibreOffice 负柱方向错误，并将分类标签放在图表下方。
- 更新 Agent 提示和工具 schema：有明确比较、趋势或构成数据时，把适合的图表放进文件，不能只在聊天中贴图。缺少数据时说明缺失，不编造图表或来源。
- 无标题时默认不增加封面；可以显式设置 `include_cover`。`expected_slides` 校验封面和拆页后的真实页数，不符时拒绝保存；`slides` 返回实际总页数，`content_slides` 返回正文页数。
- Word 去除标题默认装饰线，图像与图注保持相邻，去掉嵌入位图中重复的标题、来源和空白边带，避免图注成为独立尾页。
- Office 使用可写的绝对临时目录、独立配置及 ASCII 中间文件名；Windows 优先控制台入口，保留转换错误摘要。输入文件仍须经过原有目录授权，转换失败保留原文件。

## 图表数据契约

两种格式共用数据，不推断未提供的数值或单位：

```json
{
  "kind": "bar",
  "title": "实验收益比较",
  "labels": ["策略甲", "策略乙"],
  "series": [{"name": "样本外", "values": [-1.0, 2.0]}],
  "unit": "%",
  "source": "实际实验 ID"
}
```

Word 将该 JSON 放入独立的 `chart` 代码围栏；PPT 放入 `{"title":"实验收益比较","chart":上述对象}`。文件式导出也使用同一契约。类别与数据须等长，不能含布尔值、缺失值或 NaN/Inf；饼图不接受负数、全零或多系列。PPT 的图表与图片二选一，不能放在章节或结束页布局中而被隐藏。

## 验证

- 文档、产物快照、工作流、研究约束及 Lab Runtime 相关回归：105 passed、1 skipped；最终文档专项再次运行 41 passed。两组存在重叠，不累加为独立样本。没有重跑整个仓库或真实模型质量测评。
- 打开生成包验证 Word 图片、PPT 图表 XML 与可编辑工作簿存在，核对数值和系列；验证错误输入不会被替换成零或偷偷丢弃，页数校验失败不会覆盖旧文件。
- 真实 LibreOffice/PyMuPDF 渲染并逐页查看最终 2 页 Word、4 页 PPT，三类图表均在文件中，中文、单位、来源与负数方向可读。Word 另经技能 `render_docx.py` 渲染并查看所有页。
- 无效 TEMP/TMP、中文空格文件名、图片缺失、无结束图表围栏和空图表布局均有回归覆盖。Ruff 检查及格式检查通过。

复现固定数据验收：

```powershell
python -X utf8 scripts/runtime_eval/document_smoke.py --output .tmp/document-chart-smoke
python -X utf8 -m pytest -q tests/test_document_charts.py tests/test_local_agent_deliverables.py tests/test_runtime_artifacts.py
```

验收数据明确标注为固定测试数据，不代表真实业绩。Word 图表当前是嵌入图片，PPT 是原生可编辑图表。本轮没有将自然语言中的任意数值或所有 Markdown 表格自动改成图；Agent 需要按已公开契约提交图表数据。通用结构化输出的有界修复和自动事实核验仍需后续独立验证。
