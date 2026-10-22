"""项目 / 工作区级技能（skills）目录：只读 ``*.md``，用于给研究规则补充可复用知识。

布局：

- 本地模式：``<项目>/.quantlab/skills/*.md``（``services.home`` 即项目数据目录）；
- 账号模式：``<数据目录>/workspaces/<工作区>/skills/*.md``（与工作区 store 并列）。

安全边界：只读、只看目录第一层、只接受普通 ``.md`` 文件；拒绝路径穿越、符号链接逃逸
与超大文件；不执行文件里的任何内容，也不写入技能目录。技能内容可由研究者手动引用，
或通过 ``POST /api/skills/{name}/attach`` 追加到某个 Lab 项目的 instructions。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .lab_state import MAX_INSTRUCTIONS_CHARS

SKILLS_DIRNAME = "skills"
SKILL_SUFFIX = ".md"
MAX_SKILL_BYTES = 64_000
MAX_SKILLS = 200
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 160

#: 技能名（= 文件名去掉 .md）：字母数字开头，只允许字母数字、点、下划线、连字符
SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+?)\s*#*\s*$")


class SkillError(Exception):
    """技能读取失败；``status`` 供 API 层映射 HTTP 状态码。"""

    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidSkillName(SkillError):
    status = 400


class SkillNotFound(SkillError):
    status = 404


class SkillTooLarge(SkillError):
    status = 413


def skills_root(services: Any) -> Path:
    """当前请求的可用技能目录（不创建目录）：本地=项目级，账号=工作区级。"""
    local = getattr(services, "local", None)
    if local is not None:
        return (Path(services.home) / SKILLS_DIRNAME).resolve()
    # 账号模式：WorkspaceStore.current() 的 root 是 <...>/workspaces/<id>/store
    workspace_store_root = Path(services.store.current().root)
    return (workspace_store_root.parent / SKILLS_DIRNAME).resolve()


def validate_name(name: str) -> str:
    candidate = str(name or "").strip()
    if not SKILL_NAME_RE.fullmatch(candidate):
        raise InvalidSkillName("技能名不合法：只允许字母数字与 . _ -，且以字母数字开头")
    return candidate


def _safe_file(root: Path, name: str) -> Path:
    """把技能名解析成目录内的普通文件；同时挡住符号链接逃逸。"""
    validate_name(name)
    path = root / f"{name}{SKILL_SUFFIX}"
    try:
        resolved = path.resolve()
    except OSError as exc:  # pragma: no cover - 极端文件系统错误
        raise SkillNotFound("技能文件不存在") from exc
    if resolved.parent != root or path.is_symlink() or not resolved.is_file():
        raise SkillNotFound("技能文件不存在")
    return resolved


def _title_and_description(text: str, name: str) -> tuple[str, str]:
    title = name
    description = ""
    for line in text.splitlines()[:80]:
        stripped = line.strip()
        if not stripped:
            continue
        heading = _HEADING_RE.match(stripped)
        if heading:
            if title == name:
                title = heading.group(1).strip()[:MAX_TITLE_CHARS] or name
            continue
        if not description and not stripped.startswith(("```", "---", "<")):
            description = stripped[:MAX_DESCRIPTION_CHARS]
        if title != name and description:
            break
    return title, description


def _meta(path: Path, name: str, *, text: str | None = None) -> dict[str, Any]:
    stat = path.stat()
    if text is None:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SKILL_BYTES + 1)
        if len(raw) > MAX_SKILL_BYTES:
            raise SkillTooLarge(f"技能文件超过 {MAX_SKILL_BYTES // 1000}KB 上限")
        text = raw.decode("utf-8", errors="replace")
    title, description = _title_and_description(text, name)
    return {
        "name": name,
        "title": title,
        "description": description,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
        "file": f"{name}{SKILL_SUFFIX}",
    }


def list_skills(services: Any) -> dict[str, Any]:
    """列出可用技能；超大 / 非法 / 非普通文件放进 ``skipped``，不读内容也不报 500。"""
    root = skills_root(services)
    result: dict[str, Any] = {
        "root": str(root),
        "exists": root.is_dir(),
        "max_bytes": MAX_SKILL_BYTES,
        "count": 0,
        "skills": [],
        "skipped": [],
        "note": "只读项目/工作区 skills 目录第一层的 .md 文件；不执行技能内容。",
    }
    if not root.is_dir():
        return result
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        result["note"] = "技能目录不可读"
        return result
    skills: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in entries:
        try:
            if path.is_symlink():
                skipped.append({"name": path.name[:80], "reason": "symlink"})
                continue
            if path.is_dir():
                skipped.append({"name": path.name[:80], "reason": "directory"})
                continue
            if not path.name.lower().endswith(SKILL_SUFFIX):
                skipped.append({"name": path.name[:80], "reason": "not_markdown"})
                continue
            name = path.stem
            if not SKILL_NAME_RE.fullmatch(name):
                skipped.append({"name": path.name[:80], "reason": "invalid_name"})
                continue
            if len(skills) >= MAX_SKILLS:
                skipped.append({"name": path.name[:80], "reason": "too_many"})
                continue
            if path.stat().st_size > MAX_SKILL_BYTES:
                skipped.append({"name": name, "reason": "too_large"})
                continue
            safe = _safe_file(root, name)
            skills.append(_meta(safe, name))
        except OSError:
            skipped.append({"name": path.name[:80], "reason": "unreadable"})
        except SkillError as exc:  # 目录解析期间文件被替换 / 越界：跳过而不是让列表失败
            skipped.append({"name": path.name[:80], "reason": type(exc).__name__})
    result["skills"] = skills
    result["skipped"] = skipped[:MAX_SKILLS]
    result["count"] = len(skills)
    return result


def read_skill(services: Any, name: str) -> dict[str, Any]:
    """读取一个技能全文；非法名 400，越界 / 不存在 404，超大 413。"""
    root = skills_root(services)
    path = _safe_file(root, name)
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise SkillTooLarge(f"技能文件超过 {MAX_SKILL_BYTES // 1000}KB 上限")
    with path.open("rb") as stream:
        raw = stream.read(MAX_SKILL_BYTES + 1)
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillTooLarge(f"技能文件超过 {MAX_SKILL_BYTES // 1000}KB 上限")
    text = raw.decode("utf-8", errors="replace")
    return {**_meta(path, path.stem), "content": text}


def skill_instructions(services: Any, name: str) -> str:
    """把技能包装成可追加到 Lab 项目 instructions 的文本块（不写库，由调用方决定）。"""
    skill = read_skill(services, name)
    block = f"【技能：{skill['name']}】\n{skill['content']}".strip()
    return block[:MAX_INSTRUCTIONS_CHARS]


__all__ = [
    "MAX_SKILLS",
    "MAX_SKILL_BYTES",
    "SKILLS_DIRNAME",
    "SKILL_NAME_RE",
    "InvalidSkillName",
    "SkillError",
    "SkillNotFound",
    "SkillTooLarge",
    "list_skills",
    "read_skill",
    "skill_instructions",
    "skills_root",
    "validate_name",
]
