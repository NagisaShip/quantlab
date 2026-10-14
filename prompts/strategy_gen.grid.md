id: strategy_gen.grid
version: 1
title: 网格策略生成
temperature: 0.1
variables: [symbol, lower, upper, grids, max_inventory]
---
<TASK>生成一个有限仓位网格策略。</TASK>

<CONTEXT>
标的：{{symbol}}　下边界：{{lower}}　上边界：{{upper}}
网格数：{{grids}}　最大库存：{{max_inventory}}
</CONTEXT>

<CONSTRAINTS>
1. 只输出目标仓位，禁止调用交易所或网络。
2. 必须设置库存上限与越界暂停。
3. 需说明手续费敏感性与成本假设。
</CONSTRAINTS>

<OUTPUT_FORMAT>A.逻辑 B.代码 C.参数表 D.风险</OUTPUT_FORMAT>
