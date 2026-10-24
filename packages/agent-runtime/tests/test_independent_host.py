"""Experiment C: install the independent package and resume in a new process."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def test_installed_cli_recovers_without_quantlab():
    root = Path(__file__).parents[1]
    repo = root.parents[1].resolve()
    # Keep Windows pip's nested wheel-cache paths below MAX_PATH.
    with tempfile.TemporaryDirectory(dir=repo, prefix="ar-") as directory:
        working = Path(directory).resolve()
        assert working.is_relative_to(repo)
        _installed_host_roundtrip(root, working)


def _installed_host_roundtrip(root: Path, working: Path) -> None:
    source = working / "s"
    (source / "src").mkdir(parents=True)
    shutil.copytree(
        root / "src" / "agent_runtime",
        source / "src" / "agent_runtime",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copy2(root / "pyproject.toml", source / "pyproject.toml")
    installed = working / "i"
    env = os.environ.copy()
    env.update({"TEMP": str(working), "TMP": str(working), "PIP_NO_CACHE_DIR": "1", "PYTHONPATH": str(installed)})
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(installed),
            str(source),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=working,
    )
    assert build.returncode == 0, build.stderr
    script = root / "examples" / "minimal_cli.py"
    database = working / "host.sqlite"
    first = subprocess.run(
        [sys.executable, str(script), str(database), "start"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=working,
    )
    waiting = json.loads(first.stdout)
    assert waiting["state"] == "WAITING"
    second = subprocess.run(
        [sys.executable, str(script), str(database), "resume", waiting["run_id"]],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=working,
    )
    assert json.loads(second.stdout) == {
        "run_id": waiting["run_id"],
        "state": "COMPLETED",
        "output": "The answer is 5",
    }
    assert "quant_agent" not in script.read_text(encoding="utf-8")
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agent_runtime,sys; print(agent_runtime.__file__); print('quant_agent' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        cwd=working,
    ).stdout.splitlines()
    assert str(installed) in imported[0]
    assert imported[1] == "False"
