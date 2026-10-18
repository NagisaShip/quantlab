"""结构化内容协议（QuantLab Blocks v1）：告诉 Agent 怎样输出有明确类型的数据块，由前端渲染成交互组件。

前端实现在 web/src/components/blocks/（protocol.ts 是同一份类型表）。写法：

    ```ql:<类型>
    {严格 JSON 对象}
    ```

设计要点：
- 数据块里的数字必须来自工具结果（回测、评估、搜索），不能编造；
- 能引用平台记录的就引用 id（backtest_id），前端自己去取完整数据，省 token，也保证与原始记录一致；
- 每个块可带 evidence（measured 实测 / inferred 推断 / external 外部来源）与 source，结论可追溯。
"""

from __future__ import annotations

BLOCK_TYPES = (
    "chart",
    "data_table",
    "metrics",
    "backtest_report",
    "kline_review",
    "factor_report",
    "parameter_heatmap",
    "stress_report",
    "compare",
    "citation",
    "claims",
    "file",
    "status",
    "steps",
    "callout",
    "choices",
    "form",
    "confirm",
    "run",
)

BLOCKS_GUIDE = (
    "【结构化内容块】需要展示图表、表格、指标、来源、文件或状态时，不要只用文字描述，"
    "输出代码块，语言写 ql:<类型>，内容是一个严格 JSON 对象（双引号、无注释），前端会渲染成可交互组件：\n"
    '- ql:backtest_report {"backtest_id": "…", "targets": {"sharpe": 1}}：回测报告（指标卡、净值 vs 基准、回撤、'
    "交易标记、K 线复盘）。跑完回测要汇报结果时优先用它，只给 id，前端会读取完整记录\n"
    '- ql:kline_review {"backtest_id": "…"}：K 线上的入场 / 出场点位与逐笔交易复盘\n'
    '- ql:compare {"versions": [{"label": "v1", "backtest_id": "…", "changes": ["改了什么规则"], '
    '"oos": {"sharpe": 0.8}}], "explanation": "你对差异原因的推测"}：多版本实验对比（净值曲线同框叠加 + '
    "指标 Diff 表），explanation 会被标成「推断」。单变量消融（例如 ATR 3.0 → 6.0）优先用它；"
    "要叠加净值曲线时每个 version 都带 backtest_id\n"
    '- ql:metrics {"items": [{"key": "sharpe", "value": 1.2, "window": "2024-01~2024-06", '
    '"source": "bt:<id>", "evidence": "measured"}]}：指标卡；常用 key：total_return、sharpe、max_drawdown、win_rate、'
    "trades、total_fees、ic、icir（前端自带定义）；其他指标写 label、format（pct / pct_signed / ratio / int / money）、definition\n"
    '- ql:parameter_heatmap {"title": "…", "x_param": "fast", "y_param": "slow", "x": [16, 20, 24], '
    '"y": [72, 80, 88], "z": [[0.4, 0.6, 0.5], [0.5, 0.9, 0.7], [0.3, 0.5, 0.4]], "metric": "sharpe", '
    '"best": {"x": 20, "y": 80}, "robustness": {"verdict": "plateau|spike|fragile", "note": "…"}}：'
    "二维参数敏感度热力图（X 参数 × Y 参数，颜色为指标值）；数据必须来自真实的网格回测\n"
    '- ql:stress_report {"v": 3, "verdict": "pass|warn|fail", "score": 1.0, "checks": [{"key": "walk_forward", '
    '"label": "滚动分段", "ok": true, "value": "3/3 段为正", "note": "…"}], "walk_forward": {...}, '
    '"sample_split": {...}, "stability": {...}, "cost": {...}}：过拟合压力测试报告（滚动分段、样本外、参数敏感性、'
    "成本压力）；数字必须来自 stress_test 工具结果\n"
    '- ql:chart {"kind": "line|area|bar|scatter|pie|heatmap", "title": "…", "x": [...], '
    '"series": [{"name": "…", "data": [...]}], "y_format": "pct"}；饼图用 labels + values；热力图用 x、y、z（二维数组）；'
    "散点的 data 写 [[x, y], …]\n"
    '- ql:data_table {"title": "…", "columns": [{"key": "a", "label": "列名", "format": "pct"}], "rows": [{"a": 0.1}]}'
    "：超过 5 行或需要排序筛选的表格用它（支持排序、筛选、导出 CSV）\n"
    '- ql:factor_report {"name": "…", "dates": [...], "ic": [...], "groups": [{"name": "Q1", "ret": 0.01}], '
    '"decay": [{"lag": 1, "ic": 0.03}], "turnover": [...], "corr": {"labels": [...], "matrix": [[...]]}}；'
    "多个因子用 factors: [{name, ic}]。数据取自 evaluate_factor 等工具结果\n"
    '- ql:citation {"sources": [{"id": 1, "title": "…", "url": "…", "snippet": "…", "kind": "web|run|data|file"}]}：'
    "用了外部资料或平台记录时，正文用 [1]、[2] 标注，末尾附这个块；kind=run 的 url 可以写站内链接 #/backtest/<id>\n"
    '- ql:claims {"claims": [{"text": "…", "type": "measured|inferred|external", "ref": "bt:<id>", "cite": [1]}]}：'
    "研究结论清单，必须如实区分实测、推断与外部来源\n"
    '- ql:status {"state": "ok|running|warn|error", "title": "…", "detail": "…", "warnings": [...], '
    '"actions": [{"label": "打开报告", "href": "#/backtest/<id>"}, {"label": "再跑一次压力测试", "prompt": "/stress 2"}]}\n'
    '- ql:steps {"items": [{"title": "…", "status": "done|doing|pending|failed", "note": "…"}]}：步骤时间线\n'
    '- ql:callout {"tone": "info|warn|danger", "title": "…", "text": "…"}：重要提示\n'
    '- ql:choices {"question": "…", "options": ["…", {"label": "…", "prompt": "点了以后发给你的话"}]}；'
    'ql:confirm {"title": "…", "detail": "…", "prompt": "确认后发给你的话"}；ql:form {"title": "…", "fields": '
    '[{"key": "k", "label": "…", "type": "text|number|select|checkbox", "options": [...]}]}：需要研究者选择或填写时\n'
    '- ql:file {"name": "x.csv", "url": "/api/…", "kind": "csv|json|log|pdf|md|image"} 或给 content：文件预览与下载\n'
    '- ql:run {"status": "ok|error", "exit_code": 0, "stdout": "…", "stderr": "…", "duration_ms": 120, "code": "…"}：'
    "只用于真实执行过的代码结果；只是展示代码就用普通代码块（```python title=strategy.py {3,7}``` 可写文件名与要标出的行号；"
    "改动用 ```diff```）\n"
    "【公式、图示与排版——积极使用】\n"
    "- 数学公式：凡是出现指标定义、因子表达式、仓位 / 风险公式、统计检验，一律用 LaTeX 写出来，"
    "行内 $\\text{Sharpe}=\\frac{\\mu}{\\sigma}\\sqrt{N}$，独立成行用 $$…$$，多行推导用 \\begin{aligned}…\\end{aligned}；"
    "不要用纯文字或代码块描述公式。金额写成 100 USDT 而不是 $100，避免被当成公式\n"
    "- Mermaid：讲流程、决策逻辑、状态机、模块关系、时间线时画 ```mermaid```（flowchart / sequenceDiagram / "
    "stateDiagram-v2 / gantt / pie / mindmap / timeline）；节点文字含括号、冒号等符号时用双引号包起来，"
    '例如 A["入场: RSI<30"]，否则会解析失败\n'
    "- 内联 SVG：需要示意图（仓位曲线示意、止损 / 止盈位置、区间划分、信号时序）而又不是数据图表时，"
    "可以直接输出 ```svg``` 代码块（一个完整的 <svg viewBox=…>…</svg>，用 currentColor 适配深浅色）；"
    "真实数据一律用 ql:chart，不要画进 SVG\n"
    "- Markdown：用 #/##/### 分清层级；支持表格（:--: 对齐）、任务列表、脚注 [^1]、> [!NOTE] / [!WARNING] 提示框、"
    "==高亮==、~~删除线~~；也可以写原生 HTML：<details><summary>标题</summary>可折叠的长内容（里面照样写 Markdown）</details>、"
    "<table>（合并单元格等 Markdown 表格做不到的）、<kbd> <sub> <sup> <mark>。脚本、样式表、iframe、表单与事件属性会被过滤\n"
    "- 图片用 ![说明](https://…)。\n"
    "- 如果研究者消息里出现「【渲染失败】」，说明你上一条回答里的某个块没能显示：按错误信息修正那个块"
    "（Mermaid 语法、LaTeX、JSON），只重新输出修好的部分，不要重复整段回答。\n"
    "【块的铁律】块里的每个数字都必须来自本轮或之前的工具结果，不能估算或编造；没有数据就不要画图，直接说明缺什么。"
    "已有 backtest_id 时引用 id，不要把净值序列抄进 JSON。一次回答里块不要超过 4 个，文字负责解释，块负责呈现数据。"
    "【避免重复】回测结果只输出一个 ql:backtest_report；不要再在前面加 ql:metrics / ql:summary 重复同一组指标。"
    "ql:backtest_report 已内含 K 线复盘与指标卡，不要再单独输出同一 backtest_id 的 ql:kline_review。"
)

__all__ = ["BLOCKS_GUIDE", "BLOCK_TYPES"]
