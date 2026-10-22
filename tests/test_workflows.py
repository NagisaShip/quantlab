"""Behavioral contracts for reusable function workflows and standalone exports."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import zipfile

import pytest

from quant_agent.workflows.contracts import (
    FORMAT,
    WorkflowError,
    compile_workflow,
    parse_definition,
)
from quant_agent.workflows.exporter import build_package
from quant_agent.workflows.registry import Registry
from quant_agent.workflows.runtime import Runner
from quant_agent.workflows.templates import classification, csv_research


def workspace(tmp_path):
    files = tmp_path / "files"
    files.mkdir()
    (files / "input.csv").write_text(
        "id,year,feature,label\n000001,2020,-2,0\n000002,2021,2,1\n000003,2022,-1,0\n000004,2023,1,1\n000005,2024,-3,0\n000006,2024,3,1\n",
        encoding="utf-8",
    )
    return Runner(tmp_path / "runtime", Registry(), files=files)


def test_multifile_function_snapshot_and_standalone_export(tmp_path, monkeypatch):
    package = tmp_path / "research_test_bundle"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text("def value():\n    return 41\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = Registry()
    code = """NODE={"kind":"user.bundle","version":"1","title":"Bundle","inputs":{},"outputs":{"value":{"type":"json"}},"parameters":{"type":"object","properties":{}},"requirements":[],"bundled_modules":["research_test_bundle"]}
def execute(ctx, inputs, params):
    from research_test_bundle import value
    return {"value":value()}
"""
    registry.add(code)
    from quant_agent.workflows.bundles import sources

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for bundle in sources(["research_test_bundle"]):
        for file in bundle["files"]:
            p = snapshot / file["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(file["code"], encoding="utf-8")
    source.write_text("def value():\n    return 99\n", encoding="utf-8")
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "node", "kind": "user.bundle", "version": "1", "params": {}}],
        "edges": [],
    }
    runner = Runner(tmp_path / "run", registry, bundles_root=snapshot)
    report = runner.run(flow)
    assert runner.read(report["nodes"]["node"]["outputs"]["value"]) == 41
    archive = tmp_path / "package.zip"
    build_package(flow, registry, runner.files, archive, bundles_root=snapshot)
    with zipfile.ZipFile(archive) as z:
        z.extractall(tmp_path / "package")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(
        [sys.executable, str(tmp_path / "package/run.py")], env=env, capture_output=True, text=True, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    saved = json.loads(result.stdout)
    reference = saved["nodes"]["node"]["outputs"]["value"]
    content = json.loads(
        (tmp_path / "package/outputs/artifacts" / (reference["hash"] + ".json")).read_text(encoding="utf-8")
    )
    assert content["value"] == 41


def test_binary_artifact_is_materialized_and_tampering_is_detected(tmp_path):
    runner = workspace(tmp_path)
    code = """NODE={"kind":"user.binary","version":"1","title":"Binary","inputs":{},"outputs":{"file":{"type":"json"}},"parameters":{"type":"object","properties":{}},"requirements":[]}
def execute(ctx, inputs, params):
    from pathlib import Path
    p=Path(ctx.work)/"numbers.txt"
    p.write_text("000001",encoding="utf-8")
    return {"file":ctx.save_file(p)}
"""
    runner.registry.add(code)
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "node", "kind": "user.binary", "version": "1", "params": {}}],
        "edges": [],
    }
    report = runner.run(flow)
    reference = report["nodes"]["node"]["outputs"]["file"]
    binary = runner.read(reference)
    path = runner.files / binary["file"]
    assert path.read_text() == "000001"
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(WorkflowError, match="二进制工件内容校验失败"):
        runner.read(reference)


def test_dependency_version_range_is_enforced():
    from quant_agent.workflows.contracts import installed_requirement

    assert installed_requirement("numpy>=1.26")
    with pytest.raises(WorkflowError, match="不满足"):
        installed_requirement("numpy>=999")


def test_failed_custom_card_keeps_diagnostic_logs(tmp_path):
    runner = workspace(tmp_path)
    code = """NODE={"kind":"user.fails","version":"1","title":"Failure","inputs":{},"outputs":{"out":{"type":"json"}},"parameters":{"type":"object","properties":{}},"requirements":[]}
