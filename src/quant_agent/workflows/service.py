"""Workspace-scoped application boundary shared by HTTP and Agent tools."""

from __future__ import annotations

import difflib
import json
import time
import uuid
from pathlib import Path

from .contracts import (
    FORMAT,
    WorkflowError,
    compile_workflow,
    identifier,
    normalize_workflow,
    parse_definition,
)
from .environments import EnvironmentManager
from .registry import Registry
from .runtime import Runner


class WorkflowService:
    def __init__(self, services):
        self.services = services
        self.store = services.store
        self.root = Path(self.store.root) / "workflows"
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = self.root / "files"
        self.files.mkdir(exist_ok=True)

    def writable(self):
        from ..platform.context import current

        if not current().can_write:
            from ..platform.errors import AppError, ErrorCode

            raise AppError(ErrorCode.PERMISSION_DENIED, "当前工作区角色不能修改或运行工作流")

    def import_data(self, name, *, data=None, csv_text=""):
        import csv
        import io

        from .files import digest

        self.writable()
        if (data is not None) == bool(csv_text):
            raise WorkflowError("data与csv_text二选一")
        path = self.file_path(name)
        if path.suffix.lower() != ".csv":
            raise WorkflowError("显式数据导入的名称须以.csv结尾")
        if path.exists():
            raise WorkflowError("资源已存在，导入不覆盖；请使用新名称")
        if data is not None:
            if not isinstance(data, list) or not 1 <= len(data) <= 5000 or any(not isinstance(r, dict) for r in data):
                raise WorkflowError("data需要1—5000行对象表格")
            names = list(data[0])
            if not names or any(set(r) != set(names) for r in data):
                raise WorkflowError("表格每行须具有相同的非空字段")
            text = io.StringIO(newline="")
            writer = csv.DictWriter(text, fieldnames=names)
            writer.writeheader()
            writer.writerows(data)
            csv_text = text.getvalue()
        if len(csv_text) > 2000000:
            raise WorkflowError("Agent文本导入最多200万字符；大文件使用本地引用或流式导入")
        reader = csv.DictReader(io.StringIO(csv_text))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise WorkflowError("CSV字段为空或重复")
        count = 0
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise WorkflowError("CSV列数不匹配")
            count += 1
        with path.open("x", encoding="utf-8", newline="") as stream:
            stream.write(csv_text)
        return {
            "name": path.name,
            "sha256": digest(path),
            "rows": count,
            "columns": reader.fieldnames,
            "bytes": path.stat().st_size,
            "source": "explicit agent data",
        }

    def call_environment(self, code, inputs, params, requirements=()):
        definition = {
            "code": code,
            "requirements": list(requirements),
            "bundled_modules": ["quantlab_research", "quantlab_files"],
        }
        return Runner(self.root, self.registry(), files=self.files)._execute(definition, inputs, params, 7, 180)[
            "outputs"
        ]

    def chart_preview(self, data, config, *, render=False):
        if render:
            code = "def execute(ctx,inputs,params):\n from quantlab_research.charts import execute\n return execute(ctx,inputs,params)\n"
            return self.call_environment(
                code, {"data": data}, config, ["pandas>=2.2,<4", "matplotlib>=3.8,<4", "networkx>=3,<4"]
            )["chart"]
        code = 'def execute(ctx,inputs,params):\n from quantlab_research.charts import prepare\n return {"spec":prepare(inputs["data"],params)[1]}\n'
        return self.call_environment(code, {"data": data}, config, ["pandas>=2.2,<4"])["spec"]

    def preview_file(self, path, *, limit=20, offset=0, encoding="utf-8-sig"):
        if not 1 <= limit <= 100:
            raise WorkflowError("预览limit须为1—100")
        code = 'def execute(ctx,inputs,params):\n from quantlab_files import read\n return {"preview":read(inputs["path"],**params)}\n'
        return self.call_environment(
            code, {"path": str(path)}, {"limit": limit, "offset": offset, "encoding": encoding}
        )["preview"]

    def find_parent(self, child_run_id, workflow_id=""):
        from ..platform.context import current_workspace

        identifier(child_run_id)
        roots = self.services.db.fetchall(
            "SELECT id FROM runs WHERE workspace_id=? AND kind='workflow' ORDER BY created_at DESC",
            (current_workspace(),),
        )
        for row in roots:
            run = self.services.runs.require(row["id"], current_workspace())
            if workflow_id and run.get("manifest", {}).get("workflow_id") != workflow_id:
                continue
            try:
                self.child_report(row["id"], child_run_id)
                return row["id"]
            except WorkflowError:
                continue
        raise WorkflowError("本工作区没有包含该子运行的父运行；使用工作流运行报告中的child_run_id")

    def registry(self):
        return Registry(self.store.list("workflow_cards"))

    def definition_sources(self, kind, version, run_id=""):
        from .bundles import sources

        root = None
        if run_id:
            from ..platform.context import current_workspace

            record = self.services.runs.require(run_id, current_workspace())
            manifest = record["manifest"]
            registry = Registry(manifest.get("definitions", []), frozen=True)
            root = self.materialize_sources(run_id, manifest.get("source_snapshot", []))
        else:
            registry = self.registry()
        definition = registry.get(kind, version)
        visited = {}
        modules = set()
        files = []

        def collect(d, depth=0):
            key = (d["kind"], str(d["version"]))
            if key in visited:
                return
            if depth > 8:
                raise WorkflowError("子流程层级超过8层")
            visited[key] = d
            modules.update(d.get("bundled_modules", []))
            for child in d.get("subflow", {}).get("workflow", {}).get("nodes", []):
                child_def = registry.get(child["kind"], str(child["version"]))
                collect(child_def, depth + 1)

        collect(definition)
        for d in visited.values():
            if d is not definition:
                files.append(
                    {
                        "path": "cards/" + d["kind"] + "@" + str(d["version"]) + ".py",
                        "code": d["code"],
                        "sha256": d["code_hash"],
                        "kind": d["kind"],
                        "version": str(d["version"]),
                    }
                )
        files.extend(f for bundle in sources(sorted(modules), root) for f in bundle["files"])
        return {"definition": definition, "files": files, "run_id": run_id or None}

    def source_snapshot(self, definitions):
        from .bundles import sources

        names = sorted({name for d in definitions for name in d.get("bundled_modules", [])})
        return [f for bundle in sources(names) for f in bundle["files"]]

    def materialize_sources(self, run_id, files):
        from .contracts import fingerprint

        root = self.root / "source_snapshots" / identifier(run_id) / fingerprint(files)[:16]
        root.mkdir(parents=True, exist_ok=True)
        for item in files:
            target = (root / item["path"]).resolve()
            if not target.is_relative_to(root.resolve()) or target.suffix != ".py":
                raise WorkflowError("非法源码快照路径")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text(item["code"], encoding="utf-8")
            elif target.read_text(encoding="utf-8") != item["code"]:
                raise WorkflowError("冻结实现包源码被修改，请核对来源：" + item["path"])
        return root

    def workflows(self):
        return sorted(self.store.list("workflows"), key=lambda r: -r.get("updated_at", 0))

    def child_report(self, parent_run_id, child_run_id):
        from ..platform.context import current_workspace

        self.services.runs.require(parent_run_id, current_workspace())
        pending = [self.run_report(parent_run_id)]
        seen = set()
        while pending:
            report = pending.pop()
            if not report or report["id"] in seen:
                continue
            seen.add(report["id"])
            for node in report["nodes"].values():
                child = node.get("child_run_id")
                if child == child_run_id:
                    return self.run_report(child)
                if child:
                    pending.append(self.run_report(child))
        raise WorkflowError("该子运行不属于指定的本工作区实验")

    def get(self, wid, revision=None):
        identifier(wid)
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
            raise WorkflowError("已保存修订号从1开始；创建首版请使用expected_revision=0")
        record = (
            self.store.get("workflow_versions", f"{wid}.r{revision}") if revision else self.store.get("workflows", wid)
        )
        if not record:
            raise WorkflowError("本工作区没有该工作流版本")
        # Version rows use a storage key ending in .rN; it is never a workflow ID.
        return {**record, "id": wid}

    def save(self, workflow, *, wid=None, expected_revision=None, origin="gui"):
        from ..platform.context import current
        from ..store import _FileLock

        self.writable()
        workflow = normalize_workflow(workflow)
        if expected_revision is not None and (
            isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0
        ):
            raise WorkflowError("expected_revision须为非负整数；创建用0，更新用当前revision")
        if workflow.get("format") != FORMAT or not isinstance(workflow.get("nodes"), list):
            raise WorkflowError("工作流格式无效")
        if len(workflow["nodes"]) > 160:
            raise WorkflowError("卡片超过160张")
        wid = identifier(wid or "workflow-" + uuid.uuid4().hex[:12])
        with _FileLock(self.root / "definitions.lock"):
            old = self.store.get("workflows", wid)
            if not old and expected_revision not in (None, 0):
                raise WorkflowError("本工作区没有该工作流；创建首版请用workflow_create或expected_revision=0")
            if old and expected_revision != old["revision"]:
                raise WorkflowError("工作流已有新版本，请刷新后合并修改")
            revision = (old["revision"] if old else 0) + 1
            record = {
                "id": wid,
                "revision": revision,
                "title": str(workflow.get("title", "未命名研究"))[:160],
                "workflow": workflow,
                "updated_at": time.time(),
                "created_by": current().user_id,
                "origin": origin,
            }
            self.store.upsert("workflow_versions", {**record, "id": f"{wid}.r{revision}"})
            self.store.upsert("workflows", record)
        return record

    def inspected(self, wid, run_id="", revision=None):
        """Bind run evidence to its real workspace, workflow and frozen revision."""
        from ..platform.context import current_workspace

        record = self.get(wid, revision)
        if not run_id:
            return record, None, None
        run = self.services.runs.require(run_id, current_workspace())
        manifest = run.get("manifest", {})
        if manifest.get("workflow_id") != wid:
            raise WorkflowError("该运行不属于指定工作流")
        if manifest.get("workflow"):
            record = {**record, "workflow": manifest["workflow"], "revision": manifest["revision"]}
        return record, run, self.run_report(run_id)

    def chart_data(self, artifact_hash="", data=None):
        if artifact_hash and data is not None:
            raise WorkflowError("artifact_hash与data二选一，不能同时指定")
        if artifact_hash:
            try:
                data = Runner(self.root, self.registry()).read({"hash": artifact_hash})
            except WorkflowError as exc:
                raise WorkflowError(
                    str(exc)
                    + "；artifact_hash来自本工作区运行报告nodes[节点].outputs[端口].hash；资产sha256/code_hash不是工件。可改用data显式表格预览，或workflow_help(topic='artifacts')。"
                ) from exc
        if isinstance(data, dict) and data.get("format") == "quantlab.chart/1":
            data = data["data"]
        if not isinstance(data, list) or len(data) > 5000 or any(not isinstance(row, dict) for row in data):
            raise WorkflowError("需要不超过5000行的对象表格；先运行上游并使用table输出hash，或显式提供data:[{字段:值}]")
        return data

    def validate(self, workflow, *, target=None):
        checks = []
        try:
            plan = compile_workflow(workflow, self.registry(), target=target)
        except (WorkflowError, KeyError, TypeError) as exc:
            return {
                "ok": False,
                "checks": [{"name": "连接与参数契约", "passed": False, "message": str(exc)}],
            }
        checks.append({"name": "连接、研究用途、参数和DAG", "passed": True})
        registry = self.registry()
        instances = []

        def collect(node, label):
            definition = registry.get(node["kind"], str(node["version"]))
            instances.append((label, node, definition))
            if definition.get("subflow"):
                from .contracts import params_for, subflow_instance

                for child in subflow_instance(definition, params_for(node, definition))["nodes"]:
                    collect(child, label + "/" + child["id"])

        for nid in plan["order"]:
            collect(plan["nodes"][nid], nid)
        for nid, _node, d in instances:
            if d.get("view") == "network":
                from quantlab_research.network import inspect

                from .contracts import params_for

                try:
                    spec = inspect(params_for(_node, d).get("graph", {}))
                    if spec["parameter_count"] > 10_000_000:
                        raise ValueError("基础组件训练器参数量超过一千万")
                    checks.append(
                        {
                            "node": nid,
                            "name": "组件图形状与参数量",
                            "passed": True,
                            "message": str(spec["parameter_count"]) + "个参数",
                        }
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    checks.append({"node": nid, "name": "组件图形状与参数量", "passed": False, "message": str(exc)})
            for req in d.get("requirements", []):
                try:
                    from .environments import EnvironmentManager

                    installed = EnvironmentManager(self.root).check(req)
                    passed = True
                except WorkflowError as exc:
                    installed = str(exc)
                    passed = False
                checks.append(
                    {
                        "node": nid,
                        "name": "依赖 " + req,
                        "passed": passed,
                        "message": installed,
                    }
                )
        try:
            runner = Runner(self.root, self.registry(), files=self.files)
            from .contracts import params_for

            for _nid, node, d in instances:
                runner._file_hashes(d, params_for(node, d), hash_contents=False)
            resources = workflow.get("resources", [])
            runner._file_hashes({"file_params": resources}, {name: name for name in resources}, hash_contents=False)
            checks.append({"name": "输入文件已导入；执行时核验内容哈希", "passed": True})
        except WorkflowError as exc:
            checks.append({"name": "输入文件", "passed": False, "message": str(exc)})
        return {
            "ok": all(x["passed"] for x in checks),
            "checks": checks,
            "order": plan["order"],
            "workflow_hash": plan["semantic_hash"],
            "estimated_tasks": len(instances),
            "scope": "静态契约、依赖与输入检查；不代表试运行或研究方法审查已通过",
        }

    def publish_card(self, code, *, origin="gui"):
        from ..store import _FileLock

        self.writable()
        spec = parse_definition(code)
        with _FileLock(self.root / "definitions.lock"):
            self.registry().add(code)
            key = spec["kind"] + ".v" + str(spec["version"])
            if self.store.get("workflow_cards", key):
                return spec
            from ..platform.context import current

            self.store.upsert(
                "workflow_cards",
                {"id": key, "code": code, "updated_at": time.time(), "origin": origin, "created_by": current().user_id},
            )
        return spec

    def interface_diff(self, old_kind, old_version, code):
        old = self.registry().get(old_kind, old_version)
        new = parse_definition(code)
        fields = ("inputs", "outputs", "parameters", "requirements")
        return {
            "changed": {k: {"before": old.get(k), "after": new.get(k)} for k in fields if old.get(k) != new.get(k)},
            "code_diff": "".join(
                difflib.unified_diff(
                    old["code"].splitlines(True),
                    code.splitlines(True),
                    fromfile="原版本",
                    tofile="编辑版本",
                )
            ),
            "definition": new,
        }

    def queue(
        self,
        wid,
        *,
        target=None,
        pins=None,
        mode="research",
        origin="gui",
        revision=None,
        seed=None,
        variant=None,
    ):
        self.writable()
        record = self.get(wid, revision)
        from .plugins import require_flow
        require_flow(self, record["workflow"], agent=origin == "agent")
        if variant:
            import copy

            record = copy.deepcopy(record)
            record["workflow"] = variant["workflow"]
        if seed is not None:
            import copy

            if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 4294967295:
                raise WorkflowError("种子必须是0至4294967295之间的整数")
            record = copy.deepcopy(record)
            record["workflow"].setdefault("settings", {})["seed"] = seed
        checks = self.validate(record["workflow"], target=target)
        if not checks["ok"]:
            raise WorkflowError("运行前检查未通过：" + json.dumps(checks["checks"], ensure_ascii=False))
        if pins and mode != "debug":
            raise WorkflowError("正式运行不能固定调试输出")
        from ..api import tasks

        definitions = self.registry().catalog()
        from ..api.routes_runs import enqueue

        run = enqueue(
            kind="workflow",
            mode="analysis",
            title=record["title"],
            manifest={
                "workflow_id": wid,
                "revision": record["revision"],
                "workflow": record["workflow"],
                "definitions": definitions,
                "source_snapshot": self.source_snapshot(definitions),
                "target": target,
                "pins": pins,
                "mode": mode,
                "origin": origin,
                "execution_environment": EnvironmentManager(self.root).info(),
                "variant": {k: v for k, v in (variant or {}).items() if k != "workflow"},
            },
        )
        return tasks.dispatch(self.services, run)

    def queue_export(self, wid, *, revision=None, include_wheels=True):
        self.writable()
        record = self.get(wid, revision)
        compile_workflow(record["workflow"], self.registry())
        from ..api import tasks
        from ..api.routes_runs import enqueue

        definitions = self.registry().catalog()

        run = enqueue(
            kind="workflow_export",
            mode="analysis",
            title="导出独立运行包 · " + record["title"],
            manifest={
                "workflow": record["workflow"],
                "definitions": definitions,
                "source_snapshot": self.source_snapshot(definitions),
                "include_wheels": include_wheels,
                "execution_environment": EnvironmentManager(self.root).info(),
            },
        )
        return tasks.dispatch(self.services, run)

    def queue_run_export(self, run_id, *, include_wheels=True):
        from ..api import tasks
        from ..api.routes_runs import enqueue
        from ..platform.context import current_workspace

        self.writable()
        original = self.services.runs.require(run_id, current_workspace())
        m = original["manifest"]
        if original["kind"] != "workflow" or not m.get("workflow"):
            raise WorkflowError("该记录不是可导出的工作流执行")
        if m.get("mode") == "debug" or m.get("pins"):
            raise WorkflowError("固定输出的调试运行不能导出为正式研究复现包")
        run = enqueue(
            kind="workflow_export",
            mode="analysis",
            title="导出冻结运行 · " + run_id,
            manifest={
                "workflow": m["workflow"],
                "definitions": m["definitions"],
                "source_snapshot": m.get("source_snapshot", []),
                "source_run_id": run_id,
                "include_wheels": include_wheels,
                "execution_environment": m.get("execution_environment") or EnvironmentManager(self.root).info(),
            },
        )
        return tasks.dispatch(self.services, run)

    def run_report(self, run_id):
        identifier(run_id)
        # Root is already scoped to the server-verified workspace.
        path = self.root / "runs" / (run_id + ".json")
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def file_path(self, name):
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise WorkflowError("只接受不带路径的导入文件名")
        p = (self.files / name).resolve()
        if not p.is_relative_to(self.files.resolve()):
            raise WorkflowError("文件路径不在工作区")
        return p

    def export(self, wid, *, revision=None, include_wheels=True):
        self.writable()
        from .exporter import build_package

        record = self.get(wid, revision)
        folder = self.root / "exports"
        folder.mkdir(exist_ok=True)
        name = identifier(wid) + "-" + uuid.uuid4().hex[:8] + ".zip"
        manifest = build_package(
            record["workflow"],
            self.registry(),
            self.files,
            folder / name,
            include_wheels=include_wheels,
            environment_info=EnvironmentManager(self.root).info(),
        )
        return name, manifest


def handle_workflow(ctl):
    service = WorkflowService(ctl.services)
    m = ctl.manifest
    registry = Registry(m["definitions"], frozen=True)
    from .market import native_handlers

    def event(value):
        ctl.progress(value["done"], value["total"])
        ctl.event("workflow_node", value)

    report = Runner(
        service.root,
        registry,
        files=service.files,
        tick=ctl.beat,
        event=event,
        bundles_root=service.materialize_sources(ctl.run_id, m.get("source_snapshot", [])),
        environment_info=m.get("execution_environment"),
        native=native_handlers(ctl.services),
    ).run(
        m["workflow"],
        run_id=ctl.run_id,
        target=m.get("target"),
        pins=m.get("pins"),
        mode=m.get("mode", "research"),
    )
    ctl.services.store.upsert(
        "workflow_experiments",
        {
            "id": ctl.run_id,
            "workflow_id": m["workflow_id"],
            "revision": m["revision"],
            "origin": m.get("origin"),
            "status": report["status"],
            "workflow_hash": report["workflow_hash"],
            "created_at": report["started_at"],
            "seed": m["workflow"].get("settings", {}).get("seed", 7),
            "variant": m.get("variant", {}),
        },
    )
    if report["status"] != "completed":
        raise WorkflowError("工作流未完成，请检查节点错误与被阻断的分支")
    return {
        "workflow_run_id": report["id"],
        "status": report["status"],
        "nodes": len(report["nodes"]),
    }


def _edge_endpoints(edge):
    """兼容规范化前后的两种边写法。"""
    if "source" in edge:
        return edge.get("source"), edge.get("source_port") or "data", edge.get("target"), edge.get("target_port") or "data"
    source = edge.get("from") or {}
    target = edge.get("to") or {}
    return source.get("node"), source.get("port") or "data", target.get("node"), target.get("port") or "data"


def _freeze_market_sources(service, workflow, registry, run_id):
    """把一次已完成运行里的直连行情数据固化成工作区文件，供离线导出使用。

    直连行情卡片依赖平台运行时注入的交易所适配器，离线包无法执行它。导出「冻结运行」时，
    将每个 source.market 节点的 data 工件写成 CSV，并把节点替换成 source.csv；
    同时保存原始 source 报告并在 manifest 里登记来源。只有 data 端口可以这样替代，
    如果下游还引用了 source 报告端口，就明确拒绝而不是悄悄丢数据。
    """
    import copy

    report_path = service.root / "runs" / (identifier(run_id) + ".json")
    if not report_path.is_file():
        raise WorkflowError(f"找不到运行记录 {run_id}，无法导出冻结行情")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "completed":
        raise WorkflowError("这次运行没有成功完成，不能导出冻结行情")
    plan = compile_workflow(workflow, registry)
    if report.get("workflow_hash") and report["workflow_hash"] != plan["semantic_hash"]:
        raise WorkflowError("运行记录与要导出的工作流不一致；请从这次运行直接导出")
    flow = copy.deepcopy(workflow)
    nodes = {node["id"]: node for node in flow["nodes"]}
    used_ports = {(src, port) for src, port, _target, _tport in map(_edge_endpoints, flow.get("edges", []))}
    frozen: list[dict] = []
    for nid, node in list(nodes.items()):
        definition = registry.get(node["kind"], str(node["version"]))
        if (definition.get("capabilities") or {}).get("native") != "market_bars":
            continue
        record = (report.get("nodes") or {}).get(nid) or {}
        if (nid, "source") in used_ports:
            raise WorkflowError(
                f"「{definition['title']}」的来源报告端口已被下游使用，不能替换成冻结 CSV；"
                "请先断开该连接，或改用文件数据源"
            )
        ref = (record.get("outputs") or {}).get("data")
        if not ref:
            raise WorkflowError(f"运行记录里没有节点「{definition['title']}」的行情数据，无法冻结")
        rows = Runner(service.root, registry).read(ref)
        if not isinstance(rows, list) or not rows:
            raise WorkflowError(f"节点「{definition['title']}」的行情数据为空，无法冻结")
        import csv
        import hashlib
        import io

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        text = buffer.getvalue()
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        data_name = f"frozen-market-{digest}.csv"
        data_path = service.files / data_name
        if not data_path.is_file():
            data_path.write_text(text, encoding="utf-8")
        source_ref = (record.get("outputs") or {}).get("source")
        source_value = Runner(service.root, registry).read(source_ref) if source_ref else {}
        report_name = f"frozen-market-{digest}.json"
        source_report_path = service.files / report_name
        if not source_report_path.is_file():
            source_report_path.write_text(
                json.dumps(
                    {"node": nid, "kind": definition["kind"], "frozen_at": time.time(), "report": source_value},
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        node["kind"] = "source.csv"
        node["version"] = "1"
        node["params"] = {"file": data_name, "max_rows": len(rows)}
        resources = flow.setdefault("resources", [])
        for name in (data_name, report_name):
            if name not in resources:
                resources.append(name)
        frozen.append(
            {"node": nid, "file": data_name, "report": report_name, "rows": len(rows), "sha256": digest}
        )
    if frozen:
        compile_workflow(flow, registry)  # 替换后仍必须是合法工作流
    return flow, frozen


def handle_export(ctl):
    from .exporter import build_package

    service = WorkflowService(ctl.services)
    folder = service.root / "exports"
    folder.mkdir(exist_ok=True)
    name = identifier(ctl.run_id) + ".zip"
    ctl.progress(0, 1)
    registry = Registry(ctl.manifest["definitions"], frozen=True)
    workflow = ctl.manifest["workflow"]
    frozen = []
    if run_id := ctl.manifest.get("source_run_id"):
        # 导出「冻结运行」：直连行情卡片没有独立实现，用这次运行拿到的数据固化成本地 CSV
        workflow, frozen = _freeze_market_sources(service, workflow, registry, run_id)
    manifest = build_package(
        workflow,
        registry,
        service.files,
        folder / name,
        include_wheels=ctl.manifest.get("include_wheels", True),
        environment_info=ctl.manifest.get("execution_environment"),
        bundles_root=service.materialize_sources(ctl.run_id, ctl.manifest.get("source_snapshot", [])),
        extra_manifest={"frozen_sources": frozen} if frozen else None,
    )
    ctl.progress(1, 1)
    return {
        "download": f"/api/workflows/exports/{name}",
        "filename": name,
        "offline_complete": manifest["offline_complete"],
    }
