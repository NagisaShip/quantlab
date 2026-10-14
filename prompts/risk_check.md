id: risk_check
version: 1
title: 风控校验
temperature: 0.1
variables: [constraints, strategy_config]
---
<TASK>检查策略是否符合账户与策略层风控。</TASK>

<CONSTRAINTS>{{constraints}}</CONSTRAINTS>
<STRATEGY_CONFIG>{{strategy_config}}</STRATEGY_CONFIG>

<OUTPUT_FORMAT>违规项列表 + 每条对应的调整建议。合规则明确回复"通过"。</OUTPUT_FORMAT>
