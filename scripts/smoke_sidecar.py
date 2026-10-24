"""Exercise the shipped binary without depending on a source Python runtime."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    manifest_path = binary.parent / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["agent_runtime"] is True, "sidecar manifest does not include Agent Runtime"
    scratch = Path(__file__).resolve().parents[1] / ".tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="quantlab-sidecar-", dir=scratch) as tmp:
        root = Path(tmp)
        (root / "project").mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("QAGENT_", "QUANTLAB_", "PYTHONPATH"))}
        env.update(
            QUANTLAB_LIBRARY=str(root / "library"),
            QUANTLAB_CREDENTIALS=str(root / "credentials"),
            QAGENT_AGENT_RUNTIME="1",
            PYTHONIOENCODING="utf-8",
        )
        with (root / "backend.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(binary), "--serve", "--project", str(root / "project"), "--port", str(port)],
                cwd=root,
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 90
                base = f"http://127.0.0.1:{port}"
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("sidecar exited before readiness")
                    try:
                        with urllib.request.urlopen(base + "/api/local/info", timeout=2) as response:
                            assert json.load(response)["mode"] == "local"
                        break
                    except (OSError, ValueError):
                        time.sleep(0.5)
                else:
                    raise RuntimeError("sidecar readiness timed out")
                with urllib.request.urlopen(base + "/api/healthz", timeout=5) as response:
                    health = json.load(response)
                    assert health.get("version"), "sidecar health handshake has no version"
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    assert b"<html" in response.read().lower(), "bundled frontend missing"
                with urllib.request.urlopen(base + "/api/local/runtime", timeout=10) as response:
                    runtime = json.load(response)
                    assert runtime["interpreter"]["frozen"], "smoke test did not run frozen backend"
                    for package in ("python-docx", "python-pptx", "PyMuPDF"):
                        assert runtime["dependencies"][package]["available"], f"bundled dependency missing: {package}"
                print("PASS: frozen backend, Agent Runtime, local API, bundled frontend and document dependencies")
            except Exception:
                log.flush()
                print((root / "backend.log").read_text(encoding="utf-8", errors="replace"))
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
