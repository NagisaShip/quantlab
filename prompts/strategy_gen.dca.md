id: strategy_gen.dca
version: 1
title: 定投再平衡策略生成
temperature: 0.1
variables: [symbol, contribution, interval_bars, rebalance_threshold, target_ratio]
---
<TASK>生成一个定投与再平衡策略，作为组合与基线策略。</TASK>

<CONTEXT>
标的：{{symbol}}　每期投入：{{contribution}}
投入间隔：{{interval_bars}} bar　再平衡阈值：{{rebalance_threshold}}
目标现货占比：{{target_ratio}}
</CONTEXT>

<CONSTRAINTS>
1. 只输出目标仓位，禁止调用交易所或网络。
2. 投入流水必须独立处理，不得混入策略收益。
3. 再平衡成本须计入。
</CONSTRAINTS>

<OUTPUT_FORMAT>A.逻辑 B.代码 C.参数表 D.风险</OUTPUT_FORMAT>
