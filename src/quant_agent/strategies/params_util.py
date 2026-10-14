"""策略 PARAMS 字面量的读写工具。"""

from __future__ import annotations

import ast
import re
from typing import Any


class ParamsError(ValueError):
    pass


def rewrite_params(source: str, values: dict[str, Any]) -> str:
    """只替换 PARAMS 字面量里对应键的值，其余文本（含注释、格式）原样保留。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ParamsError(f"策略有语法错误，无法改写参数：第 {exc.lineno} 行") from exc
    node = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in n.targets)
        ),
        None,
    )
    if node is None or not isinstance(node.value, ast.Dict):
        raise ParamsError("策略没有字面量形式的 PARAMS 声明")
    lines = source.splitlines(keepends=True)
    start, end = node.lineno - 1, node.end_lineno
    block = "".join(lines[start:end])
    for key, value in values.items():
        literal = repr(value)
        pattern = re.compile(r'((["\'])' + re.escape(key) + r"\2\s*:\s*)([^,\n}#]+?)(\s*(,|\n|}|#))")
        block, n = pattern.subn(lambda m, lit=literal: f"{m.group(1)}{lit}{m.group(4)}", block, count=1)
        if n == 0:
            raise ParamsError(f"无法在 PARAMS 中定位参数 {key}")
    return "".join(lines[:start]) + block + "".join(lines[end:])
