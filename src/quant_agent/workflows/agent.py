"""Agent calls use the same workspace service and immutable definitions as GUI."""

from __future__ import annotations

import inspect as signature_inspect
import os
import re
import sys
import uuid
from pathlib import Path

from .contracts import WorkflowError
from .views import workflow_view

# ---- 本地交付物工具：只在本地项目可用，写入必须落在用户显式授权的可写目录 ----
MAX_TEXT_BYTES = 2 * 1024 * 1024
TEXT_READ_LIMIT = 20000
#: 明确按二进制/办公文件处理，不允许 local_read_text 假装读出文本。
BINARY_SUFFIXES = frozenset(
    {
        ".bin",
        ".bmp",
        ".db",
        ".dll",
        ".doc",
        ".docx",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mp3",
        ".mp4",
        ".otf",
        ".parquet",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".pyc",
        ".rar",
        ".so",
        ".sqlite",
        ".sqlite3",
        ".ttf",
        ".wav",
        ".webp",
        ".woff",
        ".woff2",
        ".xls",
        ".xlsm",
        ".xlsx",
        ".zip",
        ".7z",
    }
)
#: 表格类文件走既有的表格预览（CSV/Excel/Parquet/JSONL）；其余按文本读取。
TABLE_SUFFIXES = frozenset({".csv", ".tsv", ".xlsx", ".xlsm", ".parquet", ".jsonl", ".ndjson"})


def service():
    from ..api.deps import get_services
    from .service import WorkflowService

    return WorkflowService(get_services())


def catalog(section: str = "cards", cursor: str = "", limit: int = 6, query: str = ""):
    s = service()
    if section not in ("cards", "workflows"):
        raise WorkflowError("section仅支持cards或workflows")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise WorkflowError("limit须为1—10")
    if cursor and (not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit()):
        raise WorkflowError("cursor须使用上次返回的next_cursor")
    offset = int(cursor or 0)
    workflows = [{k: r[k] for k in ("id", "title", "revision")} for r in s.workflows()]
    cards = []
    for d in sorted(s.registry().catalog(), key=lambda d: (d["kind"], str(d["version"]))):
        cards.append(
            {
                "kind": d["kind"],
                "version": str(d["version"]),
                "title": d["title"],
                "level": d.get("level", "function"),
                "inputs": {k: p["type"] for k, p in d["inputs"].items()},
                "outputs": {k: p["type"] for k, p in d["outputs"].items()},
            }
        )
    rows = cards if section == "cards" else workflows
    rows = [r for r in rows if query.lower() in str(r).lower()]
    page = rows[offset : offset + limit]
    return {
        section: page,
        **({"workflows": workflows[:5]} if section == "cards" else {}),
        "card_total": len(cards),
        "section": section,
        "total": len(rows),
        "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
        "truncated": offset + limit < len(rows),
        "workflow_total": len(workflows),
        "workflow_next_cursor": "5" if section == "cards" and len(workflows) > 5 else None,
        "guide": "workflow_help获取创建/连接示例；workflow_sources分段读源码；workflow_create创建首版；资产快照本身不是行情数据。",
    }


def inspect(workflow_id: str, run_id: str = "", node_id: str = "", child_run_id: str = "", revision: int | None = None):
    s = service()
    if child_run_id and not run_id:
        run_id = s.find_parent(child_run_id, workflow_id)
    record, platform_run, report = s.inspected(workflow_id, run_id, revision)
    view = workflow_view(s, record, report, platform_run)
    if child_run_id:
        if not run_id:
            raise WorkflowError("读取子运行需提供父run_id")
        report = s.child_report(run_id, child_run_id)
    if node_id:
        if not report or node_id not in report["nodes"]:
            raise WorkflowError("本次运行尚无该节点证据；先读取不带node_id的workflow_inspect")
        return {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "node_id": node_id,
            "node": report["nodes"][node_id],
            "workflow_view": view,
        }
    return {
        "workflow": record,
        "run": report,
        "platform_run": {k: platform_run.get(k) for k in ("id", "state", "error_message")} if platform_run else None,
        "workflow_view": view,
    }


def validate(workflow_id: str, target: str = ""):
    s = service()
    record = s.get(workflow_id)
    checks = s.validate(record["workflow"], target=target or None)
    return {**checks, "workflow_view": workflow_view(s, record, validation=checks)}


def run(
    workflow_id: str,
    target: str = "",
    mode: str = "research",
    source_run_id: str = "",
    revision: int | None = None,
    seed: int | None = None,
):
    s = service()
    pins = None
    if source_run_id:
        if mode != "debug" or not target:
            raise WorkflowError("历史输入重放需要mode=debug和target")
        _, _, history = s.inspected(workflow_id, source_run_id)
        if not history:
            raise WorkflowError("源运行没有可用证据")
        flow = s.get(workflow_id, revision)["workflow"]
        parents = set()

        def visit(nid):
            for edge in flow["edges"]:
                if edge["target"] == nid and edge["source"] not in parents:
                    parents.add(edge["source"])
                    visit(edge["source"])

        visit(target)
        pins = {}
        for nid in parents:
            outputs = history["nodes"].get(nid, {}).get("outputs")
            if not outputs:
                raise WorkflowError(f"源运行缺少{nid}的输出")
            pins[nid] = outputs
    result = s.queue(
        workflow_id, target=target or None, origin="agent", mode=mode, pins=pins, revision=revision, seed=seed
    )
    record, actual, report = s.inspected(workflow_id, result["id"])
    return {**result, "workflow_view": workflow_view(s, record, report, actual)}


def revise(workflow: dict, workflow_id: str = "", expected_revision: int = 0):
    s = service()
    before = s.store.get("workflows", workflow_id) if workflow_id else None
    result = s.save(workflow, wid=workflow_id or None, expected_revision=expected_revision, origin="agent")
    return {
        "id": result["id"],
        "revision": result["revision"],
        "before": before["workflow"] if before else None,
        "after": result["workflow"],
        "created": before is None,
        "workflow_view": workflow_view(s, result),
    }


def create(workflow: dict):
    return revise(workflow=workflow)


def propose(workflow: dict, workflow_id: str = "", expected_revision: int = 0):
    from .contracts import fingerprint, normalize_workflow

    s = service()
    workflow = normalize_workflow(workflow)
    before = s.get(workflow_id) if workflow_id else None
    if before and before["revision"] != expected_revision:
        raise WorkflowError("提案基于过期修订，请先读取当前工作流")
    old = {n["id"]: n for n in (before or {}).get("workflow", {}).get("nodes", [])}
    new = {n["id"]: n for n in workflow["nodes"]}
    changes = {
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "changed": [k for k in old.keys() & new.keys() if old[k] != new[k]],
        "edges_before": len((before or {}).get("workflow", {}).get("edges", [])),
        "edges_after": len(workflow.get("edges", [])),
    }
    checks = s.validate(workflow)
    record = {
        "id": workflow_id or "draft-" + fingerprint(workflow)[:12],
        "revision": expected_revision,
        "title": workflow.get("title", "工作流提案"),
        "workflow": workflow,
    }
    view = workflow_view(s, record, validation=checks)
    view.update(
        state="draft",
        evidence="draft",
        proposal={
            "workflow": workflow,
            "workflow_id": workflow_id,
            "expected_revision": expected_revision,
            "changes": changes,
        },
    )
    return {
        "saved": False,
        "changes": changes,
        "validation": checks,
        "workflow_view": view,
        "note": "只生成提案；右侧可查看差异后保存，或使用workflow_revise明确保存。",
    }


def help_workflow(topic: str = "workflow"):
    from .guide import help_for

    return help_for(topic)


def list_files():
    s = service()
    return {
        "files": [
            {"name": p.name, "bytes": p.stat().st_size}
            for p in sorted(s.files.iterdir())
            if p.is_file() and not p.name.startswith(".")
        ],
        "note": (
            "source.market 直连交易所公开K线，无需导入文件（声明network，不能导出为完整离线包）；"
            "source.csv.params.file使用name，导入入口在研究工作流数据源卡片。"
        ),
    }


def inspect_network(graph: dict):
    from quantlab_research.network import code, inspect

    return {**inspect(graph), "code": code(graph)}


def preview_chart(config: dict, artifact_hash: str = "", data: list[dict] | None = None):
    s = service()
    from .plugins import require

    require(s, "charts", agent=True)
    s.writable()
    data = s.chart_data(artifact_hash, data)
    spec = s.chart_preview(data, config, render=True)
    from .runtime import Runner

    ref = Runner(s.root, s.registry()).artifact(spec, {"type": "report"})
    import time

    s.store.upsert(
        "workflow_preview_artifacts",
        {
            **ref,
            "id": ref["hash"],
            "title": str(config.get("title") or "Agent 图表预览"),
            "node_id": "",
            "port": "chart",
            "workflow_id": "",
            "workflow_title": "独立图表预览",
            "revision": 0,
            "run_id": "",
            "mode": "preview",
            "status": "completed",
            "created_at": time.time() * 1000,
            "origin": "agent",
        },
    )
    shown = read_artifact(ref["hash"])
    return {
        **spec,
        "data": spec["data"][:20],
        "preview_limit": 20,
        "total": len(spec["data"]),
        "truncated": len(spec["data"]) > 20,
        "artifact_hash": ref["hash"],
        "artifact_view": shown["artifact_view"],
    }


