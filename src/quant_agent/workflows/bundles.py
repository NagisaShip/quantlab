"""Explicit source bundles for portable multi-file function implementations."""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

from .contracts import WorkflowError


def sources(names, root=None):
    result = []
    for name in names:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name == "quant_agent":
            raise WorkflowError("源码包必须是独立顶层Python包，不能打包平台服务")
        if root is not None:
            base = Path(root).resolve() / name
            if not (base / "__init__.py").is_file():
                raise WorkflowError(f"实现包快照缺失：{name}")
        else:
            spec = importlib.util.find_spec(name)
            if (
                not spec
                or not spec.origin
                or not spec.submodule_search_locations
                or Path(spec.origin).name != "__init__.py"
            ):
                raise WorkflowError(f"找不到显式实现包：{name}")
            base = Path(spec.origin).resolve().parent
        files = []
        for path in sorted(base.rglob("*.py")):
            if not path.resolve().is_relative_to(base) or any(p.startswith(".") for p in path.relative_to(base).parts):
                continue
            code = path.read_text(encoding="utf-8")
            files.append(
                {
                    "path": name + "/" + path.relative_to(base).as_posix(),
                    "code": code,
                    "sha256": hashlib.sha256(code.encode()).hexdigest(),
                }
            )
        if sum(len(f["code"].encode()) for f in files) > 2_000_000:
            raise WorkflowError("单个实现包源码超过2MB")
        result.append({"name": name, "parent": str(base.parent), "files": files})
    return result
