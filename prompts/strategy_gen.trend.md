id: strategy_gen.trend
version: 1
title: 趋势策略生成
temperature: 0.1
variables: [symbol, timeframe, fast, slow, max_position, stop_loss]
---
<TASK>根据用户描述生成一个趋势跟随策略。</TASK>

<CONTEXT>
标的：{{symbol}}
周期：{{timeframe}}
快线：{{fast}}　慢线：{{slow}}
仓位上限：{{max_position}}　止损：{{stop_loss}}
</CONTEXT>

<CONSTRAINTS>
1. 只输出目标仓位（ctx.set_target_position），禁止调用交易所或网络。
2. 只使用已收盘的 bar，禁止未来数据。
3. 必须包含止损与仓位上限。
4. 缺少关键参数先列出澄清问题。
</CONSTRAINTS>

<OUTPUT_FORMAT>
A. 策略逻辑说明（3–5 句）
B. Python 代码（initialize / on_bar / snapshot / restore）
C. 参数表（名称、默认值、范围、含义）
D. 风险与局限
</OUTPUT_FORMAT>