def read_artifact(artifact_hash: str, offset: int = 0, limit: int = 20):
    from ..platform.context import current_workspace
    from .artifacts import preview

    value = preview(service(), artifact_hash, offset, limit)
    return {
        **value,
        "namespace": "workflow.run.output",
        "artifact_view": {**value, "workspace_id": current_workspace()},
    }


def artifacts(workflow_id: str = "", run_id: str = "", query: str = "", offset: int = 0, limit: int = 40):
    from .artifacts import listing

    return listing(service(), workflow_id, run_id, query, offset, limit)


def cancel(run_id: str):
    from ..api import tasks
    from ..api.routes_runs import apply_inline
    from ..platform import context

    s = service()
    s.writable()
    actual = s.services.runs.require(run_id, context.current_workspace())
    if not actual.get("manifest", {}).get("workflow_id"):
        raise WorkflowError("该任务不是工作流运行")
    command = s.services.runs.command(run_id, context.current_workspace(), context.current().user_id, "cancel")
    if tasks.worker_mode() == "inline":
        apply_inline(run_id)
    return {"command": command, "run_id": run_id, "state": s.services.runs.get(run_id)["state"]}


def export(workflow_id: str = "", run_id: str = "", include_wheels: bool = True):
    from ..platform.context import current_workspace

    s = service()
    if run_id:
        result = s.queue_run_export(run_id, include_wheels=include_wheels)
    elif workflow_id:
        result = s.queue_export(workflow_id, include_wheels=include_wheels)
    else:
        raise WorkflowError("请提供workflow_id或run_id")
    view = {k: result.get(k) for k in ("id", "title", "state", "summary", "progress", "error_message")}
    return {**result, "task_view": {**view, "workspace_id": current_workspace()}}


def inspect_sources(kind: str, version: str, file_path: str = "", offset: int = 0, limit: int = 2400):
    result = service().definition_sources(kind, version)
    if (
        any(isinstance(v, bool) or not isinstance(v, int) for v in (offset, limit))
        or offset < 0
        or not 1 <= limit <= 3000
    ):
        raise WorkflowError("源码offset须非负，limit须为1—3000字符")
    definition = result["definition"]
    files = [{"path": "NODE.py", "code": definition["code"], "sha256": definition["code_hash"]}, *result["files"]]
    if file_path:
        chosen = next((f for f in files if f["path"] == file_path), None)
        if not chosen:
            raise WorkflowError("此函数没有指定实现文件")
        code = chosen["code"]
        return {
            "kind": kind,
            "version": version,
            "file": file_path,
            "sha256": chosen["sha256"],
            "code": code[offset : offset + limit],
            "total_chars": len(code),
            "next_offset": offset + limit if offset + limit < len(code) else None,
            "truncated": offset + limit < len(code),
        }
    return {
        "definition": {k: definition[k] for k in ("kind", "version", "title", "inputs", "outputs", "code_hash")},
        "parameter_names": list(definition["parameters"].get("properties", {})),
        "files": [{"path": f["path"], "sha256": f["sha256"], "total_chars": len(f["code"])} for f in files],
        "note": "file_path='NODE.py'分段读取完整契约和execute入口；使用next_offset继续。",
    }


def preview_definition(code: str, old_kind: str = "", old_version: str = ""):
    from .contracts import parse_definition

    return (
        service().interface_diff(old_kind, old_version, code)
        if old_kind
        else {"definition": parse_definition(code), "changed": {}}
    )


def publish_definition(code: str):
    return service().publish_card(code, origin="agent")


def list_assets():
    from .assets import listing

    return {"assets": listing(service())}


def snapshot_asset(domain: str, asset_id: str):
    from .assets import snapshot

    s = service()
    s.writable()
    definition = snapshot(s, domain, asset_id, origin="agent")
    return {
        "kind": definition["kind"],
        "version": str(definition["version"]),
        "title": definition["title"],
        "code_hash": definition["code_hash"],
        "inputs": definition["inputs"],
        "outputs": definition["outputs"],
        "parameter_names": list(definition["parameters"].get("properties", {})),
        "asset": {k: definition["asset"][k] for k in ("domain", "id", "sha256")},
        "truncated": False,
        "definition_ref": {
            "tool": "workflow_sources",
            "kind": definition["kind"],
            "version": str(definition["version"]),
            "file_path": "NODE.py",
            "offset": 0,
            "limit": 2400,
        },
        "naming": "user.asset_ + 来源类别/资产ID/内容指纹的前12位；界面名称为title，同内容快照复用版本。",
    }


def import_data(name: str, data: list[dict] | None = None, csv_text: str = ""):
    """Explicit data only; local disk access has a separate authorized-root tool."""
    return service().import_data(name, data=data, csv_text=csv_text)


def local_files(
    path: str = "",
    action: str = "browse",
    mode: str = "reference",
    name: str = "",
    limit: int = 20,
    offset: int = 0,
):
    s = service()
    if not s.services.local:
        raise WorkflowError("本地目录工具仅在本地模式可用；账号版使用workflow_import_data或GUI导入")
    p = s.services.local
    if action == "browse":
        return p.browse(path)
    if action == "import":
        return p.import_file(s, path, mode, name)
    if action == "preview":
        target = p.authorize(path)
        suffix = target.suffix.lower()
        if suffix in BINARY_SUFFIXES:
            raise WorkflowError(
                f"这是二进制/办公文件（{suffix or '无扩展名'}），不能用文本预览，也不会假装读出内容；"
                "docx/pdf/xlsx 等请用系统应用打开或使用对应的导出/生成工具。"
                "文本文件（.md/.txt/.json/.py/.csv 等）用 action=preview 或 local_read_text（支持 offset 分段）；"
                "表格文件支持 CSV/TSV/XLSX/XLSM/Parquet/JSONL。"
            )
        if suffix in TABLE_SUFFIXES:
            return s.preview_file(target, limit=limit)
        if not target.is_file():
            raise WorkflowError("文件不存在")
        # .md/.txt/.json/.py 等文本：直接按文本读取，不再走“只支持表格”的预览分支。
        return {**local_read_text(str(target), offset=offset, limit=TEXT_READ_LIMIT), "kind": "text"}
    raise WorkflowError("action仅支持browse、preview、import；目录授权由用户在本地项目页面设置")


# ---------------------------------------------------------------------------
# 本地交付物：写文本 / 建目录 / 读文本 / 导出 Word、PPT
#
# 边界（与平台审批一起工作）：
# - 只在本地项目可用（services.local 非空）；账号模式明确报错，不静默退化；
# - 写入只走 local_profile.authorize(write=True) 已授权的可写目录，默认是项目目录；
# - 平台数据目录（.quantlab）、凭证目录与模块库目录一律拒绝读写；
# - 不提供任意 shell / Python 执行能力。
# ---------------------------------------------------------------------------
def _local_profile():
    s = service()
    profile = s.services.local
    if profile is None:
        raise WorkflowError("仅本地项目可用：请在桌面端打开一个本地项目后再使用本机文件与交付物工具")
    return s, profile


def _private_roots(profile):
    """Agent 不能读写的平台目录：数据目录、凭证密钥目录、模块库。"""
    roots = [profile.home.resolve()]
    credential = os.environ.get("QUANTLAB_CREDENTIALS")
    if not credential:
        credential = str(Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "QuantLab" / "keys")
    roots.append(Path(credential).expanduser().resolve())
    library = os.environ.get("QUANTLAB_LIBRARY")
    roots.append(
        Path(library).expanduser().resolve() if library else (Path.home() / ".quantlab" / "module-library").resolve()
    )
    return roots


def _guard_private(profile, target):
    for root in _private_roots(profile):
        if target.is_relative_to(root):
            raise WorkflowError(
                "该路径属于平台数据目录、凭证密钥目录或模块库；Agent 只能读写用户授权的项目目录，"
                "不能用本机工具读取密钥或改写平台状态"
            )


def _authorized_target(profile, path, *, write):
    if not isinstance(path, str) or not path.strip():
        raise WorkflowError("path须为非空字符串（绝对路径或项目内相对路径）")
    # LocalProject.authorize 内部把相对路径按项目根解析（不依赖后端进程 cwd），再做授权校验。
    target = profile.authorize(path.strip(), write=write)
    _guard_private(profile, target)
    return target


def _temp_beside(target):
    return target.with_name("." + target.name + "." + uuid.uuid4().hex[:8] + ".tmp")


