"""规则策略：基于 AST 白名单的可校验条件表达式。

设计来自实验三（QuantLab）的核心思路，这里吸收并加固：

- 用户提交的是**条件表达式**，不是 Python 代码。用 `ast.parse(mode="eval")`
  解析后逐节点检查白名单，禁止函数调用、属性访问、下标、推导式、幂运算等。
- 求值同样逐节点递归，不使用 `eval`/`exec`，也不导入用户代码。
- 只暴露 5 个指标名，且指标本身由平台计算，用户无法引入外部数据。
- 规则文本与参数一起进 SHA256，任一改变即产生新版本号（对应原方案"改参数生成新版本"）。

可用指标：
    close       最近一根已收盘 bar 的收盘价
    sma_fast    快周期简单移动平均
    sma_slow    慢周期简单移动平均
    rsi         指定周期涨跌幅简单平均 RSI（非 Wilder 平滑）
    position    当前持仓占净值比例，0–1

支持：比较（< <= > >= == !=）、括号、and / or / not、有限数值常量。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

# 只允许出现在规则里的指标名
INDICATOR_NAMES: frozenset[str] = frozenset({"close", "sma_fast", "sma_slow", "rsi", "position"})

# AST 节点白名单。刻意不含 Call / Attribute / Subscript / ListComp / Pow / BinOp。
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
)

MAX_RULE_LENGTH = 500
MAX_RULE_NODES = 100

# 常见误写的中文全角/半角符号，给出可读的改写建议
_CONFUSABLES = {
    "＞": ">",
    "＜": "<",
    "＝": "==",
    "！＝": "!=",
    "≥": ">=",
    "≤": "<=",
    "＆": " and ",
    "｜": " or ",
    "！": " not ",
    "(": "(",
    ")": ")",
}


class RuleError(ValueError):
    """规则不合法。消息面向使用者，直接展示在界面上。"""


def _normalize(text: str) -> str:
    """把中文标点替换成 ASCII，避免用户用全角符号时收到莫名的语法错误。"""
    out = text
    for src, dst in _CONFUSABLES.items():
        out = out.replace(src, dst)
    return out


def parse_rule(text: str) -> ast.expr:
    """解析并校验一条规则表达式，返回可复用的 AST 节点。

    校验失败抛 RuleError，消息说明具体原因与可用写法。
    """
    if not isinstance(text, str) or not text.strip():
        raise RuleError("规则不能为空")
    if len(text) > MAX_RULE_LENGTH:
        raise RuleError(f"规则最多 {MAX_RULE_LENGTH} 个字符，当前 {len(text)}")

    try:
        tree = ast.parse(_normalize(text), mode="eval")
    except SyntaxError as exc:
        raise RuleError(f"规则语法错误：{exc.msg}") from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_RULE_NODES:
        raise RuleError(f"规则过于复杂（{len(nodes)} 个节点，上限 {MAX_RULE_NODES}）")

    # 先查节点白名单，再查整体形状——这样 `eval('1')` 会得到
    # "不能调用函数" 这种具体原因，而不是笼统的"必须是比较条件"。
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            if isinstance(node, ast.Call):
                raise RuleError("规则不能调用函数，只支持指标比较与 and / or / not")
            if isinstance(node, ast.Attribute):
                raise RuleError("规则不能访问属性，只支持指标比较与 and / or / not")
            if isinstance(node, ast.Subscript):
                raise RuleError("规则不能取下标，只支持指标比较与 and / or / not")
            if isinstance(node, ast.BinOp):
                raise RuleError("规则不支持算术运算，请把计算结果写成指标（如 rsi）")
            raise RuleError(f"规则包含不支持的语法：{type(node).__name__}")

        if isinstance(node, ast.Name) and node.id not in INDICATOR_NAMES:
            available = "、".join(sorted(INDICATOR_NAMES))
            raise RuleError(f"未知指标 {node.id}；可用指标：{available}")

        if isinstance(node, ast.Constant) and not (
            isinstance(node.value, (int, float)) and not isinstance(node.value, bool) and math.isfinite(node.value)
        ):
            raise RuleError("规则常量只能是有限数值（布尔、字符串、NaN、无穷都不允许）")

    if not isinstance(tree.body, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
        raise RuleError("规则必须是一个指标比较或逻辑条件，例如 close > 100")

    return tree.body


def evaluate(node: ast.expr, context: dict[str, float]) -> Any:
    """按 AST 逐节点求值。

    注意：`Name` / `Constant` 返回**原始数值**而不是布尔值。这里曾经把它们
    包成 bool()，导致 `sma_fast > sma_slow` 变成 `True > True` → False，
    所有规则静默不触发。布尔化只在 BoolOp / UnaryOp 处发生。
    """
    if isinstance(node, ast.Name):
        try:
            return context[node.id]
        except KeyError as exc:  # 调用方没给全指标，属程序错误而非用户错误
            raise RuleError(f"求值时缺少指标 {node.id}") from exc

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.BoolOp):
        values = [bool(evaluate(v, context)) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    if isinstance(node, ast.UnaryOp):
        return not bool(evaluate(node.operand, context))

    if isinstance(node, ast.Compare):
        left = evaluate(node.left, context)
        for op, right_node in zip(node.ops, node.comparators, strict=False):
            right = evaluate(right_node, context)
            if isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.Eq):
                ok = left == right
            else:
                ok = left != right
            if not ok:
                return False
            left = right
        return True

    raise RuleError(f"无法计算的语法：{type(node).__name__}")


# ---------------------------------------------------------------------------
# 规则策略的完整定义与版本摘要
# ---------------------------------------------------------------------------


@dataclass
class RuleSpec:
    """一条规则策略的规范化定义。"""

    name: str
    team: str
    weight: float  # 目标仓位上限，0–1
    fast: int  # 快均线周期
    slow: int  # 慢均线周期
    period: int  # RSI 周期
    buy: str  # 买入条件
    sell: str  # 卖出条件
    kind: str = "rule"
    version: str = ""  # 参数 + 规则的 SHA256 前 12 位
    id: str = ""  # 同上前 16 位，用作稳定标识

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["indicators"] = sorted(INDICATOR_NAMES)
        return d

    @property
    def buy_ast(self) -> ast.expr:
        return parse_rule(self.buy)

    @property
    def sell_ast(self) -> ast.expr:
        return parse_rule(self.sell)


def _finite(value: Any, low: float, high: float, label: str) -> float:
    if isinstance(value, bool):
        raise RuleError(f"{label}需要数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuleError(f"{label}需要数值，收到 {value!r}") from exc
    if not math.isfinite(number) or not low <= number <= high:
        raise RuleError(f"{label}必须在 {low} 至 {high} 之间，收到 {number}")
    return number


def _integer(value: Any, low: float, high: float, label: str) -> int:
    number = _finite(value, low, high, label)
    if int(number) != number:
        raise RuleError(f"{label}需要整数，收到 {number}")
    return int(number)


def validate_rule_spec(raw: dict[str, Any]) -> RuleSpec:
    """校验并规范化一份规则策略提交。

    所有字段都会被重新赋值而不是直接信任输入；
    任一字段不合法都抛 RuleError 并说明原因。
    """
    if not isinstance(raw, dict):
        raise RuleError("策略必须是一个 JSON 对象")

    def text_field(key: str, label: str, max_len: int = 40) -> str:
        value = raw.get(key, "")
        if not isinstance(value, str) or not value.strip():
            raise RuleError(f"{label}不能为空")
        value = value.strip()
        if len(value) > max_len:
            raise RuleError(f"{label}最多 {max_len} 个字符，当前 {len(value)}")
        return value

    spec = RuleSpec(
        name=text_field("name", "策略名称"),
        team=text_field("team", "团队名称"),
        weight=_finite(raw.get("weight", 0.85), 0.0, 1.0, "目标仓位"),
        fast=_integer(raw.get("fast", 12), 2, 99, "快均线周期"),
        slow=_integer(raw.get("slow", 48), 3, 100, "慢均线周期"),
        period=_integer(raw.get("period", 14), 2, 99, "RSI 周期"),
        # 规则文本先做全角符号规范化再存，保证存储的就是真正参与解析与摘要的那一份
        buy=_normalize(text_field("buy", "买入条件", MAX_RULE_LENGTH)),
        sell=_normalize(text_field("sell", "卖出条件", MAX_RULE_LENGTH)),
    )
    if spec.fast >= spec.slow:
        raise RuleError(f"快均线周期（{spec.fast}）必须小于慢均线周期（{spec.slow}）")

    parse_rule(spec.buy)
    parse_rule(spec.sell)

    canonical = {
        "kind": spec.kind,
        "name": spec.name,
        "team": spec.team,
        "weight": spec.weight,
        "fast": spec.fast,
        "slow": spec.slow,
        "period": spec.period,
        "buy": spec.buy,
        "sell": spec.sell,
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    spec.version = digest[:12]
    spec.id = digest[:16]
    return spec


def rsi(closes: list[float], period: int) -> float:
    """简单涨跌幅平均的 RSI（非 Wilder 平滑），与实验三口径一致。"""
    if period < 1 or len(closes) < period + 1:
        return 50.0
    changes = [b - a for a, b in zip(closes[-period - 1 :], closes[-period:], strict=False)]
    gains = sum(max(x, 0.0) for x in changes)
    losses = sum(max(-x, 0.0) for x in changes)
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def indicator_context(
    closes: list[float], *, fast: int, slow: int, period: int, position: float
) -> dict[str, float] | None:
    """构造规则求值所需的指标上下文。

    历史长度不足（尚未预热完）时返回 None，调用方应跳过该 bar。
    """
    if len(closes) < slow:
        return None
    return {
        "close": closes[-1],
        "sma_fast": sum(closes[-fast:]) / fast,
        "sma_slow": sum(closes[-slow:]) / slow,
        "rsi": rsi(closes, period),
        "position": position,
    }


def rule_target(
    spec: RuleSpec,
    closes: list[float],
    position: float,
    *,
    buy_ast: ast.expr | None = None,
    sell_ast: ast.expr | None = None,
) -> float | None:
    """返回目标仓位；返回 None 表示"保持当前仓位不变"。

    卖出条件优先——买卖同时满足时先离场，这是保守假设，与实验三一致。
    """
    ctx = indicator_context(closes, fast=spec.fast, slow=spec.slow, period=spec.period, position=position)
    if ctx is None:
        return None
    buy = buy_ast if buy_ast is not None else spec.buy_ast
    sell = sell_ast if sell_ast is not None else spec.sell_ast
    if evaluate(sell, ctx):
        return 0.0
    if evaluate(buy, ctx):
        return spec.weight
    return None


def rule_summary(spec: RuleSpec) -> str:
    """一句人类可读的参数摘要，用于列表与卡片。"""
    return (
        f"买入：{spec.buy}｜卖出：{spec.sell}"
        f"（快 {spec.fast} / 慢 {spec.slow} / RSI {spec.period}，目标仓位 {spec.weight:.0%}）"
    )


RULE_TEMPLATE: dict[str, Any] = {
    "name": "趋势过滤 RSI",
    "team": "第四研究组",
    "kind": "rule",
    "weight": 0.8,
    "fast": 12,
    "slow": 48,
    "period": 14,
    "buy": "sma_fast > sma_slow and rsi < 60",
    "sell": "sma_fast < sma_slow or rsi > 75",
}
