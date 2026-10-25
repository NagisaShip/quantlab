"""Artifact identity belongs to committed Steps, not a mutable host file path."""

import hashlib
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from agent_runtime import (
    AgentRuntime,
    AgentSpec,
    ArtifactRef,
    Journal,
    ModelResult,
    RuntimeArtifacts,
    ToolCall,
    ToolSpec,
)
from agent_runtime.artifacts import FIELD


class Model:
    def generate(self, messages, *, request_id):
        if any(m["role"] == "tool" for m in messages):
            return ModelResult(text="done", usage_micro=0)
        return ModelResult(tool_call=ToolCall("write", {}), usage_micro=0)


class Tools:
    calls = 0

    def execute(self, call, *, effect_id):
        self.calls += 1
        return {
            FIELD: [
                asdict(
                    ArtifactRef(
                        key="blob-v1",
                        sha256=hashlib.sha256(b"v1").hexdigest(),
                        bytes=2,
                        name="report.pdf",
                        media_type="application/pdf",
                        expires_at=0,
                    )
                )
            ]
        }


def test_artifacts_reopen_identity_scope_expiry_and_no_io(tmp_path):
    journal = Journal(tmp_path / "artifacts.sqlite")
    tools = Tools()
    runtime = AgentRuntime(journal, Model(), tools)
    rid = runtime.start("write", AgentSpec("writer", "1", tools=(ToolSpec("write"),)), tenant_id="a")
    assert RuntimeArtifacts(journal).list(rid, tenant_id="a") == []
    assert runtime.resume(rid)["state"] == "COMPLETED"
    refs = RuntimeArtifacts(journal).list(rid, tenant_id="a")
    assert len(refs) == 1 and refs[0]["expired"]
    step = next(s for s in runtime.get(rid)["steps"] if s["kind"] == "tool")
    assert refs[0]["step_id"] == step["id"] and refs[0]["effect_id"] == step["effect_id"]
    assert refs == RuntimeArtifacts(Journal(journal.path)).list(rid, tenant_id="a")
    assert runtime.resume(rid)["state"] == "COMPLETED" and tools.calls == 1
    with pytest.raises(KeyError):
        RuntimeArtifacts(journal).list(rid, tenant_id="other")
    journal.audit(rid)


def test_invalid_manifest_cannot_commit_a_successful_step(tmp_path):
    class Invalid(Tools):
        def execute(self, call, *, effect_id):
            return {FIELD: [{"key": "untrusted", "sha256": "not-a-digest"}]}

    journal = Journal(tmp_path / "invalid.sqlite")
    runtime = AgentRuntime(journal, Model(), Invalid())
    rid = runtime.start("write", AgentSpec("writer", "1", tools=(ToolSpec("write"),)), tenant_id="a")
    runtime.tick(rid)
    assert not runtime.tick(rid)
    assert RuntimeArtifacts(journal).list(rid, tenant_id="a") == []
    assert runtime.get(rid)["steps"][1]["state"] == "RECONCILING"
    journal.audit(rid)
