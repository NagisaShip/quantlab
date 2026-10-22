"""Resolve explicitly registered local references without widening ctx.file access."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import WorkflowError


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def references(files):
    path = Path(files) / ".references.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def resolve_input(files, name, *, verify=True):
    files = Path(files).resolve()
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise WorkflowError("输入资源必须使用已导入的相对名称")
    ref = references(files).get(name)
    if ref:
        path = Path(ref["path"]).resolve()
        grants = json.loads(Path(ref["grants_file"]).read_text(encoding="utf-8"))
        allowed = [Path(g["path"]).resolve() for g in grants if g["mode"] in ("read", "write")]
        if not any(path.is_relative_to(root) for root in allowed):
            raise WorkflowError("引用目录授权已撤销：" + name)
        if not path.is_file():
            raise WorkflowError("引用文件已移动或缺失，请重新定位：" + name)
        if verify and digest(path) != ref["sha256"]:
            raise WorkflowError("引用文件内容已变化，旧运行指纹仍保留；请确认并刷新引用：" + name)
        return path
    path = (files / name).resolve()
    if not path.is_relative_to(files) or not path.is_file():
        raise WorkflowError("输入文件未导入：" + name)
    return path
