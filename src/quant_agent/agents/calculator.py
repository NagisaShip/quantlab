"""安全的表达式计算器：给 Agent 做额度、成本、收益与仓位换算。

只解析 Python 表达式的 AST 白名单，不使用 eval；支持四则运算、幂、取模、
括号、列表，以及 abs / round / min / max / sum / sqrt / log / exp / sin / cos /
tan / floor / ceil 等纯函数。``percent(18, 80)`` 表示 80 的 18%。
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Any

MAX_EXPRESSION_CHARS = 500
MAX_EXPONENT = 1000

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "sqrt": math.sqrt,
    "log": math.log,
    "ln": math.log,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
    "percent": lambda p, value: p / 100 * value,
}
_FUNCTIONS["pct"] = _FUNCTIONS["percent"]
_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("只支持数字常量")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and abs(right) > MAX_EXPONENT:
            raise ValueError(f"指数绝对值不能超过 {MAX_EXPONENT}")
        return _BIN_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.List):
        return [_eval(x) for x in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(x) for x in node.elts)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS or node.keywords:
            raise ValueError("不支持这个函数")
        return _FUNCTIONS[node.func.id](*[_eval(arg) for arg in node.args])
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    raise ValueError("表达式里只能使用数字、括号、运算符和允许的函数")


def calculate(expression: str) -> dict[str, Any]:
    """计算一个纯数学表达式，返回 JSON 友好的结果。"""
    text = str(expression or "").strip()
    if not text:
        return {"error": "expression 不能为空"}
    if len(text) > MAX_EXPRESSION_CHARS:
        return {"error": f"表达式不能超过 {MAX_EXPRESSION_CHARS} 个字符"}
    # 常见计算器写法：^ 表示幂；允许中文全角括号。
    text = text.replace("^", "**").replace("（", "(").replace("）", ")").replace("，", ",")
    text = re.sub(r"\s+", " ", text)
    try:
        result = _eval(ast.parse(text, mode="eval"))
        if isinstance(result, float):
            if not math.isfinite(result):
                return {"error": "计算结果不是有限数字"}
            result = round(result, 12)
        if isinstance(result, (list, tuple)):
            return {"expression": text, "result": list(result), "result_text": str(list(result))}
        return {
            "expression": text,
            "result": result,
            "result_text": f"{result:.12g}" if isinstance(result, float) else str(result),
        }
    except ZeroDivisionError:
        return {"error": "除数不能为 0"}
    except OverflowError:
        return {"error": "计算结果溢出"}
    except (SyntaxError, TypeError, ValueError) as exc:
        return {"error": f"计算失败：{exc}"}
