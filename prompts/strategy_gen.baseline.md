id: strategy_gen.baseline
version: 1
title: 基线策略生成
temperature: 0.0
variables: [symbol, ratio]
---
<TASK>生成买入持有基线策略。</TASK>
<CONTEXT>标的：{{symbol}}　买入比例：{{ratio}}</CONTEXT>
<CONSTRAINTS>同币种、同区间、同初始资金；只输出目标仓位。</CONSTRAINTS>
<OUTPUT_FORMAT>代码 + 一句说明</OUTPUT_FORMAT>
