"""Portable DAG execution with immutable artifacts, targeted replay and cache."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .contracts import (
    WorkflowError,
    canonical,
    compile_workflow,
    fingerprint,
    identifier,
    params_for,
    subflow_instance,
    validate_value,
)


class CardExecutionError(WorkflowError):
    def __init__(self, result):
        super().__init__(result.get("error", "卡片失败"))
        self.result = result


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + uuid.uuid4().hex[:12] + ".tmp")
    tmp.write_text(canonical(value), encoding="utf-8")
    os.replace(tmp, path)


class Runner:
    def __init__(
        self,
        root,
        registry,
        *,
        files=None,
        tick=None,
        event=None,
        bundles_root=None,
        environment_info=None,
        native=None,
    ):
        self.root = Path(root).resolve()
        self.files = Path(files or self.root / "files").resolve()
        self.registry = registry
        self.tick = tick or (lambda: None)
        self.event = event or (lambda value: None)
        self.bundles_root = bundles_root
        self._environment = environment_info
        # 平台内执行的原生卡片（例如直连行情）：kind → handler(params, tick=, log=)
        self.native = dict(native or {})
        self.files.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def environment(self):
        if self._environment is None:
            from .environments import EnvironmentManager

            self._environment = EnvironmentManager(self.root).info()
        return self._environment

    def artifact(self, value, port):
        validate_value(value, port.get("schema", {}), "输出")
        body = {"type": port["type"], "role": port.get("role"), "value": value}
        if port.get("semantics"):
            body["semantics"] = port["semantics"]
        digest = fingerprint(body)
        path = self.root / "artifacts" / (digest + ".json")
        if not path.exists():
            atomic_json(path, body)
        return {
            "hash": digest,
            "type": body["type"],
            "role": body["role"],
            "bytes": path.stat().st_size,
            **({"semantics": body["semantics"]} if body.get("semantics") else {}),
        }

    def read(self, ref):
        digest = ref.get("hash", "")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise WorkflowError("非法工件引用")
        path = self.root / "artifacts" / (digest + ".json")
        if not path.is_file():
            raise WorkflowError("本工作区没有该工件")
        body = json.loads(path.read_text(encoding="utf-8"))
        if fingerprint(body) != digest:
            raise WorkflowError("工件内容校验失败")

        def verify_files(value):
            if isinstance(value, dict):
                if (
                    isinstance(value.get("file"), str)
                    and isinstance(value.get("sha256"), str)
                    and len(value["sha256"]) == 64
                    and all(c in "0123456789abcdef" for c in value["sha256"])
                ):
                    binary = (self.files / value["file"]).resolve()
                    if not binary.is_relative_to(self.files) or not binary.is_file():
                        raise WorkflowError("二进制工件缺失或不在本工作区")
                    h = hashlib.sha256()
                    with binary.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            h.update(chunk)
                    if h.hexdigest() != value["sha256"]:
                        raise WorkflowError("二进制工件内容校验失败")
                else:
                    for child in value.values():
                        verify_files(child)
            elif isinstance(value, list):
                for child in value:
                    verify_files(child)

        verify_files(body["value"])
        return body["value"]

    def _file_hashes(self, definition, params, *, hash_contents=True):
        hashes = {}
        for key in definition.get("file_params", []):
            name = params.get(key)
            if not name:
                continue
            from .files import resolve_input

            path = resolve_input(self.files, name, verify=hash_contents)
            if not hash_contents:
                continue
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
                    self.tick()
            hashes[key] = h.hexdigest()
        return hashes

    def _execute(self, definition, inputs, params, seed, timeout):
        from .environments import requirement_status
        from .files import references, resolve_input

        # 平台原生卡片（capabilities.native）：在进程内调用平台服务，不进入子进程。
        # 这样直连行情能复用交易所适配器与缓存；独立运行包没有该能力时会明确失败。
        native_key = (definition.get("capabilities") or {}).get("native")
        if native_key:
            handler = self.native.get(native_key)
            if handler is None:
                raise WorkflowError(
                    f"「{definition.get('title') or definition['kind']}」需要平台的 {native_key} 能力；"
                    "离线或独立运行不支持直连操作，请先改用文件数据源冻结数据"
                )
            logs: list[str] = []

            def log(message: str) -> None:
                if len(logs) < 100:
                    logs.append(str(message)[:2000])

            try:
                outputs = handler(params, tick=self.tick, log=log)
            except WorkflowError:
                raise
            except Exception as exc:  # 网络/交易所/缓存错误统一转成卡片可读错误
                raise WorkflowError(f"{definition.get('title') or definition['kind']}执行失败：{exc}") from exc
            if not isinstance(outputs, dict):
                raise WorkflowError("原生卡片必须返回具名输出")
            return {"ok": True, "outputs": outputs, "logs": logs, "stdout": ""}

        info = self.environment()
        dependency_paths = list(info["paths"])
        from .bundles import sources

        dependency_paths.extend(b["parent"] for b in sources(definition.get("bundled_modules", []), self.bundles_root))
        for requirement in definition.get("requirements", []):
            requirement_status(info, requirement)
        with tempfile.TemporaryDirectory(prefix="card-", dir=self.root) as work:
            request = Path(work) / "request.json"
            output = Path(work) / "output.json"
            atomic_json(
                request,
                {
                    "code": definition["code"],
                    "inputs": inputs,
                    "params": params,
                    "files": str(self.files),
                    "work": work,
                    "seed": seed,
                    "dependency_paths": dependency_paths,
                    "input_files": {
                        name: str(resolve_input(self.files, name))
                        for name in references(self.files)
                        if name in params.values()
                    },
                },
            )
            # Do not forward service/API credentials into custom code.
            env = {
                k: v
                for k, v in os.environ.items()
                if k.upper()
                in (
                    "SYSTEMROOT",
                    "WINDIR",
                    "PATH",
                    "TEMP",
                    "TMP",
                    "LANG",
                    "LC_ALL",
                    "VIRTUAL_ENV",
                )
            }
            env.update(PYTHONIOENCODING="utf-8", PYTHONHASHSEED=str(seed), OMP_NUM_THREADS="1")
            proc = subprocess.Popen(
                [
                    info["python"],
                    "-E",
                    "-P",
                    str(Path(__file__).with_name("node_child.py")),
                    str(request),
                    str(output),
                ],
                cwd=work,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            start = time.monotonic()
            try:
                while proc.poll() is None:
                    self.tick()
                    if time.monotonic() - start > timeout:
                        raise WorkflowError("卡片执行超时")
                    time.sleep(0.05)
                if not output.exists():
                    raise WorkflowError(f"卡片进程退出，状态{proc.returncode}")
                result = json.loads(output.read_text(encoding="utf-8"))
                if not result["ok"]:
                    raise CardExecutionError(result)
                return result
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

    def run(
        self,
        workflow,
        *,
        run_id=None,
        target=None,
        pins=None,
        mode="research",
        external=None,
    ):
        if pins and mode != "debug":
            raise WorkflowError("固定输出仅可用于调试，正式实验不接受固定输出")
        plan = compile_workflow(workflow, self.registry, target=target, external=external)
        rid = identifier(run_id or "wf-" + uuid.uuid4().hex[:14])
        report = {
            "id": rid,
            "format": "quantlab.workflow-run/1",
            "status": "running",
            "mode": mode,
            "workflow_hash": plan["semantic_hash"],
            "workflow": copy.deepcopy(workflow),
            "target": target,
            "started_at": time.time(),
            "nodes": {},
            "definition_hashes": {k: v["code_hash"] for k, v in plan["definitions"].items()},
            "environment": self.environment(),
        }
        path = self.root / "runs" / (rid + ".json")

        def persist():
            atomic_json(path, report)

        persist()
        canceled = None
        for index, nid in enumerate(plan["order"]):
            definition = plan["definitions"][nid]
            node = plan["nodes"][nid]
            record = {
                "status": "running",
                "started_at": time.time(),
                "outputs": {},
                "code_hash": definition["code_hash"],
            }
            report["nodes"][nid] = record
            persist()
            try:
                self.tick()
                refs = dict((external or {}).get(nid, {}))
                for port, edge in plan["incoming"][nid].items():
                    parent = report["nodes"].get(edge["source"], {})
                    if parent.get("status") not in ("completed", "cached", "pinned"):
                        raise WorkflowError("上游未完成，当前分支被阻断")
                    refs[port] = parent["outputs"][edge["source_port"]]
                inputs = {port: self.read(ref) for port, ref in refs.items()}
                params = params_for(node, definition)
                for name, binding in node.get("bindings", {}).items():
                    value = inputs[binding["input"]]
                    for key in binding.get("path", []):
                        value = value[
                            int(key) if isinstance(value, list) and isinstance(key, str) and key.isdecimal() else key
                        ]
                    params[name] = value
                validate_value(params, definition["parameters"], nid)
                for port, value in inputs.items():
                    validate_value(value, definition["inputs"][port].get("schema", {}), port)
                seed = int(workflow.get("settings", {}).get("seed", 7))
                context = {
                    "python": self.environment()["version"],
                    "interpreter": self.environment()["python"],
                    "platform": self.environment()["platform"],
                    "requirements": definition.get("requirements", []),
                }
                from .bundles import sources

                context["source_bundles"] = {
                    f["path"]: f["sha256"]
                    for b in sources(definition.get("bundled_modules", []), self.bundles_root)
                    for f in b["files"]
                }

                for requirement in definition.get("requirements", []):
                    import re

                    package = re.split(r"[\[<>=!~]", requirement)[0]
                    from .environments import requirement_status

                    context[package] = requirement_status(self.environment(), requirement)
                file_hashes = self._file_hashes(definition, params)
                resources = workflow.get("resources", [])
                file_hashes.update(self._file_hashes({"file_params": resources}, {name: name for name in resources}))
                key = fingerprint(
                    {
                        "code": definition["code_hash"],
                        "inputs": refs,
                        "params": params,
                        "seed": seed,
                        "files": file_hashes,
                        "environment": context,
                    }
                )
                cache_path = self.root / "cache" / (key + ".json")
                record.update(inputs=refs, params=params, cache_key=key, file_hashes=file_hashes, environment=context)
                if (pins or {}).get(nid) is not None:
                    outputs = {port: self.read(ref) for port, ref in pins[nid].items()}
                    record["status"] = "pinned"
                elif (
                    cache_path.exists()
                    and not node.get("force")
                    and not definition.get("subflow")
                    and (definition.get("capabilities") or {}).get("cache", True)
                ):
                    cached = json.loads(cache_path.read_text(encoding="utf-8"))
                    for ref in cached.values():
                        self.read(ref)
                    record.update(status="cached", outputs=cached)
                    outputs = None
                elif definition.get("subflow"):
                    sub = definition["subflow"]
                    child_external = {}
                    for name, endpoint in sub["inputs"].items():
                        child_external.setdefault(endpoint["node"], {})[endpoint["port"]] = refs[name]
                    child_flow = subflow_instance(definition, params)
                    if node.get("force"):
                        for child_node in child_flow["nodes"]:
                            child_node["force"] = True
                    child_flow.setdefault("settings", {})["seed"] = seed
                    child = self.run(child_flow, external=child_external, mode=mode)
                    if child["status"] != "completed":
                        raise WorkflowError("子流程未完成：" + child["id"])
                    outputs = {
                        name: self.read(child["nodes"][endpoint["node"]]["outputs"][endpoint["port"]])
                        for name, endpoint in sub["outputs"].items()
                    }
                    record["child_run_id"] = child["id"]
                    record["child_cache"] = {
                        "cached": sum(n["status"] == "cached" for n in child["nodes"].values()),
                        "total": len(child["nodes"]),
                    }
                    if child["nodes"] and all(n["status"] == "cached" for n in child["nodes"].values()):
                        record["status"] = "cached"
                else:
                    result = self._execute(
                        definition,
                        inputs,
                        params,
                        seed,
                        max(1, min(3600, int(node.get("timeout_seconds", 300)))),
                    )
                    outputs = result["outputs"]
                    record.update(logs=result.get("logs", []), stdout=result.get("stdout", ""))
                if outputs is not None:
                    if not isinstance(outputs, dict) or set(outputs) != set(definition["outputs"]):
                        raise WorkflowError("实际输出必须与声明的具名输出完全一致")
                    record["outputs"] = {
                        port: self.artifact(value, definition["outputs"][port]) for port, value in outputs.items()
                    }
                    if record["status"] != "pinned":
                        if record["status"] != "cached":
                            record["status"] = "completed"
                        if (definition.get("capabilities") or {}).get("cache", True):
                            atomic_json(cache_path, record["outputs"])
            except Exception as exc:
                if isinstance(exc, CardExecutionError):
                    record.update(
                        {k: exc.result.get(k, "" if k != "logs" else []) for k in ("logs", "stdout", "traceback")}
                    )
                if exc.__class__.__name__ in ("Canceled", "LeaseLost"):
                    canceled = exc
                    record.update(status="canceled", error=str(exc))
                else:
                    record.update(
                        status="blocked" if str(exc).startswith("上游未完成") else "failed",
                        error=str(exc),
                    )
            record["finished_at"] = time.time()
            persist()
            self.event(
                {
                    "run_id": rid,
                    "node_id": nid,
                    "status": record["status"],
                    "done": index + 1,
                    "total": len(plan["order"]),
                }
            )
            if canceled:
                break
        report["status"] = (
            "canceled"
            if canceled
            else (
                "completed"
                if all(n["status"] in ("completed", "cached", "pinned") for n in report["nodes"].values())
                else "failed"
            )
        )
        report["finished_at"] = time.time()
        persist()
        if canceled:
            raise canceled
        return report
