"""规则策略的运行器：把 RuleSpec 接到平台回测引擎的策略接口上。

这样规则策略与 Python 模板策略走完全相同的账户核算、成本模型与风控路径，
避免"两套账"——这正是原方案强调的"复用策略、组合和风控逻辑"。
"""

from __future__ import annotations

from ..exchanges.base import Bar
from .rules import RuleSpec, parse_rule, rule_target
from .sdk import StrategyBase, StrategyContext


class RuleStrategy(StrategyBase):
    """把一个 RuleSpec 包成平台策略。"""

    def __init__(self, spec: RuleSpec) -> None:
        super().__init__(params={})
        self.spec = spec
        self.name = f"rule:{spec.name}"
        # 规则在初始化时就解析成 AST，运行期不再解析（既快也不会中途报错）
        self._buy_ast = parse_rule(spec.buy)
        self._sell_ast = parse_rule(spec.sell)
        self.label = spec.name
        self.description = f"{spec.team} 提交的规则策略"
        self.defaults = {
            "fast": spec.fast,
            "slow": spec.slow,
            "period": spec.period,
            "weight": spec.weight,
        }

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        closes = [b.close for b in ctx.history(bar.symbol, self.spec.slow)]
        if len(closes) < self.spec.slow:
            return
        equity = ctx.cash + ctx.position * bar.close
        position = (ctx.position * bar.close / equity) if equity > 0 else 0.0
        target = rule_target(
            self.spec,
            closes,
            position,
            buy_ast=self._buy_ast,
            sell_ast=self._sell_ast,
        )
        if target is not None:  # None = 保持当前仓位
            ctx.set_target_position(target)