def execute(ctx, inputs, params):
    ctx.log("before failure")
    print("captured diagnostic")
    raise ValueError("expected failure")
"""
    runner.registry.add(code)
    flow = {"format": FORMAT, "nodes": [{"id": "bad", "kind": "user.fails", "version": "1", "params": {}}], "edges": []}
    result = runner.run(flow)
    assert result["nodes"]["bad"]["logs"] == ["before failure"]
    assert "captured diagnostic" in result["nodes"]["bad"]["stdout"]
    assert "ValueError" in result["nodes"]["bad"]["traceback"]


def test_explicit_auxiliary_resource_changes_invalidate_cache(tmp_path):
    runner = workspace(tmp_path)
    resource = runner.files / "extra.txt"
    resource.write_text("old", encoding="utf-8")
    code = """NODE={"kind":"user.resource","version":"1","title":"Resource","inputs":{},"outputs":{"out":{"type":"json"}},"parameters":{"type":"object","properties":{}},"requirements":[]}
def execute(ctx, inputs, params):
    return {"out":ctx.file("extra.txt").read_text(encoding="utf-8")}
"""
    runner.registry.add(code)
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "read", "kind": "user.resource", "version": "1", "params": {}}],
        "edges": [],
        "resources": ["extra.txt"],
    }
    first = runner.run(flow)
    assert first["status"] == "completed"
    assert runner.run(flow)["nodes"]["read"]["status"] == "cached"
    resource.write_text("new", encoding="utf-8")
    latest = runner.run(flow)
    assert latest["nodes"]["read"]["status"] == "completed"
    assert runner.read(latest["nodes"]["read"]["outputs"]["out"]) == "new"


def test_composite_dispatch_source_cannot_silently_ignore_edited_execute():
    from quant_agent.workflows.subflows import encapsulate

    code, _ = encapsulate(csv_research(), ["check", "summary"], "user.compound", "统计", Registry())
    parse_definition(code)
    with pytest.raises(WorkflowError, match="标准分派函数"):
        parse_definition(code.replace("return ctx.run_subflow(inputs, params)", "return {}"))


def test_real_csv_pipeline_and_cache_respects_code_data_but_not_layout(tmp_path):
    runner = workspace(tmp_path)
    flow = csv_research()
    first = runner.run(flow)
    assert first["status"] == "completed"
    data = runner.read(first["nodes"]["source"]["outputs"]["data"])
    assert data[0]["id"] == "000001"
    changed = copy.deepcopy(flow)
    changed["nodes"][0]["position"] = {"x": 900, "y": 600}
    changed["nodes"][0]["flipped"] = True
    second = runner.run(changed)
    assert all(n["status"] == "cached" for n in second["nodes"].values())
    assert first["workflow_hash"] == second["workflow_hash"]
    changed["nodes"][-1]["params"]["title"] = "changed"
    third = runner.run(changed)
    assert third["nodes"]["source"]["status"] == "cached"
    assert third["nodes"]["report"]["status"] == "completed"
    (runner.files / "input.csv").write_text("id,year,feature,label\n000008,2024,4,1\n", encoding="utf-8")
    fourth = runner.run(changed)
    assert fourth["nodes"]["source"]["status"] == "completed"


def test_compile_rejects_cycles_missing_ports_and_test_labels_in_training():
    registry = Registry()
    flow = csv_research()
    flow["edges"].append(
        {
            "source": "check",
            "source_port": "data",
            "target": "check",
            "target_port": "data",
        }
    )
    with pytest.raises(WorkflowError):
        compile_workflow(flow, registry)
    flow = classification()
    flow["edges"][1]["source_port"] = "test"
    with pytest.raises(WorkflowError, match="用途"):
        compile_workflow(flow, registry)
    flow = csv_research()
    flow["edges"][0]["source_port"] = "nonexistent"
    with pytest.raises(WorkflowError, match="端口"):
        compile_workflow(flow, registry)


def test_output_pinning_is_debug_only_and_has_provenance(tmp_path):
    runner = workspace(tmp_path)
    flow = csv_research()
    first = runner.run(flow)
    pins = {"source": first["nodes"]["source"]["outputs"]}
    with pytest.raises(WorkflowError, match="调试"):
        runner.run(flow, pins=pins)
    debug = runner.run(flow, pins=pins, mode="debug")
    assert debug["mode"] == "debug" and debug["nodes"]["source"]["status"] == "pinned"


def test_targeted_run_does_not_execute_downstream(tmp_path):
    runner = workspace(tmp_path)
    result = runner.run(csv_research(), target="check")
    assert set(result["nodes"]) == {"source", "check"}


def test_failure_blocks_dependents_and_does_not_hide_independent_branch(tmp_path):
    runner = workspace(tmp_path)
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = "missing.csv"
    flow["nodes"].append(
        {
            "id": "independent",
            "kind": "config.value",
            "version": "1",
            "params": {"value": {"seed": 9}},
        }
    )
    result = runner.run(flow)
    assert result["status"] == "failed"
    assert result["nodes"]["source"]["status"] == "failed"
    assert result["nodes"]["check"]["status"] == "blocked"
    assert result["nodes"]["independent"]["status"] == "completed"


def test_registered_versions_are_immutable_and_code_is_not_imported_for_discovery(
    tmp_path,
):
    registry = Registry()
    d = registry.get("config.value", "1")
    code = d["code"].replace('"config.value"', '"user.config"')
    registry.add(code)
    with pytest.raises(WorkflowError, match="不可覆盖"):
        registry.add(code + "\n# changed\n")
    with pytest.raises(WorkflowError):
        parse_definition("print('not executed')")
    assert registry.get("user.config", "1")["code"] == code


def test_scalar_parameter_binding_is_resolved_and_validated(tmp_path):
    runner = workspace(tmp_path)
    flow = csv_research()
    code = """NODE={"kind":"user.bound","version":"1","title":"Bound","inputs":{"config":{"type":"json"}},"outputs":{"value":{"type":"json"}},"parameters":{"type":"object","properties":{"number":{"type":"integer"}},"required":["number"],"additionalProperties":False},"requirements":[]}
