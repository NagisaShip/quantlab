"""Build the QuantLab Python backend as a PyInstaller onedir sidecar.

This is intentionally a *packaging* helper, not a runtime dependency:

    python scripts/build_sidecar.py --check
    python scripts/build_sidecar.py --clean --output desktop/src-tauri/resources/backend

``--check`` only verifies prerequisites and prints the exact command; it never
installs packages or downloads anything. A real build requires ``PyInstaller``
in the active interpreter.

The frozen executable is ``quantlab-backend[.exe]`` and accepts the same
``--serve --project ... --port ...`` arguments as the desktop launcher.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "sidecar_entry.py"
RUNTIME_SRC = ROOT / "packages" / "agent-runtime" / "src"
DEFAULT_OUT = ROOT / "desktop" / "src-tauri" / "resources" / "backend"
NAME = "quantlab-backend"


def _command(output: Path, clean: bool) -> list[str]:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name",
        NAME,
        "--distpath",
        str(output),
        "--workpath",
        str(ROOT / ".tmp" / "pyinstaller-work"),
        "--specpath",
        str(ROOT / ".tmp" / "pyinstaller-spec"),
        "--collect-all",
        "quant_agent",
        "--collect-all",
        "quantlab_research",
        "--collect-all",
        "quantlab_files",
        "--collect-all",
        "quantlab_supplychain",
        "--collect-all",
        "agent_runtime",
        "--collect-all",
        "docx",
        "--collect-all",
        "pptx",
        "--collect-all",
        "pymupdf",
        "--paths",
        str(ROOT / "src"),
        "--paths",
        str(RUNTIME_SRC),
        "--add-data",
        f"{ROOT / 'web' / 'dist'}{os.pathsep}web/dist",
        str(ENTRY),
    ]
    if clean:
        args.insert(3, "--clean")
    return args


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the QuantLab backend sidecar (PyInstaller onedir).")
    parser.add_argument("--check", action="store_true", help="only verify prerequisites and print the command")
    parser.add_argument("--clean", action="store_true", help="pass --clean to PyInstaller")
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUT, help="output directory (default: desktop resources)"
    )
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401

        have = True
    except ImportError:
        have = False

    output = args.output.resolve()
    if not ENTRY.is_file():
        print(f"缺少入口文件：{ENTRY}")
        return 2
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        print("缺少 web/dist/index.html：先运行 `cd web && npm run build:windows`")
        return 1
    if not (RUNTIME_SRC / "agent_runtime" / "__init__.py").is_file():
        print(f"缺少 Agent Runtime 源码：{RUNTIME_SRC}")
        return 1
    if not have:
        print("未安装 PyInstaller：先 `python -m pip install pyinstaller`（或 `.[dev]` 中的打包依赖）")
        print("将执行：", " ".join(_command(output, args.clean)))
        return 1
    if args.check:
        print("PyInstaller 可用；输出：", output)
        print("将执行：", " ".join(_command(output, args.clean)))
        return 0

    output.mkdir(parents=True, exist_ok=True)
    print("Building sidecar:", output)
    code = subprocess.call(_command(output, args.clean))
    if code:
        return code
    executable = output / NAME / (NAME + (".exe" if os.name == "nt" else ""))
    if not executable.is_file():
        print(f"PyInstaller 未生成预期可执行文件：{executable}")
        return 1
    manifest = {
        "format": "quantlab.sidecar-build/1",
        "name": NAME,
        "executable": executable.name,
        "agent_runtime": True,
        "frontend": "web/dist",
    }
    (executable.parent / "build-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Sidecar ready:", executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
