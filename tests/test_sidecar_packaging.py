"""Sidecar packaging must carry the independent Agent Runtime, not only import it in tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("build_sidecar", ROOT / "scripts" / "build_sidecar.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_sidecar_bundle_includes_agent_runtime(tmp_path):
    module = _module()
    command = module._command(tmp_path / "out", clean=True)
    assert module.RUNTIME_SRC.is_dir()
    assert command.count("--collect-all") >= 1
    runtime_index = command.index("agent_runtime")
    assert command[runtime_index - 1] == "--collect-all"
    assert str(module.RUNTIME_SRC) in command
    assert "--clean" in command