def execute(ctx, inputs, params):
    return {"value":params["number"]*2}
"""
    runner.registry.add(code)
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "a",
                "kind": "config.value",
                "version": "1",
                "params": {"value": {"n": 4}},
            },
            {
                "id": "b",
                "kind": "user.bound",
                "version": "1",
                "params": {},
                "bindings": {"number": {"input": "config", "path": ["n"]}},
            },
        ],
        "edges": [
            {
                "source": "a",
                "source_port": "config",
                "target": "b",
                "target_port": "config",
            }
        ],
    }
    result = runner.run(flow)
    assert runner.read(result["nodes"]["b"]["outputs"]["value"]) == 8
    flow["nodes"][0]["params"]["value"]["n"] = "oops"
    assert runner.run(flow)["nodes"]["b"]["status"] == "failed"


def test_output_signature_and_corrupted_artifacts_are_rejected(tmp_path):
    runner = workspace(tmp_path)
    code = (
        runner.registry.get("config.value", "1")["code"]
        .replace('"config.value"', '"user.bad"')
        .replace('return {"config":params["value"]}', 'return {"wrong":params["value"]}')
        .replace('return {"config": params["value"]}', 'return {"wrong": params["value"]}')
    )
    runner.registry.add(code)
    result = runner.run(
        {
            "format": FORMAT,
            "nodes": [{"id": "bad", "kind": "user.bad", "version": "1", "params": {}}],
            "edges": [],
        }
    )
    assert result["status"] == "failed"
    ref = runner.artifact({"x": 1}, {"type": "json"})
    (runner.root / "artifacts" / (ref["hash"] + ".json")).write_text('{"tampered":true}')
    with pytest.raises(WorkflowError, match="校验"):
        runner.read(ref)


def test_standalone_export_runs_without_platform_or_network(tmp_path):
    runner = workspace(tmp_path)
    package = tmp_path / "standalone.zip"
    manifest = build_package(csv_research(), runner.registry, runner.files, package)
    assert manifest["offline_complete"] is True
    extract = tmp_path / "export"
    extract.mkdir()
    with zipfile.ZipFile(package) as z:
        assert "requirements.lock" in z.namelist() and "workflow_runtime/runtime.py" in z.namelist()
        assert not any(n.startswith("quant_agent/") for n in z.namelist())
        z.extractall(extract)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHONPATH", "QAGENT_"))}
    proc = subprocess.run(
        [sys.executable, str(extract / "run.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "completed"
    assert not (extract / "quant_agent").exists()


def test_export_rejects_missing_resources_and_unbundled_dependencies(tmp_path):
    runner = workspace(tmp_path)
    with pytest.raises(WorkflowError):
        build_package(
            classification(),
            runner.registry,
            runner.files,
            tmp_path / "bad.zip",
            include_wheels=False,
        )
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = "../secret.csv"
    with pytest.raises(WorkflowError):
        build_package(flow, runner.registry, runner.files, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_classification_runs_without_touching_test_during_training(tmp_path):
    runner = workspace(tmp_path)
    flow = classification()
    flow["nodes"][2]["params"]["epochs"] = 20
    report = runner.run(flow)
    assert report["status"] == "completed"
    training = report["nodes"]["train"]
    assert set(training["inputs"]) == {"train", "validation"}
    metrics = runner.read(report["nodes"]["evaluate"]["outputs"]["report"])
    assert metrics["samples"] == 2 and metrics["roc_auc"] == 1


def test_single_class_auc_unavailable(tmp_path):
    runner = workspace(tmp_path)
    ref = runner.artifact(
        [{"label": 1, "prediction": 1, "probability": 0.9}],
        {"type": "predictions", "role": "test"},
    )
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "e", "kind": "classification.evaluate", "version": "1", "params": {}}],
        "edges": [],
    }
    report = runner.run(flow, external={"e": {"predictions": ref}})
    assert runner.read(report["nodes"]["e"]["outputs"]["report"])["roc_auc"] is None


def test_subflow_encapsulation_expansion_and_source_refresh(tmp_path):
    from quant_agent.workflows.subflows import encapsulate, expand

    runner = workspace(tmp_path)
    flow = csv_research()
    expected = runner.run(flow)
    code, wrapped = encapsulate(flow, ["check", "summary"], "user.preparation", "数据准备", runner.registry)
    runner.registry.add(code)
    actual = runner.run(wrapped)
    assert actual["status"] == "completed"
    assert actual["nodes"]["report"]["outputs"] == expected["nodes"]["report"]["outputs"]
    composite = next(n for n in wrapped["nodes"] if n["kind"] == "user.preparation")
    expanded = expand(wrapped, composite["id"], runner.registry)
    assert runner.run(expanded)["nodes"]["report"]["outputs"] == expected["nodes"]["report"]["outputs"]
    code2, with_source = encapsulate(
        flow,
        ["source", "check", "summary"],
        "user.source_group",
        "数据准备",
        runner.registry,
    )
    runner.registry.add(code2)
    first = runner.run(with_source)
    (runner.files / "input.csv").write_text("id,value\n000999,2\n", encoding="utf-8")
    refreshed = runner.run(with_source)
    assert first["nodes"]["report"]["outputs"] != refreshed["nodes"]["report"]["outputs"]
