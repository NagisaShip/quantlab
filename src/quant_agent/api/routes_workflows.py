"""Typed research workflow API. All state is bound to the verified workspace."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..platform import context
from ..workflows.contracts import WorkflowError, parse_definition
from ..workflows.runtime import Runner
from ..workflows.service import WorkflowService
from .deps import get_services

router = APIRouter(prefix="/api/workflows", tags=["研究工作流"])


def svc():
    return WorkflowService(get_services())


class DefinitionIn(BaseModel):
    code: str = Field(max_length=150000)
    old_kind: str = ""
    old_version: str = ""


class SaveIn(BaseModel):
    workflow: dict
    expected_revision: int | None = None


class RunIn(BaseModel):
    target: str | None = None
    mode: str = "research"
    pins: dict | None = None
    revision: int | None = None
    seed: int | None = Field(default=None, ge=0, le=4294967295)


class ExportIn(BaseModel):
    revision: int | None = None
    include_wheels: bool = True


class GroupIn(BaseModel):
    workflow: dict
    selected: list[str] = Field(default_factory=list)
    kind: str = "user.subflow"
    title: str = "复用子流程"
    node_id: str = ""
    exposed: list[dict] = Field(default_factory=list, max_length=100)


class NetworkIn(BaseModel):
    graph: dict


class ChartIn(BaseModel):
    config: dict
    artifact_hash: str = ""
    data: list[dict] | None = Field(default=None, max_length=5000)


class AssetIn(BaseModel):
    domain: str
    asset_id: str


class BatchIn(BaseModel):
    plan: dict
    revision: int | None = None


class CompareIn(BaseModel):
    run_ids: list[str] = Field(min_length=1, max_length=8)


class NotesIn(BaseModel):
    text: str = Field(max_length=100000)
    run_ids: list[str] = Field(default_factory=list, max_length=30)
    expected_version: int = Field(ge=0)


class PinIn(BaseModel):
    hash: str
    run_id: str
    workflow_id: str
    node_id: str
    port: str
    title: str = Field(default="", max_length=200)
    type: str = Field(default="", max_length=40)
    note: str = Field(default="", max_length=2000)


@router.get("/{wid}/notes", summary="研究结论、笔记与冻结运行引用")
def read_notes(wid: str):
    from ..workflows.notes import read

    return read(svc(), wid)


@router.get("/{wid}/activity", summary="GUI与Agent共用的最近运行与进行中任务")
def workflow_activity(wid: str):
    service = svc()
    record = service.get(wid)
    rows = service.services.runs.list(context.current_workspace(), kind="workflow", limit=500)
    rows = [row for row in rows if row.get("manifest", {}).get("workflow_id") == wid][:30]
    return {
        "revision": record["revision"],
        "runs": [
            {
                **{k: r.get(k) for k in ("id", "state", "progress", "created_at", "error_message")},
                "revision": r["manifest"].get("revision"),
                "origin": r["manifest"].get("origin"),
                "name": r["manifest"].get("variant", {}).get("name") or r.get("title"),
            }
            for r in rows
        ],
    }


@router.get("/recent-result", summary="当前工作区最近一次有工件证据的已完成研究")
def recent_result():
    from ..workflows.recent import latest_result

    return latest_result(svc())


@router.put("/{wid}/notes", summary="保存带版本检查的研究笔记")
def save_notes(wid: str, payload: NotesIn):
    from ..workflows.notes import save

    return save(svc(), wid, payload.text, payload.run_ids, payload.expected_version)


@router.post("/experiments/compare", summary="读取实验指标并保留失败和未完成结果")
def compare_experiments(payload: CompareIn):
    from ..workflows.experiments import comparison

    return comparison(svc(), payload.run_ids)


@router.get("/assets", summary="浏览同一工作区的策略、因子、模型与组合执行资产")
def assets():
    from ..workflows.assets import listing

    return {"assets": listing(svc())}


@router.post("/assets/snapshot", summary="以冻结定义创建已有资产的研究模块")
def asset_snapshot(payload: AssetIn):
    from ..workflows.assets import snapshot

    service = svc()
    service.writable()
    return snapshot(service, payload.domain, payload.asset_id)


@router.post("/network/inspect", summary="验证神经组件图并返回形状、参数量与代码")
def network_inspect(payload: NetworkIn):
    from quantlab_research.network import code, inspect

    try:
        return {**inspect(payload.graph), "code": code(payload.graph)}
    except (ValueError, TypeError, KeyError) as exc:
        raise WorkflowError(str(exc)) from exc


@router.post("/charts/preview", summary="使用已冻结数据预览图表，不重跑上游研究")
def chart_preview(payload: ChartIn):
    import base64

    service = svc()
    data = service.chart_data(payload.artifact_hash, payload.data)
    from ..workflows.plugins import require

    require(service, "charts")
    spec = service.chart_preview(data, payload.config, render=True)
    return {
        "spec": spec,
        "png": "data:image/png;base64,"
        + base64.b64encode((service.files / spec["files"]["png"]["file"]).read_bytes()).decode(),
        "svg": (service.files / spec["files"]["svg"]["file"]).read_text(encoding="utf-8"),
    }


@router.get("/catalog", summary="函数卡片、完整代码与研究模板")
def catalog():
    from quantlab_research.templates import templates

    from ..workflows.templates import classification, csv_research

    return {
        "cards": svc().registry().catalog(),
        "templates": [*templates(), csv_research(), classification()],
        "custom_code_policy": "可信Python扩展；子进程超时、取消与凭证环境清理，不是对抗恶意代码的沙箱",
    }


@router.get("/guide", summary="查询工作流、组件图和运行工件规范")
def workflow_guide(topic: str = "workflow"):
    from ..workflows.guide import help_for

    return help_for(topic)


@router.get("/{wid}/view", summary="对话侧只读编排图与真实运行状态")
def chat_view(wid: str, run_id: str = "", revision: int | None = None):
    from ..workflows.views import workflow_view

    service = svc()
    record, run, report = service.inspected(wid, run_id, revision)
    return workflow_view(service, record, report, run)


@router.get("/files", summary="工作区已导入的不可覆盖输入文件")
def files():
    from ..workflows.files import references

    return {
        "files": [
            {"name": p.name, "bytes": p.stat().st_size}
            for p in svc().files.iterdir()
            if p.is_file() and not p.name.startswith(".")
        ]
        + [
            {"name": name, "bytes": ref["bytes"], "mode": "reference", "path": ref["path"], "sha256": ref["sha256"]}
            for name, ref in references(svc().files).items()
        ]
    }


@router.get("/files/preview", summary="分页预览生成的数据文件或文本内容")
def preview_generated_file(name: str, offset: int = 0, limit: int = 20):
    service = svc()
    path = (service.files / name).resolve()
    if not path.is_relative_to(service.files.resolve()) or not path.is_file() or path.name.startswith("."):
        raise WorkflowError("本工作区没有该文件")
    if offset < 0 or not 1 <= limit <= 100:
        raise WorkflowError("分页范围无效")
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".json", ".py", ".log"):
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            text = stream.read(200001)
        return {
            "value": text[:200000],
            "truncated": len(text) > 200000,
            "files": [],
            "download": "/api/workflows/files/download?name=" + quote(name),
            "note": "文本最多预览20万字符",
        }
    result = service.preview_file(path, limit=limit, offset=offset)
    return {
        "value": result["data"],
        "total": result["total"],
        "offset": offset,
        "truncated": result["has_more"],
        "next_offset": result["next_offset"],
        "files": [],
    }


@router.post("/files", summary="流式导入研究文件，不加载整个文件到内存")
async def upload(request: Request, name: str):
    service = svc()
    service.writable()
    original = service.file_path(name).name
    stored = uuid.uuid4().hex[:10] + "-" + original
    path = service.file_path(stored)
    count = 0
    h = hashlib.sha256()
    try:
        with path.open("xb") as f:
            async for block in request.stream():
                count += len(block)
                if not service.services.local and count > 1024 * 1024 * 1024:
                    raise HTTPException(413, "单个输入文件不能超过1GiB")
                f.write(block)
                h.update(block)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        "name": stored,
        "original_name": original,
        "bytes": count,
        "sha256": h.hexdigest(),
    }


@router.post("/definitions/preview", summary="静态检查代码并展示端口、参数及代码差异")
def preview(payload: DefinitionIn):
    return (
        svc().interface_diff(payload.old_kind, payload.old_version, payload.code)
        if payload.old_kind
        else {"definition": parse_definition(payload.code), "changed": {}}
    )


@router.post("/definitions", summary="发布不可覆盖的自定义函数卡片版本")
def publish(payload: DefinitionIn):
    return svc().publish_card(payload.code)


@router.get("/definitions/{kind}/{version}/sources", summary="查看真实实现包源码或指定运行的冻结源码")
def implementation_sources(kind: str, version: str, run_id: str = ""):
    return svc().definition_sources(kind, version, run_id)


@router.post("/subflows/encapsulate", summary="将选中卡片封装为具名输入输出的复用子流程")
def group(payload: GroupIn):
    from ..workflows.subflows import encapsulate

    service = svc()
    service.writable()
    code, workflow = encapsulate(
        payload.workflow,
        payload.selected,
        payload.kind,
        payload.title,
        service.registry(),
        exposed=payload.exposed,
    )
    definition = service.publish_card(code)
    return {"definition": definition, "workflow": workflow}


@router.post("/subflows/expand", summary="展开子流程为可编辑卡片，保持输入输出连接")
def ungroup(payload: GroupIn):
    from ..workflows.subflows import expand

    return {"workflow": expand(payload.workflow, payload.node_id, svc().registry())}


@router.get("/experiments", summary="工作流实验与来源记录")
def experiments(workflow_id: str = ""):
    rows = svc().store.list("workflow_experiments")
    return {
        "experiments": sorted(
            [r for r in rows if not workflow_id or r["workflow_id"] == workflow_id],
            key=lambda r: -r["created_at"],
        )[:100]
    }


@router.get("/runs/{run_id}", summary="查询运行状态、节点输入输出与日志")
def run_status(run_id: str):
    service = svc()
    run = get_services().runs.require(run_id, context.current_workspace())
    return {"run": run, "report": service.run_report(run_id)}


@router.post("/runs/{run_id}/export", summary="以运行时冻结的工作流、版本和实现包构建离线复现包")
def export_run(run_id: str, payload: ExportIn):
    return svc().queue_run_export(run_id, include_wheels=payload.include_wheels)


@router.get("/runs/{run_id}/children/{child_id}", summary="读取本次实验内的子模块运行证据")
def child_evidence(run_id: str, child_id: str):
    return svc().child_report(run_id, child_id)


@router.get("/files/download", summary="下载本工作区数据或二进制研究工件")
def file_download(name: str, preview: bool = False, filename: str = ""):
    service = svc()
    path = (service.files / name).resolve()
    if not path.is_relative_to(service.files.resolve()) or not path.is_file():
        raise HTTPException(404, "本工作区没有该文件")
    display_name = filename if filename and len(filename) <= 200 and Path(filename).name == filename else path.name
    return FileResponse(
        path,
        filename=display_name,
        content_disposition_type="inline" if preview and path.suffix.lower() == ".pdf" else "attachment",
    )


# 固定成果与版本历史必须注册在 /artifacts/{digest} 之前：FastAPI 按注册顺序匹配，
# 否则 "pins" / "versions" 会被当成工件 hash 抢先命中。
@router.post("/artifacts/pins", summary="固定本工作区的一个真实运行产物")
def pin_artifact(payload: PinIn):
    from ..workflows.artifacts import pin_create

    return pin_create(svc(), payload.model_dump())


@router.get("/artifacts/pins", summary="列出当前工作区的固定成果")
def artifact_pins(workflow_id: str = "", version_key: str = "", limit: int = 100):
    from ..workflows.artifacts import pins

    return pins(svc(), workflow_id, version_key, limit)


@router.delete("/artifacts/pins/{pin_id}", summary="取消固定成果")
def unpin_artifact(pin_id: str):
    from ..workflows.artifacts import pin_delete

    return pin_delete(svc(), pin_id)


@router.get("/artifacts/versions", summary="按版本键读取同一节点端口的历史产物")
def artifact_versions(version_key: str = "", hash: str = "", limit: int = 100):
    from ..workflows.artifacts import versions

    return versions(svc(), version_key, hash, limit)


@router.get("/artifacts/{digest}", summary="读取本工作区工件的原始JSON内容")
def artifact(digest: str, limit: int = 100, offset: int = 0):
    from ..workflows.artifacts import preview

    return preview(svc(), digest, offset, limit)


@router.get("/artifacts/{digest}/download", summary="下载经完整性验证的工件JSON或表格CSV")
def artifact_download(digest: str, format: str = "json"):
    import csv
    import io
    import json

    value = Runner(svc().root, svc().registry()).read({"hash": digest})
    filename = "artifact-" + digest[:12]
    if format == "csv":
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise WorkflowError("仅对象表格支持CSV导出")
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for row in value for k in row)))
        writer.writeheader()
        writer.writerows(value)
        return Response(
            content="\ufeff" + stream.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )
    if format != "json":
        raise WorkflowError("format仅支持json或csv")
    return Response(
        content=json.dumps(value, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}.json"'},
    )


@router.get("/artifacts/{digest}/versions", summary="由工件hash反查同节点端口的历史版本")
def artifact_versions_by_hash(digest: str, version_key: str = "", limit: int = 100):
    from ..workflows.artifacts import versions

    return versions(svc(), version_key, digest, limit)


@router.get("/artifacts", summary="按研究、运行和关键词检索真实产物")
def artifact_center(workflow_id: str = "", run_id: str = "", query: str = "", offset: int = 0, limit: int = 40):
    from ..workflows.artifacts import listing

    return listing(svc(), workflow_id, run_id, query, offset, limit)


@router.get("/exports/{name}", summary="下载含依赖安装包的独立运行研究包")
def download(name: str):
    service = svc()
    if Path(name).name != name or not name.endswith(".zip"):
        raise HTTPException(400, "非法导出文件名")
    path = (service.root / "exports" / name).resolve()
    if not path.is_relative_to((service.root / "exports").resolve()) or not path.is_file():
        raise HTTPException(404, "导出文件不存在")
    return FileResponse(path, filename=name, media_type="application/zip")


@router.get("", summary="工作流列表与当前版本")
def listing():
    return {"workflows": svc().workflows()}


@router.post("", summary="保存可继续编辑的工作流草稿")
def create(payload: SaveIn):
    return svc().save(payload.workflow)


@router.get("/{wid}", summary="读取工作流及历史快照")
def get(wid: str, revision: int | None = None):
    return svc().get(wid, revision)


@router.put("/{wid}", summary="校验修订号并创建新工作流版本")
def save(wid: str, payload: SaveIn):
    return svc().save(payload.workflow, wid=wid, expected_revision=payload.expected_revision)


@router.post("/{wid}/validate", summary="运行前自检，不执行卡片代码")
def validate(wid: str, payload: RunIn):
    return svc().validate(svc().get(wid, payload.revision)["workflow"], target=payload.target)


@router.post("/{wid}/run", summary="使用现有Worker执行完整或局部工作流")
def run(wid: str, payload: RunIn):
    if payload.mode not in ("research", "debug"):
        raise WorkflowError("mode仅支持research或debug")
    return svc().queue(
        wid,
        target=payload.target,
        pins=payload.pins,
        mode=payload.mode,
        revision=payload.revision,
        seed=payload.seed,
    )


@router.post("/{wid}/export", summary="后台构建含代码、数据及依赖wheel的完整离线包")
def export(wid: str, payload: ExportIn):
    return svc().queue_export(wid, revision=payload.revision, include_wheels=payload.include_wheels)


@router.post("/{wid}/experiments/plan", summary="预览参数网格或滚动时间窗口，不执行研究")
def plan_experiments(wid: str, payload: BatchIn):
    from ..workflows.experiments import variants

    service = svc()
    return {"variants": variants(service.get(wid, payload.revision)["workflow"], service.registry(), payload.plan)}


@router.post("/{wid}/experiments/submit", summary="按冻结修订提交独立实验，不修改画布")
def submit_experiments(wid: str, payload: BatchIn):
    from ..workflows.experiments import submit

    return submit(svc(), wid, payload.plan, payload.revision)


@router.get("/{workflow_id}/module-view", summary="逐层查看模块内部函数、参数和运行证据")
def module_view(workflow_id: str, node_path: str, revision: int | None = None, run_id: str = ""):
    from ..workflows.contracts import params_for, subflow_instance
    from ..workflows.views import workflow_view

    service = svc()
    record = service.get(workflow_id, revision)
    run = None
    report = None
    if run_id:
        from ..platform.context import current_workspace

        run = service.services.runs.require(run_id, current_workspace())
        if run["manifest"].get("workflow_id") != workflow_id or run["manifest"].get("revision") != record["revision"]:
            raise WorkflowError("运行与工作流修订不一致")
        report = service.run_report(run_id)
    flow = record["workflow"]
    parts = node_path.split("/")
    if not 1 <= len(parts) <= 8:
        raise WorkflowError("模块展开路径需要1—8层节点ID")
    for part in parts:
        node = next((n for n in flow["nodes"] if n["id"] == part), None)
        if not node:
            raise WorkflowError("模块路径不存在：" + part)
        definition = service.registry().get(node["kind"], str(node["version"]))
        if not definition.get("subflow"):
            raise WorkflowError("该函数没有内部模块，可在函数代码页查看实现")
        flow = subflow_instance(definition, params_for(node, definition))
        child = (report or {}).get("nodes", {}).get(part, {}).get("child_run_id")
        report = service.child_report(run_id, child) if child else None
    view = workflow_view(
        service, {**record, "workflow": flow, "title": definition["title"] + " · 内部函数"}, report, run
    )
    view["module_path"] = node_path
    return view
