"""Immutable document delivery, authorization, hashes and cache-only retention."""

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "packages/agent-runtime/src"))
from agent_runtime import AgentRuntime, AgentSpec, ModelResult, RuntimeArtifacts, ToolCall, ToolSpec

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.local.runtime_artifacts import RETENTION_MS, prune, read_snapshot
from quant_agent.platform import context
from quant_agent.platform.runtime_adapters import QuantLabToolPort, RuntimeToolBinding
from quant_agent.workflows.contracts import WorkflowError


@pytest.fixture
def local(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("QUANTLAB_MODE", "local")
    monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    reset_services()
    services = AppServices.build(store_root=str(project / ".quantlab"))
    set_services(services)
    with TestClient(create_app()) as client:
        client.headers["X-Requested-With"] = "quantlab-test"
        me = client.get("/api/auth/me").json()
        with context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
            yield client, services, project, me
    reset_services()


def create_delivery(services, target, me):
    class Model:
        calls = 0

        def generate(self, messages, *, request_id):
            self.calls += 1
            if self.calls == 1:
                return ModelResult(tool_call=ToolCall("local_export_docx_file", {}), usage_micro=0)
            return ModelResult(text="delivered", usage_micro=0)

    def export(args, effect_id):
        target.write_bytes(b"original-document")
        return {"path": str(target), "format": "docx"}

    journal = services.runtime_bridge.runtime.journal
    model = Model()
    runtime = AgentRuntime(journal, model, QuantLabToolPort({"local_export_docx_file": RuntimeToolBinding(export)}))
    rid = runtime.start(
        "write",
        AgentSpec("writer", "1", tools=(ToolSpec("local_export_docx_file"),), metadata={"user_id": me["user"]["id"]}),
        tenant_id=me["workspace"]["id"],
    )
    assert runtime.resume(rid)["state"] == "COMPLETED"
    step = next(s for s in runtime.get(rid)["steps"] if s["kind"] == "tool")
    ref = RuntimeArtifacts(journal).list(rid, tenant_id=me["workspace"]["id"])[0]
    return runtime, rid, step["result"]["download_url"], ref, model


def test_historical_download_after_overwrite_hash_tampering_and_retention(local):
    client, services, project, me = local
    target = project / "report.docx"
    runtime, rid, url, ref, model = create_delivery(services, target, me)
    target.write_bytes(b"new-document")
    response = client.get(url)
    assert response.status_code == 200 and response.content == b"original-document"
    assert response.headers["cache-control"] == "no-store"
    assert runtime.resume(rid)["state"] == "COMPLETED" and model.calls == 2
    snapshot = services.local.home / "runtime-artifacts" / ref["key"]
    snapshot.write_bytes(b"corrupted")
    assert client.get(url).status_code >= 400
    snapshot.write_bytes(b"original-document")
    os.utime(snapshot, (1, 1))
    unknown = snapshot.parent / "keep.txt"
    unknown.write_text("not a cache artifact")
    assert prune(services.local, at=RETENTION_MS + 1001) == 1
    assert target.read_bytes() == b"new-document" and unknown.is_file()
    assert client.get(url).status_code >= 400
    assert RuntimeArtifacts(runtime.journal).list(rid, tenant_id=me["workspace"]["id"])[0]["id"] == ref["id"]
    assert model.calls == 2


def test_download_rechecks_revoked_source_and_run_tenant(local, tmp_path):
    client, services, _project, me = local
    external = tmp_path / "authorized"
    external.mkdir()
    services.local.grant(str(external), "write")
    runtime, rid, url, ref, model = create_delivery(services, external / "report.docx", me)
    assert client.get(url).status_code == 200
    services.local.revoke(str(external))
    assert client.get(url).status_code >= 400
    with pytest.raises(WorkflowError, match="授权"):
        read_snapshot(services.local, ref)
    with pytest.raises(KeyError):
        RuntimeArtifacts(runtime.journal).list(rid, tenant_id="other")
    assert client.get(url.rsplit("/", 1)[0] + "/" + "0" * 64).status_code == 404
    assert model.calls == 2


def test_real_pdf_pages_are_committed_as_downloadable_snapshot_images(local):
    import json

    fitz = pytest.importorskip("fitz")
    from quant_agent.workflows.agent import local_preview_document

    client, services, project, me = local
    source = project / "pages.pdf"
    with fitz.open() as pdf:
        for label in ("first", "second"):
            page = pdf.new_page()
            page.insert_text((40, 40), label)
        pdf.save(source)

    class Model:
        calls = 0

        def generate(self, messages, *, request_id):
            self.calls += 1
            if self.calls == 1:
                return ModelResult(tool_call=ToolCall("local_preview_document", {"path": str(source)}), usage_micro=0)
            assert json.loads(messages[-1]["content"])["rendered"]
            return ModelResult(text="reviewed", usage_micro=0)

    model = Model()
    journal = services.runtime_bridge.runtime.journal
    runtime = AgentRuntime(
        journal,
        model,
        QuantLabToolPort(
            {"local_preview_document": RuntimeToolBinding(lambda args, effect_id: local_preview_document(**args))}
        ),
    )
    rid = runtime.start(
        "preview",
        AgentSpec("reviewer", "1", tools=(ToolSpec("local_preview_document"),), metadata={"user_id": me["user"]["id"]}),
        tenant_id=me["workspace"]["id"],
    )
    assert runtime.resume(rid)["state"] == "COMPLETED"
    refs = RuntimeArtifacts(journal).list(rid, tenant_id=me["workspace"]["id"])
    assert len(refs) == 3
    result = runtime.get(rid)["steps"][1]["result"]
    for page in result["pages"]:
        response = client.get(page["url"])
        assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    source.write_bytes(b"overwritten")
    assert client.get(result["download_url"]).content.startswith(b"%PDF")
    assert model.calls == 2
