id: strategy_gen.mean_reversion
version: 1
title: 均值回归策略生成
temperature: 0.1
variables: [symbol, timeframe, window, deviation, max_hold_bars, stop_loss]
---
<TASK>根据用户描述生成一个均值回归策略。</TASK>

<CONTEXT>
标的：{{symbol}}　周期：{{timeframe}}
均值窗口：{{window}}　入场偏离：{{deviation}}
最长持有：{{max_hold_bars}}　止损：{{stop_loss}}
</CONTEXT>

<CONSTRAINTS>
1. 只输出目标仓位，禁止调用交易所或网络。
2. 只使用已收盘 bar，禁止未来数据。
3. 必须包含止损、持有时限。
4. 需说明震荡/趋势失效场景。
</CONSTRAINTS>

<OUTPUT_FORMAT>A.逻辑 B.代码 C.参数表 D.风险</OUTPUT_FORMAT>