def _atomic_write(target, data):
    tmp = _temp_beside(target)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:
        raise WorkflowError(f"写入失败：{exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def local_write_text(path: str, text: str, overwrite: bool = False, append: bool = False):
    """写入已授权目录内的 UTF-8 文本；默认不覆盖，单文件上限 2MB。

    append=True 时追加到已有文件（用于分段写长报告）；append 与 overwrite 互斥。
    """
    _s, profile = _local_profile()
    if not isinstance(text, str):
        raise WorkflowError("text须为字符串")
    if append and overwrite:
        raise WorkflowError("append 与 overwrite 不能同时为 true")
    target = _authorized_target(profile, path, write=True)
    if target.exists() and target.is_dir():
        raise WorkflowError("目标路径是目录，不能写入文本文件")
    existed = target.exists()
    if existed and not overwrite and not append:
        raise WorkflowError("目标文件已存在；确需覆盖请显式传overwrite=true，或在末尾追加请传append=true")
    previous = target.stat().st_size if (existed and append) else 0
    data = text.encode("utf-8")
    if previous + len(data) > MAX_TEXT_BYTES:
        raise WorkflowError(
            f"单个文本文件上限2MB（当前已存在{previous}字节，本次{len(data)}字节）；请拆分文件或改用其他交付方式"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if append and existed:
        with target.open("ab") as stream:
            stream.write(data)
    else:
        _atomic_write(target, data)
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "written_bytes": len(data),
        "chars": len(text),
        "appended": bool(append and existed),
        "overwritten": existed and not append,
        "encoding": "utf-8",
    }


def local_make_dir(path: str):
    """在已授权可写目录内创建目录（含多级父目录）。"""
    _s, profile = _local_profile()
    target = _authorized_target(profile, path, write=True)
    if target.exists() and not target.is_dir():
        raise WorkflowError("目标已存在且不是目录")
    created = not target.exists()
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkflowError(f"创建目录失败：{exc}") from exc
    return {"path": str(target), "created": created}


def _ensure_text_file(target):
    if target.suffix.lower() in BINARY_SUFFIXES:
        raise WorkflowError(
            f"不支持读取 {target.suffix} 等二进制/办公文件；local_read_text 只读 UTF-8 文本。"
            "docx/pdf/xlsx 请用系统应用打开或改用导出工具，不要当作纯文本读取"
        )
    try:
        with target.open("rb") as stream:
            head = stream.read(4096)
    except OSError as exc:
        raise WorkflowError(f"读取失败：{exc}") from exc
    if b"\x00" in head:
        raise WorkflowError("文件包含二进制内容，不能按文本读取；请确认它确实是 UTF-8 文本文件")


def read_text_target(target, offset: int = 0, limit: int = TEXT_READ_LIMIT):
    """读取已验证授权/私密边界后的 UTF-8 文本；供 Agent 工具与本地预览路由共用。"""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise WorkflowError("offset须为非负整数")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= TEXT_READ_LIMIT:
        raise WorkflowError(f"limit须为1—{TEXT_READ_LIMIT}")
    if not target.exists():
        raise WorkflowError("文件不存在")
    if not target.is_file():
        raise WorkflowError("目标不是文件；目录请用browse")
    _ensure_text_file(target)
    try:
        with target.open("r", encoding="utf-8", errors="replace", newline="") as stream:
            text = stream.read(offset + limit + 1)
    except OSError as exc:
        raise WorkflowError(f"读取失败：{exc}") from exc
    truncated = len(text) > offset + limit
    return {
        "path": str(target),
        "text": text[offset : offset + limit],
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "has_more": truncated,
        "more_hint": (
            f"还有更多内容：继续调用 local_read_text(offset={offset + limit})，或传更大的limit"
            if truncated
            else "已读到文件结尾"
        ),
        "next_offset": offset + limit if truncated else None,
        "bytes": target.stat().st_size,
    }


def local_read_text(path: str, offset: int = 0, limit: int = TEXT_READ_LIMIT):
    """分段读取已授权目录内的文本；二进制/办公文件明确报错。"""
    _s, profile = _local_profile()
    target = _authorized_target(profile, path, write=False)
    return read_text_target(target, offset, limit)


def _missing_library(package, label):
    return (
        f"当前服务的 Python 未安装 {package}，无法导出{label}。请在运行 QuantLab 的 Python 中执行："
        f'"{sys.executable}" -m pip install "{package}"，或安装交付物可选依赖：'
        'pip install -e ".[deliverables]"；安装后重试（桌面版需重启服务）。这是可选依赖缺失，不是研究失败。'
    )


def _export_target(profile, path, suffix):
    target = _authorized_target(profile, path, write=True)
    if target.suffix.lower() != suffix:
        raise WorkflowError(f"导出文件路径须以{suffix}结尾；收到{target.suffix or '无扩展名'}")
    if target.exists() and target.is_dir():
        raise WorkflowError("目标路径是目录，不能导出为文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _md_inline(value):
    """去掉简单 Markdown 的行内标记，保留可读文本。"""
    text = str(value)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    return re.sub(r"`([^`]+)`", r"\1", text).strip()


def _table_cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [_md_inline(cell) for cell in line.split("|")]


def _is_table_separator(line):
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-+:?", cell) for cell in cells)


def _resolve_md_image(src, base_dir=None, allowed_root=None):
    """解析 Markdown 图片地址：支持本地绝对路径、相对 Markdown 的路径，以及 /api/local/files/image?path=。"""
    from urllib.parse import parse_qs, unquote, urlparse

    raw = unquote(str(src or "").strip())
    if raw.startswith("/api/local/files/image"):
        query = parse_qs(urlparse(raw).query)
        raw = (query.get("path") or [""])[0]
    if not raw:
        return None
    path = Path(raw).expanduser()
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        if base_dir:
            candidates.append(Path(base_dir) / path)
        if allowed_root:
            candidates.append(Path(allowed_root) / path)
        if not candidates:
            candidates.append(Path.cwd() / path)
    root = Path(allowed_root).resolve() if allowed_root else None
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if root is not None:
            try:
                if not resolved.is_relative_to(root):
                    continue
            except OSError:
                continue
        if resolved.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
            continue
        if resolved.is_file():
            return resolved
    return None


def _build_docx(markdown, title="", base_dir=None, allowed_root=None):
    """把简单 Markdown（标题/正文/列表/表格/代码块/引用/图片）转成 python-docx 文档。"""
    from docx import Document
    from docx.shared import Pt

    document = Document()
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.2)
    section.left_margin = section.right_margin = Cm(2.3)
    for name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3", "Heading 4", "List Bullet", "List Number"):
        style = document.styles[name]
        style.font.name = "Calibri"
        style.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "Microsoft YaHei")
        style.paragraph_format.space_after = Pt(7)
        if name.startswith("Heading"):
            style.paragraph_format.keep_with_next = True
    document.styles["Normal"].font.size = Pt(11)
    from docx.shared import RGBColor

    title_style = document.styles["Title"]
    title_style.font.color.rgb = RGBColor(0, 0, 0)
    title_style.font.underline = False
    for border in title_style.element.xpath("./w:pPr/w:pBdr"):
        border.getparent().remove(border)
    document.styles["Caption"].font.size = Pt(9)
    document.styles["Caption"].font.color.rgb = RGBColor.from_string("475569")
    document.styles["Normal"].paragraph_format.line_spacing = 1.25
    footer = section.footer.paragraphs[0]
    footer.alignment = 2
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    counts = {"headings": 0, "paragraphs": 0, "bullets": 0, "tables": 0, "code_blocks": 0, "images": 0, "charts": 0}
    if title and str(title).strip():
        document.add_heading(str(title).strip()[:200], level=0)
    lines = str(markdown).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line.startswith("```"):
            inline_images = list(re.finditer(r"!\[[^\]]*\]\((?:<[^>]+>|[^)]+)\)", line))
            if inline_images and not (len(inline_images) == 1 and inline_images[0].group() == line):
                fragments, previous = [], 0
                for match in inline_images:
                    fragments.extend([line[previous : match.start()], match.group()])
                    previous = match.end()
                fragments.append(line[previous:])
                lines[index : index + 1] = [fragment for fragment in fragments if fragment.strip()]
                continue
        image = re.fullmatch(r"!\[([^\]]*)\]\((.+?)\)", line)
        if image:
            src = re.sub(r'\s+"[^"]*"$', "", image.group(2)).strip().removeprefix("<").removesuffix(">")
            path = _resolve_md_image(src, base_dir, allowed_root)
            if path is not None:
                try:
                    from PIL import Image

                    with Image.open(path) as image_size:
                        image_width, image_height = image_size.size
                    picture = document.add_picture(str(path), width=Cm(min(16.4, 19 * image_width / image_height)))
                    picture._inline.docPr.set("descr", image.group(1))
                    document.paragraphs[-1].paragraph_format.keep_with_next = bool(image.group(1).strip())
                    counts["images"] += 1
                    if image.group(1).strip():
                        caption = document.add_paragraph(image.group(1).strip()[:200])
                        caption.paragraph_format.keep_with_next = False
                        for run in caption.runs:
                            run.italic = True
                            run.font.size = Pt(9)
                except (OSError, ValueError) as exc:
                    raise WorkflowError(f"图片无法插入：{image.group(1)}") from exc
            else:
                raise WorkflowError(f"图片未找到或不在项目目录：{image.group(1)}")
            index += 1
            continue
        if line.startswith("```"):
            language = line[3:].strip().lower()
            index += 1
            code = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            if language == "chart" and index >= len(lines):
                raise WorkflowError("chart 代码块缺少结束围栏，请补齐后重新导出")
            index += 1
            if language == "chart":
                import json

                from ..local.document_charts import chart_caption, chart_png, normalize_chart

                try:
                    chart = normalize_chart(json.loads("\n".join(code)))
                except ValueError as exc:
                    raise WorkflowError("chart 代码块须为有效 JSON") from exc
                figure = document.add_picture(chart_png({**chart, "title": "", "source": ""}), width=Cm(16.4))
                figure._inline.docPr.set("descr", chart_caption(chart))
                document.paragraphs[-1].paragraph_format.keep_with_next = bool(chart_caption(chart))
                caption = document.add_paragraph(chart_caption(chart), style="Caption")
                caption.paragraph_format.keep_with_next = False
                counts["charts"] += 1
                counts["images"] += 1
                continue
            paragraph = document.add_paragraph()
            run = paragraph.add_run("\n".join(code))
            run.font.name = "Consolas"
            run.font.size = Pt(9)
            counts["code_blocks"] += 1
            continue
        if not line:
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            document.add_heading(_md_inline(heading.group(2)), level=min(len(heading.group(1)), 4))
            counts["headings"] += 1
            index += 1
            continue
        if line.startswith("|") and index + 1 < len(lines) and _is_table_separator(lines[index + 1]):
            header = _table_cells(line)
            index += 2
            rows = []
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                rows.append(_table_cells(lines[index]))
                index += 1
            cols = max(1, len(header))
            table = document.add_table(rows=1, cols=cols)
            table.style = "Table Grid"
            repeat = OxmlElement("w:tblHeader")
            table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for col, value in enumerate(header[:cols]):
                cell = table.rows[0].cells[col]
                cell.text = ""
                cell.paragraphs[0].add_run(value).bold = True
            for row in rows:
                cells = table.add_row().cells
                for col, value in enumerate(row[:cols]):
                    cells[col].text = value
            counts["tables"] += 1
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", line)
        if bullet:
            document.add_paragraph(_md_inline(bullet.group(1)), style="List Bullet")
            counts["bullets"] += 1
            index += 1
            continue
        numbered = re.match(r"^\d+[.)]\s+(.*)$", line)
        if numbered:
            document.add_paragraph(_md_inline(numbered.group(1)), style="List Number")
            counts["bullets"] += 1
            index += 1
            continue
        if line.startswith(">"):
            document.add_paragraph(_md_inline(line.lstrip(">").strip()), style="Quote")
            counts["paragraphs"] += 1
            index += 1
            continue
        paragraph = document.add_paragraph()
        for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)", line):
            if part.startswith("**") and part.endswith("**"):
                paragraph.add_run(part[2:-2]).bold = True
            elif part.startswith("`") and part.endswith("`"):
                run = paragraph.add_run(part[1:-1])
                run.font.name = "Consolas"
            elif part.startswith("*") and part.endswith("*"):
                paragraph.add_run(part[1:-1]).italic = True
            else:
                paragraph.add_run(_md_inline(part) if "[" in part else part)
        counts["paragraphs"] += 1
        index += 1
    return document, counts


