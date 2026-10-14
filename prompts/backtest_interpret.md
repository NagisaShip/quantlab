id: backtest_interpret
version: 1
title: 回测解读
temperature: 0.3
variables: [metrics]
---
<TASK>把回测指标翻译成人话，并指出可信度。</TASK>

<METRICS>{{metrics}}</METRICS>

<CONSTRAINTS>
短区间年化可能夸大结果，须同时引用实际收益、交易次数与观测天数。
零波动或回撤为零时，相关比率显示"不适用"。
</CONSTRAINTS>

<OUTPUT_FORMAT>
1. 一句话结论
2. 收益/风险/交易/执行四个维度各一段
3. 可信度评估（样本量、样本外、压力测试）
4. 下一步建议实验
</OUTPUT_FORMAT>
