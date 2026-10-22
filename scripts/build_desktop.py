"""Build an auditable Windows desktop folder and optional per-user EXE installer.

Run with a dedicated environment installed from .[research,desktop]. No user
data is read or included. Requires Windows .NET csc and an official Inno compiler.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    p.add_argument("--iscc", default="")
    args = p.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve()
    distribution = output / "QuantLab-Local-x64"
    if distribution.exists():
        raise SystemExit("Output folder already exists; choose a fresh destination")
    distribution.mkdir(parents=True)
    base = Path(sys.base_prefix)
    runtime = distribution / "runtime"
    runtime.mkdir()
    for f in base.iterdir():
        if f.is_file() and (f.suffix in (".exe", ".dll") or f.name == "LICENSE.txt"):
            shutil.copy2(f, runtime / f.name)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "site-packages", "test", "tests")
    for name in ("Lib", "DLLs", "tcl", "include", "libs"):
        if (base / name).exists():
            shutil.copytree(base / name, runtime / name, ignore=ignore)
    packages = Path(sys.prefix) / "Lib/site-packages"
    shutil.copytree(
        packages,
        runtime / "Lib/site-packages",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "__editable__*", "quant_agent-*.dist-info"),
    )
    app = distribution / "app"
    shutil.copytree(repo / "src", app / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    shutil.copytree(repo / "web/dist", app / "web/dist")
    shutil.copytree(repo / "docs", app / "docs", ignore=shutil.ignore_patterns("*.png", "*.pdf"))
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        if (repo / name).exists():
            shutil.copy2(repo / name, app / name)
    packaging = repo / "packaging/desktop"
    shutil.copy2(packaging / "start_desktop.py", app / "start_desktop.py")
    compiler = Path("C:/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe")
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/reference:System.Windows.Forms.dll",
            "/out:" + str(distribution / "QuantLab.exe"),
            str(packaging / "Launcher.cs"),
        ],
        check=True,
    )
    target_python = runtime / "python.exe"
    check = subprocess.run(
        [
            str(target_python),
            "-I",
            "-c",
            "import pandas,matplotlib,networkx,webview,pyarrow,openpyxl,sqlite3,tkinter;print('Bundled runtime verified')",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    print(check.stdout, flush=True)
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--disable-pip-version-check", "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    (app / "dependencies.json").write_text(installed.stdout, encoding="utf-8")
    files = {f.relative_to(distribution).as_posix(): hash_file(f) for f in distribution.rglob("*") if f.is_file()}
    (distribution / "BUILD-MANIFEST.json").write_text(
        json.dumps(
            {
                "format": "quantlab.desktop-build/1",
                "python": sys.version,
                "architecture": "Windows x64",
                "files": files,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Verified distribution: {distribution}; {len(files)} files", flush=True)
    if args.iscc:
        subprocess.run(
            [
                args.iscc,
                "/DSourcePath=" + str(distribution),
                "/DOutputPath=" + str(output),
                str(packaging / "QuantLab.iss"),
            ],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
