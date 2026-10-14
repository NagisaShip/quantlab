id: research_market
version: 1
title: 市场调研
temperature: 0.4
variables: [symbol, strategy_name, timeframe]
---
<TASK>调研指定策略/标的市场表现的背景信息。</TASK>

<CONTEXT>标的：{{symbol}}；策略类型：{{strategy_name}}；关注区间：{{timeframe}}</CONTEXT>

<CONSTRAINTS>
- 区分"事实"与"推测"，推测需标注。
- 不得给出投资建议或收益承诺。
- 引用平台历史实验数据（query_runs）时注明 run_id。
</CONSTRAINTS>

<OUTPUT_FORMAT>要点式摘要（≤300 字）+ 数据来源 + 未解决问题。</OUTPUT_FORMAT>
