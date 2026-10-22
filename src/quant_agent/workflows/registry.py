"""Immutable function versions, statically discoverable without importing user code."""

from __future__ import annotations

from pathlib import Path

from .contracts import WorkflowError, parse_definition


class Registry:
    def __init__(self, custom: list[dict] | None = None, *, frozen=False):
        self.definitions = {}
        if frozen:
            for item in custom or []:
                self.add(item["code"])
            return
        for path in sorted((Path(__file__).parent / "nodes").glob("*.py")):
            if not path.name.startswith("_"):
                self.add(path.read_text(encoding="utf-8"), builtin=True)
        for item in custom or []:
            self.add(item["code"])
        try:
            from quantlab_research.catalog import card_sources
            from quantlab_research.modules import module_sources
        except ModuleNotFoundError as exc:
            if exc.name != "quantlab_research":
                raise
        else:
            for code in [*card_sources(), *module_sources()]:
                self.add(code, builtin=True)

    def add(self, code: str, *, builtin: bool = False) -> dict:
        spec = parse_definition(code)
        key = (spec["kind"], str(spec["version"]))
        old = self.definitions.get(key)
        if old and old["code_hash"] != spec["code_hash"]:
            raise WorkflowError("同名同版本代码不可覆盖；请增加version或使用新的kind")
        spec["builtin"] = builtin
        self.definitions[key] = spec
        return spec

    def get(self, kind: str, version: str) -> dict:
        try:
            return self.definitions[kind, str(version)]
        except KeyError as exc:
            raise WorkflowError(f"卡片未安装：{kind}@{version}") from exc

    def catalog(self) -> list[dict]:
        return list(self.definitions.values())
