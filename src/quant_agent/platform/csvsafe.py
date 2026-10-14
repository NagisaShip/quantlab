"""CSV 公式注入防护（导入与导出两侧）。

电子表格会把以 `=`、`+`、`-`、`@`、制表符、回车开头的单元格当作公式执行
（例如 `=HYPERLINK(...)`、`=cmd|' /C calc'!A0`）。
- 导入：这类单元格如果不是合法数字，整行拒绝并报告“疑似公式注入”，绝不写入数据库；
- 导出：所有以这些字符开头的文本单元格前加单引号 `'`，在表格软件里显示为文本。
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from typing import Any

DANGEROUS_PREFIX = ("=", "+", "-", "@", "\t", "\r")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def is_formula(value: str) -> bool:
    v = value.lstrip(" ") if isinstance(value, str) else ""
    if not v or not v.startswith(DANGEROUS_PREFIX):
        return False
    return not _NUMBER.match(v.strip())


def neutralize(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(DANGEROUS_PREFIX) and not _NUMBER.match(value.strip()):
        return "'" + value
    return value


def to_csv(rows: Iterable[dict[str, Any]], columns: list[str]) -> str:
    """导出 CSV（UTF-8 带 BOM，Excel 打开中文不乱码），逐格做公式中和。"""
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([neutralize("" if r.get(c) is None else r.get(c)) for c in columns])
    return buf.getvalue()
