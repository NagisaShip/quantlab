"""发现本机 Python：同一个解释器经不同路径（shim / 大小写 / venv）只显示一次。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import quant_agent.workflows.environments as environments
from quant_agent.workflows.contracts import WorkflowError


def test_frozen_backend_cannot_be_used_as_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(environments.shutil, "which", lambda _name: None)
    monkeypatch.delenv("QUANTLAB_RESEARCH_PYTHON", raising=False)
    monkeypatch.setattr(
        environments.subprocess, "run", lambda *_a, **_k: pytest.fail("must not launch sidecar as Python")
    )
    manager = environments.EnvironmentManager(tmp_path)
    assert manager.selected() == ""
    with pytest.raises(WorkflowError, match="研究环境"):
        manager.info()
    with pytest.raises(WorkflowError, match="独立 Python"):
        environments.probe(sys.executable)


def test_discover_dedupes_same_interpreter(tmp_path, monkeypatch) -> None:
    resolved = str(Path(sys.executable).resolve())
    calls: list[str] = []

    def fake_probe(python, refresh=False):
        calls.append(str(python))
        return {
            "python": resolved,
            "version": "3.12.0",
            "packages": {},
            "platform": sys.platform,
        }

    monkeypatch.setattr(environments, "probe", fake_probe)
    monkeypatch.setattr(
        environments.shutil,
        "which",
        lambda name: "C:/fake/python.exe" if name == "python" else None,
    )
    manager = environments.EnvironmentManager(tmp_path)
    result = manager.discover()["environments"]
    assert len(calls) >= 2  # 至少两个不同输入路径都探测过
    assert len(result) == 1
    assert result[0]["ok"] is True and result[0]["python"] == resolved
