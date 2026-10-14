id: strategy_review
version: 1
title: 策略审查
temperature: 0.2
variables: [strategy_code, metrics]
---
<TASK>审查以下策略，指出主要风险，不改写代码。</TASK>

<STRATEGY_CODE>{{strategy_code}}</STRATEGY_CODE>
<METRICS>{{metrics}}</METRICS>

<CHECKLIST>
- 是否使用了未来数据（look-ahead）？
- 是否只在盈利交易上扣费？
- 回撤控制是否只有仓位上限、没有止损？
- 参数是否过拟合（样本外是否崩掉）？
- 是否把"价格触及"当成"全部成交"？
</CHECKLIST>

<OUTPUT_FORMAT>逐条结论 + 总体评级（低/中/高风险）+ 建议的验证实验。</OUTPUT_FORMAT>
