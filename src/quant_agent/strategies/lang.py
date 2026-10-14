"""策略语言分派：规则 DSL（默认、无代码执行）与高级 Python（隔离子进程执行）。"""

from __future__ import annotations

from typing import Any

from .dsl import DslError, load_dsl, parse_dsl_text, validate

DSL = "dsl"
PYTHON = "python"


def lang_of(kind: str | None) -> str:
    return DSL if kind == DSL else PYTHON


def issues(lang: str, source: str) -> list[str]:
    if lang == DSL:
        try:
            validate(parse_dsl_text(source))
        except DslError as exc:
            return [f"{p['path']}：{p['message']}" for p in exc.problems]
        return []
    from ..sdk import check_source

    return check_source(source)


def declared_params(lang: str, source: str) -> dict[str, Any]:
    if lang == DSL:
        try:
            return dict(parse_dsl_text(source).get("params") or {})
        except DslError:
            return {}
    from ..sdk import declared_params as py_params

    return py_params(source)


def load(lang: str, source: str, *, name: str, params: dict[str, Any] | None = None, path: str | None = None):
    if lang == DSL:
        return load_dsl(source, name=name, params=params or None)
    from ..sdk import load_source

    return load_source(source, name=name, params=params or None, path=path)
