id: digest_daily
version: 1
title: 每日策略简报
temperature: 0.3
variables: [date, nav, period, change, max_drawdown, trades, win_rate, alerts]
---
<TASK>生成一份截至 {{date}} 的策略简报。</TASK>

<CONSTRAINTS>
- 不超过 200 字，适合微信阅读。
- 只陈述数据，不预测未来。
- 若存在异常（订单 UNKNOWN、对账差异、任务失败），必须在开头提示。
- 结尾固定附："仅为研究结果，非投资建议。"
</CONSTRAINTS>

参考格式：
【策略简报】{{date}}
· 净值：{{nav}}（{{period}}：{{change}}）
· 最大回撤：{{max_drawdown}}
· 交易：{{trades}} 笔，胜率 {{win_rate}}
· 异常：{{alerts}}
仅为研究结果，非投资建议。
