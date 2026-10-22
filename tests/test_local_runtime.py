"""受管 Python 运行时诊断：本地模式探测解释器与依赖，非本地部署 404。"""

from __future__ import annotations

import importlib
import json
import platform
import sys
from pathlib import Path

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.local import runtime as local_runtime
from quant_agent.platform.errors import ErrorCode


@pytest.fixture()
def local_client(tmp_path, monkeypatch):
    project = tmp_path / "研究项目"
    project.mkdir()
    monkeypatch.setenv("QUANTLAB_MODE", "local")
    monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
    monkeypatch.setenv("QUANTLAB_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("QUANTLAB_CREDENTIALS", str(tmp_path / "keys"))
    reset_services()
    local_runtime.reset_cache()
    set_services(AppServices.build(store_root=str(project / ".quantlab")))
    with TestClient(create_app()) as c:
        c.headers["X-Requested-With"] = "quantlab-test"
        yield c, project
    reset_services()
    local_runtime.reset_cache()


def _account_client(tmp_path, monkeypatch, *, authenticated: bool):
    for env in ("QUANTLAB_MODE", "QUANTLAB_PROJECT", "QUANTLAB_LIBRARY", "QUANTLAB_CREDENTIALS"):
        monkeypatch.delenv(env, raising=False)
    reset_services()
    set_services(AppServices.build(store_root=str(tmp_path)))
    c = TestClient(create_app())
    c.__enter__()
    if authenticated:
        login(c)
    return c


def _close(c) -> None:
    c.__exit__(None, None, None)
    reset_services()


# ---------------------------------------------------------------------------
# GET /api/local/runtime：真实探测当前解释器与受管依赖
# ---------------------------------------------------------------------------
def test_runtime_endpoint_reports_real_interpreter(local_client):
    c, _project = local_client
    response = c.get("/api/local/runtime")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["mode"] == "local"
    manifest = body["manifest"]
    assert manifest["format"] == "quantlab.local-runtime/1" and manifest["managed"] is True
    assert manifest["required"] == list(local_runtime.MANAGED_DEPENDENCIES)
    assert any("不创建虚拟环境" in line for line in manifest["limitations"])
    assert any("不是完整打包" in line for line in manifest["limitations"])

    interp = body["interpreter"]
    assert interp["version"] == platform.python_version()
    assert interp["version_info"] == list(sys.version_info[:3])
    assert interp["implementation"] == platform.python_implementation()
    assert interp["platform"] == sys.platform
    assert interp["executable"] == sys.executable
    assert Path(interp["executable"]).is_file()
    assert isinstance(interp["venv"], bool) and isinstance(interp["is_64bit"], bool)
    assert interp["prefix"] and interp["base_prefix"]

    # 依赖可用性必须与当前进程真实 import 结果一致（python-docx 的模块名是 docx）
    deps = body["dependencies"]
    assert set(deps) == set(local_runtime.MANAGED_DEPENDENCIES)
    for distribution, module_name in local_runtime.MANAGED_DEPENDENCIES.items():
        row = deps[distribution]
        assert row["module"] == module_name
        try:
            module = importlib.import_module(module_name)
        except Exception:
            assert row["available"] is False, (distribution, row)
            assert row["error"]
        else:
            assert row["available"] is True, (distribution, row)
            assert row["version"] == local_runtime.module_version(module)

    summary = body["summary"]
    assert summary["ok"] is (not summary["missing"])
    assert set(summary["available"]) | set(summary["missing"]) == set(deps)
    assert summary["version"] == platform.python_version()
    assert summary["executable"] == sys.executable


def test_runtime_probe_does_not_leak_environment_secrets(local_client, monkeypatch):
    c, _project = local_client
    monkeypatch.setenv("OPENAI_API_KEY", "sk-runtime-secret-value")
    response = c.get("/api/local/runtime", params={"refresh": 1})
    assert response.status_code == 200, response.text
    assert "sk-runtime-secret-value" not in response.text


def test_runtime_endpoint_is_read_only(local_client):
    c, _project = local_client
    assert c.post("/api/local/runtime", json={}).status_code == 405


# ---------------------------------------------------------------------------
# /api/local/info：保留原字段，追加运行时摘要
# ---------------------------------------------------------------------------
def test_info_keeps_existing_fields_and_adds_runtime_summary(local_client):
    c, project = local_client
    response = c.get("/api/local/info")
    assert response.status_code == 200, response.text
    body = response.json()
    for key in (
        "mode",
        "first_run",
        "project",
        "root",
        "home",
        "permissions",
        "disabled_pages",
        "account_features",
        "upload_byte_limit",
        "preferences",
        "interpretation",
    ):
        assert key in body, key
    assert body["mode"] == "local"
    assert Path(body["root"]) == project.resolve()

    runtime = body["runtime"]
    assert runtime["version"] == platform.python_version()
    assert runtime["implementation"] == platform.python_implementation()
    assert runtime["executable"] == sys.executable
    assert set(runtime["dependencies"]) == set(local_runtime.MANAGED_DEPENDENCIES)
    assert runtime["ok"] is (not runtime["missing"])
    # info 里是摘要，不是完整探测快照
    assert "interpreter" not in runtime and "manifest" not in runtime


# ---------------------------------------------------------------------------
# 非本地部署：404（未登录也不先暴露 401）
# ---------------------------------------------------------------------------
def test_runtime_endpoint_is_404_outside_local_mode(tmp_path, monkeypatch):
    anonymous = _account_client(tmp_path, monkeypatch, authenticated=False)
    try:
        assert anonymous.get("/api/local/runtime").status_code == 404
    finally:
        _close(anonymous)

    authenticated = _account_client(tmp_path / "account", monkeypatch, authenticated=True)
    try:
        response = authenticated.get("/api/local/runtime")
        assert response.status_code == 404
        assert response.json()["code"] == ErrorCode.NOT_FOUND
        # 现有 /api/local/* 在账号模式仍是原来的 400，不被本次改动影响
        assert authenticated.get("/api/local/info").status_code == 400
    finally:
        _close(authenticated)


# ---------------------------------------------------------------------------
# 探测缓存：/api/local/info 高频读取时复用结果，refresh / reset_cache 可更新
# ---------------------------------------------------------------------------
def test_dependency_probe_is_cached_and_can_be_refreshed(monkeypatch):
    calls: list[str] = []
    original = local_runtime._probe_dependency

    def counting(distribution: str):
        calls.append(distribution)
        return original(distribution)

    monkeypatch.setattr(local_runtime, "_probe_dependency", counting)
    local_runtime.reset_cache()
    try:
        first = local_runtime.dependency_status()
        assert len(calls) == len(local_runtime.MANAGED_DEPENDENCIES)
        second = local_runtime.dependency_status()
        assert second == first and len(calls) == len(local_runtime.MANAGED_DEPENDENCIES)
        local_runtime.dependency_status(use_cache=False)
        assert len(calls) == 2 * len(local_runtime.MANAGED_DEPENDENCIES)
    finally:
        local_runtime.reset_cache()


def test_probe_and_manifest_are_json_serializable(local_client):
    c, _project = local_client
    body = c.get("/api/local/runtime").json()
    # 整个诊断快照必须能被桌面端 / 诊断脚本直接写盘
    json.dumps(body)
    assert body["manifest"]["generated_at"] > 0
    assert body["manifest"]["mode"] == "local"