def _normalize_slides(slides):
    if not isinstance(slides, list) or not 1 <= len(slides) <= 80:
        raise WorkflowError("slides须为1—80页的数组，每页为{title,bullets,notes,image?,layout?}")
    result = []
    for position, item in enumerate(slides):
        if not isinstance(item, dict):
            raise WorkflowError(f"第{position + 1}页须为对象{{title,bullets,notes}}")
        title = item.get("title") or ""
        bullets = item.get("bullets") or []
        notes = item.get("notes") or ""
        if not isinstance(title, str):
            raise WorkflowError(f"第{position + 1}页的title须为字符串")
        if not isinstance(bullets, list) or any(isinstance(b, (dict, list)) for b in bullets):
            raise WorkflowError(f"第{position + 1}页的bullets须为字符串数组")
        if not isinstance(notes, str):
            raise WorkflowError(f"第{position + 1}页的notes须为字符串")
        image = item.get("image") or ""
        chart = item.get("chart")
        if chart is not None:
            from ..local.document_charts import normalize_chart

            chart = normalize_chart(chart)
            if image:
                raise WorkflowError(f"第{position + 1}页不能同时使用 image 和 chart，请拆页")
        if image and not isinstance(image, str):
            raise WorkflowError(f"第{position + 1}页的image须为路径字符串")
        layout = str(item.get("layout") or ("chart" if chart else "image" if image else "bullets")).lower()
        if (chart or image) and layout in ("section", "closing"):
            raise WorkflowError(f"第{position + 1}页含图表或图片，不能使用章节或结束页布局")
        if chart:
            layout = "chart"
        elif image:
            layout = "image"
        if layout == "chart" and chart is None:
            raise WorkflowError(f"第{position + 1}页缺少 chart 数据")
        if layout == "image" and not image:
            raise WorkflowError(f"第{position + 1}页缺少 image 路径")
        if layout not in ("bullets", "image", "chart", "section", "closing"):
            layout = "bullets"
        if len(bullets) > 12 or any(len(str(bullet)) > 1000 for bullet in bullets):
            raise WorkflowError(f"第{position + 1}页内容过长：最多12条要点，每条最多1000字，请分批拆页")
        result.append(
            {
                "title": title[:200],
                "bullets": [str(bullet)[:1000] for bullet in bullets][:12],
                "notes": notes[:4000],
                "image": image[:1000],
                "chart": chart,
                "layout": layout,
                "kicker": str(item.get("kicker") or "")[:120],
            }
        )
    paginated = []
    for slide in result:
        if slide["layout"] not in ("bullets", "image", "chart"):
            paginated.append(slide)
            continue
        chunk_size = 70 if slide["layout"] in ("image", "chart") else 140
        chunks = [bullet[i : i + chunk_size] for bullet in slide["bullets"] for i in range(0, len(bullet), chunk_size)]
        per_page = 2 if slide["layout"] in ("image", "chart") else 4
        for offset in range(0, max(1, len(chunks)), per_page):
            paginated.append(
                {
                    **slide,
                    "title": slide["title"] + ("（续）" if offset else ""),
                    "bullets": chunks[offset : offset + per_page],
                }
            )
    if len(paginated) > 80:
        raise WorkflowError("内容拆页后超过80页，请分批导出")
    return paginated


def _pptx_font(size, bold=False, color=None):
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    return {"size": Pt(size), "bold": bool(bold), "color": RGBColor.from_string(color or "0F172A")}


def _pptx_text(
    slide,
    text,
    left,
    top,
    width,
    height,
    *,
    size=18,
    bold=False,
    color="0F172A",
    align=None,
    font="Microsoft YaHei",
    anchor=None,
    line_spacing=1.15,
):
    import math
    import unicodedata

    from pptx.dml.color import RGBColor
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Pt

    # Conservative CJK-aware estimate. The actual rendered pages still require review.
    while size > 12:
        lines = sum(
            max(
                1,
                math.ceil(
                    sum(1 if unicodedata.east_asian_width(c) in "WF" else 0.55 for c in line) * size / (width / 12700)
                ),
            )
            for line in str(text or "").split("\n")
        )
        if lines * size * line_spacing <= height / 12700:
            break
        size -= 1

    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    frame.vertical_anchor = anchor or MSO_ANCHOR.TOP
    paragraph = frame.paragraphs[0]
    paragraph.text = str(text or "")
    paragraph.alignment = align or PP_ALIGN.LEFT
    paragraph.line_spacing = line_spacing
    for run in paragraph.runs:
        run.font.name = font
        run.font.size = Pt(size)
        run.font.bold = bool(bold)
        run.font.color.rgb = RGBColor.from_string(color)
    return box


def _pptx_bg(slide, color):
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, Inches(13.333), Inches(7.5))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color)
    shape.line.fill.background()
    slide.shapes._spTree.remove(shape._element)
    slide.shapes._spTree.insert(2, shape._element)
    return shape


def _pptx_rule(slide, left, top, width, color="16A34A", height=6):
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, Emu(int(height * 9525)))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color)
    shape.line.fill.background()
    return shape


def _pptx_image(slide, path, left, top, max_w, max_h):
    from PIL import Image
    from pptx.util import Emu

    try:
        with Image.open(path) as probe:
            width, height = probe.size
    except OSError:
        return False
    if not width or not height:
        return False
    ratio = min(max_w / width, max_h / height)
    w, h = int(width * ratio), int(height * ratio)
    slide.shapes.add_picture(str(path), left + (max_w - w) // 2, top + (max_h - h) // 2, width=Emu(w), height=Emu(h))
    return True


def _build_pptx(slides, title="", base_dir=None, allowed_root=None, include_cover=None):
    """生成 16:9 汇报 PPT：深色封面/章节页、浅色内容页、图表页与页脚，尽量不依赖模板。"""
    from datetime import date

    from pptx import Presentation
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    blank = presentation.slide_layouts[6]
    dark, light, ink, muted, accent = "0F172A", "F8FAFC", "0F172A", "64748B", "16A34A"
    title_text = str(title or "").strip()[:200]

    # Only an explicit title (or host policy) adds an extra cover.
    include_cover = bool(title_text) if include_cover is None else include_cover
    cover = presentation.slides.add_slide(blank) if include_cover else None
    if cover is not None:
        _pptx_bg(cover, dark)
        _pptx_rule(cover, Inches(0.9), Inches(1.35), Inches(0.9), accent, 8)
        _pptx_text(
            cover,
            title_text or "研究报告",
            Inches(0.9),
            Inches(1.65),
            Inches(11.4),
            Inches(2.0),
            size=40,
            bold=True,
            color="FFFFFF",
        )
        _pptx_text(
            cover,
            "QuantLab Bot · 本地 Agent 生成",
            Inches(0.94),
            Inches(3.9),
            Inches(8),
            Inches(0.5),
            size=18,
            color="CBD5E1",
        )
        _pptx_text(
            cover, date.today().isoformat(), Inches(0.94), Inches(6.4), Inches(4), Inches(0.4), size=12, color="94A3B8"
        )

    for index, item in enumerate(slides, 1):
        layout = item.get("layout") or "bullets"
        slide = presentation.slides.add_slide(blank)
        if layout == "section":
            _pptx_bg(slide, dark)
            _pptx_text(
                slide,
                f"{index:02d}",
                Inches(0.9),
                Inches(1.1),
                Inches(1.2),
                Inches(0.7),
                size=24,
                bold=True,
                color=accent,
            )
            _pptx_text(
                slide,
                item["title"],
                Inches(0.9),
                Inches(2.1),
                Inches(11.4),
                Inches(2.4),
                size=36,
                bold=True,
                color="FFFFFF",
            )
            if item["bullets"]:
                _pptx_text(
                    slide,
                    " · ".join(item["bullets"]),
                    Inches(0.94),
                    Inches(4.8),
                    Inches(11.2),
                    Inches(1.2),
                    size=16,
                    color="CBD5E1",
                )
        elif layout == "closing":
            _pptx_bg(slide, dark)
            _pptx_text(
                slide,
                item["title"] or "谢谢",
                Inches(0.9),
                Inches(2.5),
                Inches(11.4),
                Inches(1.4),
                size=40,
                bold=True,
                color="FFFFFF",
            )
            _pptx_text(
                slide,
                " · ".join(item["bullets"]) or "欢迎提问与指正",
                Inches(0.94),
                Inches(4.2),
                Inches(11),
                Inches(1.2),
                size=18,
                color="CBD5E1",
            )
        else:
            _pptx_bg(slide, light)
            _pptx_rule(slide, 0, 0, Inches(13.333), accent, 8)
            _pptx_text(
                slide,
                item.get("kicker") or f"{index:02d} / {len(slides):02d}",
                Inches(0.9),
                Inches(0.42),
                Inches(8),
                Inches(0.35),
                size=12,
                bold=True,
                color=accent,
            )
            _pptx_text(
                slide,
                item["title"],
                Inches(0.9),
                Inches(0.78),
                Inches(11.6),
                Inches(0.9),
                size=28,
                bold=True,
                color=ink,
            )
            if layout == "chart" and item.get("chart"):
                from ..local.document_charts import add_pptx_chart, chart_caption

                add_pptx_chart(slide, item["chart"], Inches(0.9), Inches(1.85), Inches(11.5), Inches(4.15))
                caption = "\n".join(filter(None, [" · ".join(item["bullets"]), chart_caption(item["chart"])]))
                _pptx_text(slide, caption, Inches(0.9), Inches(6.15), Inches(11.5), Inches(0.75), size=12, color=muted)
            elif layout == "image" and item.get("image"):
                image_path = _resolve_md_image(item["image"], base_dir, allowed_root)
                if image_path is None:
                    raise WorkflowError(f"图片未找到或不在项目目录：{item['image']}")
                _pptx_image(
                    slide, image_path, Inches(0.9), Inches(1.85), Inches(11.5), Inches(4.3)
                ) if image_path else None
                if item["bullets"]:
                    _pptx_text(
                        slide,
                        " · ".join(item["bullets"]),
                        Inches(0.9),
                        Inches(6.2),
                        Inches(11.5),
                        Inches(0.75),
                        size=12,
                        color=muted,
                    )
            else:
                bullet_text = "\n".join(f"•  {b}" for b in item["bullets"]) or "（本页无要点）"
                _pptx_text(
                    slide,
                    bullet_text,
                    Inches(1.0),
                    Inches(2.0),
                    Inches(11.2),
                    Inches(4.4),
                    size=20,
                    color="1E293B",
                    line_spacing=1.35,
                )
            _pptx_text(
                slide,
                "QuantLab",
                Inches(11.1),
                Inches(7.05),
                Inches(1.2),
                Inches(0.25),
                size=9,
                color="94A3B8",
                align=PP_ALIGN.RIGHT,
            )
            _pptx_text(
                slide,
                str(index),
                Inches(12.55),
                Inches(7.05),
                Inches(0.4),
                Inches(0.25),
                size=9,
                color="94A3B8",
                align=PP_ALIGN.RIGHT,
            )
        if item["notes"].strip():
            slide.notes_slide.notes_text_frame.text = item["notes"]
    return presentation


def local_chart(kind: str, title: str, labels: list, values: list, path: str, width: int = 1200, height: int = 675):
    """用 Pillow 画一张 PNG 图表，返回可在对话/Word/PPT 中引用的本地路径与 URL。

    不依赖 matplotlib：Pillow 是桌面运行时已有的轻量依赖，适合 Agent 在没有完整
    Python 科学计算环境时也能产出可交付图表。支持 bar / line / pie。
    """
    from urllib.parse import quote

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise WorkflowError(_missing_library("Pillow", "图表")) from exc

    _s, profile = _local_profile()
    chart_type = str(kind or "bar").strip().lower()
    if chart_type not in ("bar", "line", "pie"):
        raise WorkflowError("kind 只支持 bar / line / pie")
    if not isinstance(labels, list) or not isinstance(values, list) or len(labels) != len(values):
        raise WorkflowError("labels 与 values 必须是等长数组")
    if not 2 <= len(labels) <= 40:
        raise WorkflowError("图表数据点须为 2—40 个")
    clean: list[tuple[str, float]] = []
    for label, value in zip(labels, values, strict=True):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("values 必须都是数字") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise WorkflowError("values 不能包含 NaN/Inf")
        clean.append((str(label)[:40], number))
    w = max(480, min(int(width or 1200), 2400))
    h = max(270, min(int(height or 675), 1350))
    target = _export_target(profile, path, ".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (w, h), "#FFFFFF")
    draw = ImageDraw.Draw(image)

    def font(size: int, bold: bool = False):
        names = ["msyhbd.ttc" if bold else "msyh.ttc", "simhei.ttf", "arial.ttf"]
        for name in names:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    title_font, label_font, value_font = font(max(22, w // 32), True), font(max(14, w // 70)), font(max(13, w // 78))
    draw.text((w // 24, h // 30), str(title or "图表")[:80], fill="#0F172A", font=title_font)
    margin_l, margin_r, margin_t, margin_b = w // 12, w // 18, h // 4, h // 7
    plot_w, plot_h = w - margin_l - margin_r, h - margin_t - margin_b
    palette = ["#16A34A", "#0EA5E9", "#F59E0B", "#EF4444", "#8B5CF6", "#14B8A6", "#F97316", "#64748B"]
    if chart_type == "pie":
        total = sum(abs(v) for _, v in clean) or 1.0
        start = -90.0
        cx, cy = w // 2, margin_t + plot_h // 2
        radius = max(40, min(plot_w, plot_h) // 2 - 10)
        for i, (_label, value) in enumerate(clean):
            extent = 360.0 * abs(value) / total
            draw.pieslice(
                (cx - radius, cy - radius, cx + radius, cy + radius),
                start,
                start + extent,
                fill=palette[i % len(palette)],
                outline="#FFFFFF",
            )
            start += extent
        legend_x, legend_y = margin_l, margin_t + plot_h + 10
        for i, (label, value) in enumerate(clean[:8]):
            y = legend_y + (i % 4) * 20
            x = legend_x + (i // 4) * (plot_w // 2)
            draw.rectangle((x, y + 3, x + 12, y + 15), fill=palette[i % len(palette)])
            draw.text((x + 18, y), f"{label} {value:g}", fill="#334155", font=label_font)
    else:
        lo = min(0.0, min(v for _, v in clean))
        hi = max(0.0, max(v for _, v in clean)) or 1.0
        span = hi - lo or 1.0
        # 只画折线没有纵轴数值时无法读出量级；柱图与折线共用清晰的数值刻度。
        for tick in range(6):
            value = lo + span * tick / 5
            y = margin_t + plot_h - tick * plot_h / 5
            label = f"{value:.4g}"
            box = draw.textbbox((0, 0), label, font=value_font)
            draw.line((margin_l, y, w - margin_r, y), fill="#F1F5F9", width=1)
            draw.text((margin_l - (box[2] - box[0]) - 10, y - 8), label, fill="#475569", font=value_font)
        zero_y = margin_t + plot_h - (0 - lo) / span * plot_h
        draw.line((margin_l, zero_y, w - margin_r, zero_y), fill="#CBD5E1", width=1)
        points: list[tuple[float, float]] = []
        for i, (label, value) in enumerate(clean):
            x = margin_l + (i + 0.5) * plot_w / len(clean)
            y = margin_t + plot_h - (value - lo) / span * plot_h
            if chart_type == "bar":
                bar_w = max(8, plot_w / len(clean) * 0.55)
                draw.rectangle(
                    (x - bar_w / 2, min(y, zero_y), x + bar_w / 2, max(y, zero_y)), fill=palette[i % len(palette)]
                )
                draw.text((x - 12, min(y, zero_y) - 18), f"{value:g}", fill="#0F172A", font=value_font)
            else:
                points.append((x, y))
            if len(clean) <= 12 or i % max(1, len(clean) // 10) == 0:
                draw.text((x - len(label) * 3, h - margin_b + 18), label, fill="#475569", font=label_font)
        if chart_type == "line":
            draw.line(points, fill=palette[0], width=max(3, w // 300), joint="curve")
            for x, y in points:
                draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#FFFFFF", outline=palette[0], width=2)
        draw.line((margin_l, margin_t, margin_l, margin_t + plot_h), fill="#94A3B8", width=1)
    try:
        image.save(str(target), format="PNG", optimize=True)
    except OSError as exc:
        raise WorkflowError(f"保存图表失败：{exc}") from exc
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "format": "png",
        "chart": chart_type,
        "points": len(clean),
        "url": "/api/local/files/image?path=" + quote(str(target)),
    }


def local_preview_document(path: str, start: int = 1, limit: int = 4):
    """Render actual Office/PDF pages; images are sent to the model for layout inspection."""
    from ..local.documents import render_document

    _s, profile = _local_profile()
    return render_document(profile, path, start, limit)


def local_export_docx(markdown: str, path: str, title: str = "", base_dir: str = ""):
    """把简单 Markdown 写成 .docx；python-docx 缺失时返回可执行的安装提示。"""
    _s, profile = _local_profile()
    if not isinstance(markdown, str) or not markdown.strip():
        raise WorkflowError("markdown须为非空字符串")
    target = _export_target(profile, path, ".docx")
    existed = target.exists()
    try:
        import docx  # noqa: F401  只做依赖检查，真正的构建在 _build_docx
    except ImportError as exc:
        raise WorkflowError(_missing_library("python-docx", "Word 文档")) from exc
    try:
        document, counts = _build_docx(
            markdown, title, base_dir=(Path(base_dir) if base_dir else target.parent), allowed_root=profile.root
        )
        tmp = _temp_beside(target)
        try:
            document.save(str(tmp))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError) as exc:
        raise WorkflowError(f"保存Word文档失败：{exc}") from exc
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "format": "docx",
        "title": str(title or ""),
        "overwritten": existed,
        "library": "python-docx",
        **counts,
    }


def local_export_pptx(
    slides: list[dict],
    path: str,
    title: str = "",
    base_dir: str = "",
    include_cover: bool | None = None,
    expected_slides: int | None = None,
):
    """把 [{title,bullets,notes}] 写成 16:9 .pptx；python-pptx 缺失时明确提示。"""
    _s, profile = _local_profile()
    normalized = _normalize_slides(slides)
    if include_cover is not None and not isinstance(include_cover, bool):
        raise WorkflowError("include_cover 须为布尔值")
    cover = bool(str(title or "").strip()) if include_cover is None else include_cover
    physical = len(normalized) + int(cover)
    if expected_slides is not None:
        if isinstance(expected_slides, bool) or not isinstance(expected_slides, int) or not 1 <= expected_slides <= 81:
            raise WorkflowError("expected_slides 须为1—81的整数")
        if physical != expected_slides:
            raise WorkflowError(
                f"实际共{physical}页（含封面和拆页），与要求的{expected_slides}页不符；请调整内容或 include_cover"
            )
    target = _export_target(profile, path, ".pptx")
    existed = target.exists()
    try:
        import pptx  # noqa: F401  只做依赖检查，真正的构建在 _build_pptx
    except ImportError as exc:
        raise WorkflowError(_missing_library("python-pptx", "PPT 汇报")) from exc
    try:
        presentation = _build_pptx(
            normalized,
            title,
            base_dir=(Path(base_dir) if base_dir else target.parent),
            allowed_root=profile.root,
            include_cover=cover,
        )
        tmp = _temp_beside(target)
        try:
            presentation.save(str(tmp))
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError) as exc:
        raise WorkflowError(f"保存PPT失败：{exc}") from exc
    return {
        "path": str(target),
        "bytes": target.stat().st_size,
        "format": "pptx",
        "title": str(title or ""),
        "slides": len(presentation.slides),
        "content_slides": len(normalized),
        "charts": sum(bool(item.get("chart")) for item in normalized),
        "images": sum(bool(item.get("image")) for item in normalized),
        "aspect": "16:9",
        "overwritten": existed,
        "library": "python-pptx",
    }


def local_append_code(path: str, source: str, heading: str = "", language: str = "python"):
    """把已授权目录内的一份源码文件作为代码块追加到目标文档（避免模型重写长代码）。"""
    _s, profile = _local_profile()
    src = _authorized_target(profile, source, write=False)
    try:
        code = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"读取源码失败：{exc}") from exc
    if len(code) > 120_000:
        raise WorkflowError("源码超过120KB；请只选择需要的文件或函数")
    target = _authorized_target(profile, path, write=True)
    if target.exists() and target.is_dir():
        raise WorkflowError("目标路径是目录，不能追加")
    lang = re.sub(r"[^A-Za-z0-9_+-]", "", str(language or ""))[:20] or "text"
    title = str(heading or "").strip()[:120]
    block = (f"\n\n## {title}\n\n" if title else "\n\n") + f"```{lang}\n{code.rstrip()}\n```\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(block)
    return {
        "path": str(target),
        "source": str(src),
        "bytes": target.stat().st_size,
        "appended_chars": len(block),
        "language": lang,
        "heading": title,
    }


def local_export_docx_file(markdown_path: str, path: str, title: str = ""):
    """把已授权目录内的 Markdown 文件导出为 .docx（适合分段写长报告后再导出）。"""
    _s, profile = _local_profile()
    source = _authorized_target(profile, markdown_path, write=False)
    try:
        markdown = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowError(f"读取 Markdown 失败：{exc}") from exc
    if not markdown.strip():
        raise WorkflowError("Markdown 文件为空")
    return local_export_docx(markdown, path, title, base_dir=str(source.parent))


def local_export_pptx_file(
    slides_path: str, path: str, title: str = "", include_cover: bool | None = None, expected_slides: int | None = None
):
    """把已授权目录内的 JSON 幻灯片文件（[{title,bullets,notes}]）导出为 .pptx。"""
    import json

    _s, profile = _local_profile()
    source = _authorized_target(profile, slides_path, write=False)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkflowError(f"读取幻灯片 JSON 失败：{exc}") from exc
    slides = raw.get("slides") if isinstance(raw, dict) else raw
    if not isinstance(slides, list):
        raise WorkflowError("幻灯片 JSON 须为数组，或含 slides 数组的对象")
    if isinstance(raw, dict):
        title = title or raw.get("title", "")
        include_cover = raw.get("include_cover") if include_cover is None else include_cover
        expected_slides = raw.get("expected_slides") if expected_slides is None else expected_slides
    return local_export_pptx(
        slides, path, title, base_dir=str(source.parent), include_cover=include_cover, expected_slides=expected_slides
    )


def child_evidence(child_run_id: str, node_id: str = ""):
    from ..platform.context import current_workspace

    s = service()
    parent = s.find_parent(child_run_id)
    run = s.services.runs.require(parent, current_workspace())
    return inspect(run["manifest"]["workflow_id"], parent, node_id, child_run_id)


def experiment_plan(workflow_id: str, plan: dict):
    from .experiments import variants

    s = service()
    return {"variants": variants(s.get(workflow_id)["workflow"], s.registry(), plan)}


def experiment_submit(workflow_id: str, plan: dict, expected_revision: int):
    from .experiments import submit

    return submit(service(), workflow_id, plan, expected_revision, origin="agent")


def compare_runs(run_ids: list[str]):
    from .experiments import comparison

    return comparison(service(), run_ids)


def tool_specs():
    return [
        (
            "workflow_notes",
            research_notes,
            "读取或保存研究笔记，run_ids关联真实运行；写入需提供text和expected_version",
            {
                "workflow_id": {"type": "string"},
                "text": {"type": "string"},
                "run_ids": {"type": "array", "items": {"type": "string"}},
                "expected_version": {"type": "integer"},
            },
        ),
        ("workflow_plugins", plugin_catalog, "查询插件能力、依赖和当前Agent可调用状态", {}),
        (
            "workflow_artifacts",
            artifacts,
            "检索研究图表、数据、报告等产物，按研究或运行筛选",
            {
                "workflow_id": {"type": "string"},
                "run_id": {"type": "string"},
                "query": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        ),
        ("workflow_cancel", cancel, "取消工作流任务", {"run_id": {"type": "string"}}),
        (
            "workflow_export",
            export,
            "导出当前研究或冻结运行的独立包，返回任务与下载入口",
            {"workflow_id": {"type": "string"}, "run_id": {"type": "string"}, "include_wheels": {"type": "boolean"}},
        ),
        (
            "workflow_propose",
            propose,
            "提出工作流改动并显示差异，不写入版本；研究者可在右侧审阅后保存",
            {
                "workflow": {"type": "object"},
                "workflow_id": {"type": "string"},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
        ),
        (
            "workflow_child_evidence",
            child_evidence,
            "凭子运行ID查找所属父运行并读取内部证据，无需猜父ID",
            {"child_run_id": {"type": "string"}, "node_id": {"type": "string"}},
        ),
        (
            "workflow_import_data",
            import_data,
            "将显式表格或CSV文本导入工作区，无需已有文件；name必须以.csv结尾；data/csv_text二选一，不覆盖已有文件",
            {
                "name": {
                    "type": "string",
                    "pattern": "\\.[cC][sS][vV]$",
                    "description": "新的CSV文件名，例如 demo_prices.csv",
                },
                "data": {"type": "array", "items": {"type": "object"}, "maxItems": 5000},
                "csv_text": {"type": "string", "maxLength": 2000000},
            },
        ),
        (
            "workflow_local_files",
            local_files,
            "发现、预览、引用/复制用户已授权本地目录的文件；preview 对 .md/.txt/.json/.py 等文本按文本分页读取"
            "（与 local_read_text 一致），对 CSV/TSV/XLSX/Parquet/JSONL 按表格预览；二进制/办公文件明确报错；"
            "相对路径按本地项目根解析；不授予新目录权限",
            {
                "path": {"type": "string"},
                "action": {"type": "string", "enum": ["browse", "preview", "import"]},
                "mode": {"type": "string", "enum": ["reference", "copy"]},
                "name": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
        ),
        (
            "workflow_catalog",
            catalog,
            "分页浏览工作流和卡片目录，含revision与next_cursor",
            {
                "section": {"type": "string", "enum": ["cards", "workflows"]},
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "query": {"type": "string"},
            },
        ),
        (
            "workflow_help",
            help_workflow,
            "获取工作流/组件图/工件的准确规范与最小示例",
            {"topic": {"type": "string", "enum": ["workflow", "network", "artifacts", "experiments", "evidence"]}},
        ),
        ("workflow_create", create, "创建首版工作流，无需已有workflow_id", {"workflow": {"type": "object"}}),
        ("workflow_files", list_files, "查询数据源可引用的工作区导入文件名", {}),
        (
            "workflow_artifact",
            read_artifact,
            "分页读取运行输出工件，使用outputs端口的hash而非资产sha256",
            {
                "artifact_hash": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        ),
        (
            "workflow_sources",
            inspect_sources,
            "查看函数契约与实现包源码",
            {
                "kind": {"type": "string"},
                "version": {"type": "string"},
                "file_path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 3000},
            },
        ),
        (
            "workflow_definition_preview",
            preview_definition,
            "检查自定义函数语法与接口变化",
            {"code": {"type": "string"}, "old_kind": {"type": "string"}, "old_version": {"type": "string"}},
        ),
        (
            "workflow_definition_publish",
            publish_definition,
            "发布不可覆盖的函数版本并记录Agent来源",
            {"code": {"type": "string"}},
        ),
        (
            "workflow_network_inspect",
            inspect_network,
            "检查神经组件图的形状和参数量；规范见workflow_help(topic='network')",
            {
                "graph": {
                    "type": "object",
                    "required": ["nodes", "output"],
                    "description": "示例nodes:[{id:'in',type:'Input',shape:[8]},{id:'out',type:'Linear',inputs:['in'],out_features:1}],output:'out'。Input不需要inputs，shape不含batch。",
                    "properties": {
                        "nodes": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 80,
                            "items": {"type": "object", "required": ["id", "type"]},
                        },
                        "output": {"type": "string"},
                        "edges": {"type": "array", "items": {"type": "object"}},
                    },
                }
            },
        ),
        (
            "workflow_chart_preview",
            preview_chart,
            "生成可在对话中显示、放大和下载的matplotlib图表；artifact_hash或不超过5000行data二选一",
            {
                "artifact_hash": {
                    "type": "string",
                    "description": "运行报告nodes[节点].outputs[端口].hash，不是资产sha256",
                },
                "config": {"type": "object"},
                "data": {"type": "array", "items": {"type": "object"}, "maxItems": 5000},
            },
        ),
        ("workflow_assets", list_assets, "浏览本工作区已有研究资产", {}),
        (
            "workflow_asset_snapshot",
            snapshot_asset,
            "从已有资产创建定义快照模块",
            {"domain": {"type": "string"}, "asset_id": {"type": "string"}},
        ),
        (
            "workflow_experiment_plan",
            experiment_plan,
            "预览参数网格或明确的滚动时间边界",
            {"workflow_id": {"type": "string"}, "plan": experiment_schema()},
        ),
        (
            "workflow_experiment_submit",
            experiment_submit,
            "按指定修订提交独立实验并记录Agent来源",
            {"workflow_id": {"type": "string"}, "plan": experiment_schema(), "expected_revision": {"type": "integer"}},
        ),
        (
            "workflow_compare",
            compare_runs,
            "比较实验指标并保留失败结果",
            {"run_ids": {"type": "array", "items": {"type": "string"}}},
        ),
        (
            "workflow_inspect",
            inspect,
            "查看工作流与节点运行证据",
            {
                "workflow_id": {"type": "string"},
                "run_id": {"type": "string"},
                "node_id": {"type": "string"},
                "child_run_id": {"type": "string"},
                "revision": {"type": "integer"},
            },
        ),
        (
            "workflow_validate",
            validate,
            "检查工作流连接、参数、依赖与数据",
            {"workflow_id": {"type": "string"}, "target": {"type": "string"}},
        ),
        (
            "workflow_run",
            run,
            "运行已保存的工作流或目标分支",
            {
                "workflow_id": {"type": "string"},
                "target": {"type": "string"},
                "mode": {"type": "string", "enum": ["research", "debug"]},
                "source_run_id": {"type": "string"},
                "revision": {"type": "integer"},
                "seed": {"type": "integer"},
            },
        ),
        (
            "workflow_revise",
            revise,
            "创建或保存工作流；首版workflow_id省略且expected_revision=0，更新填当前revision",
            {
                "workflow_id": {"type": "string"},
                "expected_revision": {"type": "integer"},
                "workflow": {"type": "object"},
            },
        ),
    ]


def register(tools):
    for name, handler, title, props in tool_specs():
        required = required_args(handler)
        tools.register(
            name,
            guarded(handler, name),
            label=title,
            description=title + "；与GUI使用同一工作区、函数版本和运行记录。",
            parameters={"type": "object", "properties": props, "required": required},
        )


def platform_tools(cls):
    tools = [
        cls(
            name=name,
            title=title,
            scene="workflow",
            description=title,
            when="研究工作流编辑、核验或运行时",
            risk="run"
            if name
            in (
                "workflow_run",
                "workflow_revise",
                "workflow_create",
                "workflow_asset_snapshot",
                "workflow_experiment_submit",
                "workflow_definition_publish",
                "workflow_import_data",
                "workflow_local_files",
                "workflow_chart_preview",
                "workflow_cancel",
                "workflow_export",
                "workflow_notes",
            )
            else "read",
            params=props,
            handler=guarded(handler, name),
            required=required_args(handler),
            category="edit" if name in ("workflow_revise", "workflow_create") else "",
        )
        for name, handler, title, props in tool_specs()
    ]
    tools.extend(local_deliverable_tools(cls))
    return tools


#: 本地交付物工具：risk=confirm（团队/角色 Agent 不会拿到），写入走 Lab 审批类别「save」。
LOCAL_DELIVERABLE_SPECS = [
    {
        "name": "local_preview_document",
        "risk": "read",
        "category": "",
        "handler": local_preview_document,
        "title": "查看文档页面",
        "description": "渲染授权目录内 Word/PPT/PDF 的真实页面截图，并把图片送入模型视觉上下文。不读取平台数据目录与密钥目录。检查截断、重叠、空白页、图表与字体；修正后重新渲染，不得只凭导出成功宣称排版通过。返回页面 URL 可在对话展示。需要 LibreOffice（Office 转 PDF）与 PyMuPDF。",
        "when": "交付 Word/PPT 前逐批查看页面并检查排版；每次最多8页",
        "params": {
            "path": {"type": "string", "description": "授权目录中的 DOCX/PPTX/PDF 路径"},
            "start": {"type": "integer", "description": "起始页，从1开始"},
            "limit": {"type": "integer", "description": "页数，默认4，最多8"},
        },
        "summary": lambda args: f"查看文档页面 {args.get('path', '')}",
    },
    {
        "name": "local_write_text",
        "handler": local_write_text,
        "title": "写本地文本文件",
        "description": (
            "在用户已授权的可写目录（默认当前本地项目目录）写入 UTF-8 文本并自动创建父目录；单文件上限2MB；"
            "长报告可先 append=true 分段追加，再用 local_export_docx_file 导出；"
            "只写授权目录，不读写平台数据目录与密钥目录；不能执行 shell / Python"
        ),
        "when": "需要把 Markdown、报告、作业文本或脚本保存到本地项目文件夹时",
        "params": {
            "path": {"type": "string", "description": "目标文件绝对路径或项目内相对路径；须位于已授权可写目录"},
            "text": {"type": "string", "description": "要写入的 UTF-8 文本", "maxLength": MAX_TEXT_BYTES},
            "overwrite": {"type": "boolean", "description": "目标已存在时是否覆盖；默认 false，先返回错误"},
            "append": {"type": "boolean", "description": "追加到已有文件末尾，用于分段写长报告；与 overwrite 互斥"},
        },
        "summary": lambda args: f"写入 {args.get('path', '')}（{len(str(args.get('text') or ''))}字符）",
    },
    {
        "name": "local_make_dir",
        "handler": local_make_dir,
        "title": "创建本地目录",
        "description": (
            "在用户已授权的可写目录内创建目录（含多级父目录）；只写授权目录，不读写平台数据目录与密钥目录；"
            "不能执行 shell / Python"
        ),
        "when": "需要为交付物准备输出文件夹时",
        "params": {"path": {"type": "string", "description": "目录绝对路径或项目内相对路径；须位于已授权可写目录"}},
        "summary": lambda args: f"创建目录 {args.get('path', '')}",
    },
    {
        "name": "local_read_text",
        "handler": local_read_text,
        "title": "读本地文本文件",
        "description": (
            "分段读取已授权只读/可写目录内的 UTF-8 文本；二进制与 docx/pdf/xlsx 等办公文件明确报错，"
            "不读取平台数据目录与密钥目录"
        ),
        "when": "需要查看已授权目录里的文本、CSV、标记语言或代码文件时",
        "params": {
            "path": {"type": "string", "description": "文件绝对路径或项目内相对路径；须位于已授权目录"},
            "offset": {"type": "integer", "minimum": 0, "description": "起始字符偏移，默认0"},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": TEXT_READ_LIMIT,
                "description": f"本次最多读取字符数，默认{TEXT_READ_LIMIT}",
            },
        },
        "summary": lambda args: f"读取 {args.get('path', '')}",
    },
    {
        "name": "local_chart",
        "handler": local_chart,
        "title": "生成本地图表",
        "description": (
            "用 Pillow 生成 PNG 图表（bar/line/pie）并保存到授权目录；返回 path 与 url，"
            "可把 url 写进对话 Markdown，或用 path 插入 Word/PPT。不依赖 matplotlib，不执行 shell"
        ),
        "when": "报告/PPT 需要真实数据图、且本地没有 matplotlib 或希望快速出图时",
        "params": {
            "kind": {"type": "string", "description": "bar / line / pie"},
            "title": {"type": "string", "description": "图表标题"},
            "labels": {"type": "array", "description": "横轴/分类标签数组"},
            "values": {"type": "array", "description": "与 labels 等长的数字数组"},
            "path": {"type": "string", "description": "PNG 输出路径（授权目录内）"},
            "width": {"type": "integer", "description": "像素宽，默认 1200"},
            "height": {"type": "integer", "description": "像素高，默认 675"},
        },
        "summary": lambda args: f"生成{args.get('kind', 'bar')}图表 {args.get('title', '')}",
    },
    {
        "name": "local_export_docx",
        "handler": local_export_docx,
        "title": "Markdown 导出 Word",
        "description": (
            "把简单 Markdown（标题、正文、列表、表格、代码块、本地图片 ![](路径)）写成 .docx 到已授权可写目录；"
            "支持 ```chart JSON 代码块直接嵌入图表，数据字段 kind、title、labels、series:[{name,values}]、unit、source；"
            "优先使用 python-docx，缺失时返回可执行的安装提示；只写授权目录，不读写平台数据目录与密钥目录，"
            "不执行 shell / Python"
        ),
        "when": "需要交付 Word 报告、作业文档或说明书时",
        "params": {
            "markdown": {
                "type": "string",
                "description": "Markdown 文本；图表用独立的 ```chart JSON 代码块，或 ![图注](本地图片路径)",
            },
            "path": {"type": "string", "description": "目标 .docx 路径；须位于已授权可写目录"},
            "title": {"type": "string", "description": "可选文档标题"},
        },
        "summary": lambda args: f"导出 Word {args.get('path', '')}",
    },
    {
        "name": "local_append_code",
        "handler": local_append_code,
        "title": "追加源码为代码块",
        "description": (
            "把已授权目录内的一份源码文件原样追加到目标 Markdown 文档，自动加标题和代码围栏；"
            "用于报告代码附录，避免模型重写长代码。只写授权目录，不执行 shell / Python"
        ),
        "when": "报告需要附程序代码，或交付物需要引用真实源码时",
        "params": {
            "path": {"type": "string", "description": "目标 Markdown 文件路径"},
            "source": {"type": "string", "description": "要追加的源码文件路径（须在已授权目录）"},
            "heading": {"type": "string", "description": "可选二级标题，例如“十七、核心程序代码”"},
            "language": {"type": "string", "description": "代码语言，默认 python"},
        },
        "summary": lambda args: f"追加源码 {args.get('source', '')}",
    },
    {
        "name": "local_export_docx_file",
        "handler": local_export_docx_file,
        "title": "Markdown 文件导出 Word",
        "description": (
            "把已授权目录内已有的 .md 文件导出为 .docx；适合先用 local_write_text(append=true) 分段写长报告，"
            "再一次性导出。只写授权目录，不执行 shell / Python"
        ),
        "when": "长报告已分段写成 Markdown 文件，需要转成 Word 交付时",
        "params": {
            "markdown_path": {"type": "string", "description": "已授权目录内的 .md 文件路径"},
            "path": {"type": "string", "description": "目标 .docx 路径；须位于已授权可写目录"},
            "title": {"type": "string", "description": "可选文档标题"},
        },
        "summary": lambda args: f"从 Markdown 导出 Word {args.get('path', '')}",
    },
    {
        "name": "local_export_pptx",
        "handler": local_export_pptx,
        "title": "导出 16:9 汇报 PPT",
        "description": (
            "把 [{title,bullets,notes,layout?,image?,chart?}] 生成为 16:9 .pptx 到已授权可写目录；"
            "chart 直接生成可编辑图表，包含 kind、labels、series:[{name,values}]、unit、source；"
            "有数据时优先嵌入 chart，而不是只列数值要点；expected_slides 校验含封面在内的实际页数；"
            "内置深色封面/章节页、浅色内容页、图表页与页脚；image 可传本地 PNG 路径，layout 可选 bullets/section/image/closing；"
            "优先使用 python-pptx，缺失时返回可执行的安装提示；只写授权目录，不读写平台数据目录与密钥目录，不执行 shell / Python"
        ),
        "when": "需要交付汇报幻灯片、答辩 PPT 或图文提纲时",
        "params": {
            "slides": {
                "type": "array",
                "minItems": 1,
                "maxItems": 80,
                "description": "每页对象：title 标题、bullets 字符串要点数组、notes 备注；可选 layout/image",
                "items": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                        "notes": {"type": "string"},
                        "layout": {"type": "string", "description": "bullets / section / image / closing"},
                        "image": {"type": "string", "description": "本地图片路径，如 图表.png"},
                        "chart": {
                            "type": "object",
                            "description": "嵌入可编辑图表，与 image 二选一；仅用真实数据，注明单位和来源",
                            "required": ["kind", "labels", "series"],
                            "properties": {
                                "kind": {"type": "string", "enum": ["bar", "line", "pie"]},
                                "title": {"type": "string"},
                                "unit": {"type": "string"},
                                "source": {"type": "string"},
                                "labels": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 40},
                                "series": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 6,
                                    "items": {
                                        "type": "object",
                                        "required": ["name", "values"],
                                        "properties": {
                                            "name": {"type": "string"},
                                            "values": {"type": "array", "items": {"type": "number"}},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
            "path": {"type": "string", "description": "目标 .pptx 路径；须位于已授权可写目录"},
            "title": {"type": "string", "description": "可选封面标题"},
            "include_cover": {
                "type": "boolean",
                "description": "是否额外加封面；默认有 title 才添加。要求固定总页数时显式设置",
            },
            "expected_slides": {
                "type": "integer",
                "description": "要求的实际总页数，含封面和自动拆页；不一致时拒绝保存",
                "minimum": 1,
                "maximum": 81,
            },
        },
        "summary": lambda args: f"导出 PPT {args.get('path', '')}（{len(args.get('slides') or [])}页）",
    },
    {
        "name": "local_export_pptx_file",
        "handler": local_export_pptx_file,
        "title": "JSON 幻灯片文件导出 PPT",
        "description": (
            '把已授权目录内的 JSON 文件（[{title,bullets,notes}] 或 {"slides":[...]}）导出为 16:9 .pptx；'
            "适合先用 local_write_text 写幻灯片 JSON，再导出。只写授权目录，不执行 shell / Python"
        ),
        "when": "幻灯片内容已写成 JSON 文件，需要转成 PPT 交付时",
        "params": {
            "slides_path": {"type": "string", "description": "已授权目录内的 JSON 文件路径"},
            "path": {"type": "string", "description": "目标 .pptx 路径；须位于已授权可写目录"},
            "title": {"type": "string", "description": "可选封面标题"},
            "include_cover": {"type": "boolean", "description": "是否额外加封面；也可在 JSON 根对象中设置"},
            "expected_slides": {
                "type": "integer",
                "description": "含封面在内的要求总页数；也可在 JSON 根对象中设置",
                "minimum": 1,
                "maximum": 81,
            },
        },
        "summary": lambda args: f"从 JSON 导出 PPT {args.get('path', '')}",
    },
]


def local_deliverable_tools(cls):
    """平台工具目录里的本地交付物：需要研究者确认（risk=confirm，审批类别 save）。"""
    return [
        cls(
            name=spec["name"],
            title=spec["title"],
            scene="workflow",
            description=spec["description"],
            when=spec["when"],
            risk=spec.get("risk", "confirm"),
            params=spec["params"],
            handler=guarded(spec["handler"], spec["name"]),
            required=required_args(spec["handler"]),
            category=spec.get("category", "save"),
            summary=spec["summary"],
        )
        for spec in LOCAL_DELIVERABLE_SPECS
    ]


def required_args(handler):
    return [
        k
        for k, p in signature_inspect.signature(handler).parameters.items()
        if p.default is signature_inspect.Parameter.empty
    ]


def guarded(handler, name):
    from functools import wraps

    @wraps(handler)
    def call(*args, **kwargs):
        from .plugins import guard_tool

        guard_tool(service(), name)
        return handler(*args, **kwargs)

    return call


def plugin_catalog():
    from .plugins import configured_policy

    return {"plugins": configured_policy(service())}


def research_notes(
    workflow_id: str, text: str | None = None, run_ids: list[str] | None = None, expected_version: int = 0
):
    from .notes import read, save

    if text is None:
        return read(service(), workflow_id)
    return save(service(), workflow_id, text, run_ids or [], expected_version)


def experiment_schema():
    return {
        "type": "object",
        "description": 'grid示例：{"mode":"grid","axes":[{"node":"source","parameter":"max_rows","values":[10,20]}]}；rolling示例见workflow_help(experiments)',
        "properties": {
            "mode": {"type": "string", "enum": ["grid", "rolling"]},
            "axes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "required": ["node", "parameter", "values"],
                    "properties": {
                        "node": {"type": "string"},
                        "parameter": {"type": "string"},
                        "values": {"type": "array", "minItems": 1, "maxItems": 8},
                    },
                },
            },
            "windows": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "required": ["updates"],
                    "properties": {
                        "name": {"type": "string"},
                        "updates": {
                            "type": "array",
                            "items": {"type": "object", "required": ["node", "parameter", "value"]},
                        },
                    },
                },
            },
        },
    }
