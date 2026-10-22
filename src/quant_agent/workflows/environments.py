"""Project-scoped interpreters shared by checks, card execution and previews."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .contracts import WorkflowError

RESEARCH = [
    "pandas>=2.2,<4",
    "matplotlib>=3.8,<4",
    "networkx>=3,<4",
    "scipy>=1.13,<2",
    "scikit-learn>=1.5,<2",
    "joblib>=1,<2",
    "openpyxl>=3.1",
    "pyarrow>=16",
]
_CACHE = {}
_LOCK = threading.RLock()
PROBE = """import sys,json,importlib.metadata as m,platform
print(json.dumps({'python':sys.executable,'version':platform.python_version(),'implementation':platform.python_implementation(),'platform':sys.platform,'paths':[p for p in sys.path if 'site-packages' in p or 'dist-packages' in p],'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata.get('Name')}},ensure_ascii=True))
"""


def write_json(path, value):
    import uuid

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + uuid.uuid4().hex[:12] + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def default_python():
    if not getattr(sys, "frozen", False):
        return sys.executable
    return os.environ.get("QUANTLAB_RESEARCH_PYTHON") or shutil.which("python") or shutil.which("python3") or ""


def probe(python=None, *, refresh=False):
    candidate = python or default_python()
    if not candidate:
        raise WorkflowError("请在研究环境中选择 Python 3.11 或更新版本；桌面后端不能用作研究解释器")
    executable = str(Path(candidate).resolve())
    if getattr(sys, "frozen", False) and Path(executable) == Path(sys.executable).resolve():
        raise WorkflowError("桌面后端不是独立 Python 解释器，请在研究环境中选择 Python 3.11 或更新版本")
    with _LOCK:
        cached = _CACHE.get(executable)
        if cached and not refresh and time.monotonic() - cached[0] < 15:
            return cached[1]
    try:
        result = subprocess.run(
            [executable, "-E", "-P", "-c", PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise WorkflowError("解释器探测失败：" + result.stderr[-500:])
        value = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise WorkflowError("无法使用该Python环境：" + str(exc)) from exc
    if tuple(map(int, value["version"].split(".")[:2])) < (3, 11):
        raise WorkflowError("研究环境需要Python 3.11或更新版本")
    with _LOCK:
        _CACHE[executable] = (time.monotonic(), value)
    return value


def requirement_status(info, requirement):
    from packaging.requirements import Requirement

    req = Requirement(requirement)
    if req.marker and not req.marker.evaluate({"python_version": ".".join(info["version"].split(".")[:2])}):
        return "不适用于此环境"
    version = info["packages"].get(req.name.lower().replace("_", "-"))
    if not version or (req.specifier and not req.specifier.contains(version, prereleases=True)):
        raise WorkflowError(
            f"环境{info['python']}缺少{requirement}或版本不符（已装{version or '无'}）；打开本地项目→Python环境→修复研究依赖"
        )
    return version


class EnvironmentManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config = self.root / "environment.json"

    def selected(self):
        try:
            return json.loads(self.config.read_text(encoding="utf-8"))["python"]
        except FileNotFoundError:
            return default_python()

    def info(self, refresh=False):
        return probe(self.selected(), refresh=refresh)

    def check(self, requirement):
        return requirement_status(self.info(), requirement)

    def status(self):
        info = self.info()
        checks = []
        for requirement in RESEARCH:
            try:
                checks.append(
                    {"requirement": requirement, "ok": True, "version": requirement_status(info, requirement)}
                )
            except WorkflowError as exc:
                checks.append({"requirement": requirement, "ok": False, "message": str(exc)})
        return {
            **info,
            "selected": str(self.selected()),
            "checks": checks,
            "ready": all(c["ok"] for c in checks),
            "server_python": sys.executable,
            "execution_scope": "workflow cards and chart preview",
            "note": "原专业研究页使用服务解释器；桌面基础环境同时包含研究依赖。切换不删除旧环境。",
        }

    def select(self, python):
        info = probe(python, refresh=True)
        old = self.selected()
        if sys.platform != info["platform"]:
            raise WorkflowError("不能选择其他操作系统的解释器")
        write_json(
            self.config,
            {"python": info["python"], "previous": str(old), "selected_at": time.time(), "version": info["version"]},
        )
        return self.status()

    def discover(self):
        paths = {str(self.selected())}
        if not getattr(sys, "frozen", False):
            paths.add(sys.executable)
        for name in ("python", "python3"):
            if found := shutil.which(name):
                paths.add(found)
        for base in (self.root.parent, self.root.parent.parent, Path.cwd()):
            for name in (".venv", "venv", ".conda"):
                for relative in ("Scripts/python.exe", "bin/python"):
                    candidate = base / name / relative
                    if candidate.is_file():
                        paths.add(str(candidate))
        conda = Path.home() / ".conda/environments.txt"
        if conda.is_file():
            for line in conda.read_text(encoding="utf-8").splitlines()[:30]:
                for relative in ("python.exe", "bin/python"):
                    if (candidate := Path(line) / relative).is_file():
                        paths.add(str(candidate))
        results = []
        for path in sorted(paths - {""}):
            try:
                results.append({"ok": True, **probe(path)})
            except WorkflowError as exc:
                results.append({"ok": False, "python": path, "message": str(exc)})
        # Windows 上 python.exe / venv 的 shim / 大小写不同的同一条路径会被 probe 解析成同一个解释器：
        # 按真实解释器去重，避免「发现本机 Python」列表里同一个环境出现两次。
        seen: set[str] = set()
        unique = []
        for item in results:
            python = item.get("python") or ""
            key = os.path.normcase(os.path.realpath(python)) if python else ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            unique.append(item)
        return {"environments": unique}

    def repair(self, *, base_python=None, wheelhouse="", neural=False):
        """Create a new environment, then select only after installation passes."""
        import uuid

        base = probe(base_python or self.selected())
        folder = self.root / "environments" / ("research-" + uuid.uuid4().hex[:8])
        folder.parent.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log = self.root / "environment-install.log"
        requirements = RESEARCH + (["torch>=2.8,<3"] if neural else [])
        try:
            subprocess.run(
                [base["python"], "-m", "venv", str(folder)],
                check=True,
                capture_output=True,
                timeout=180,
                creationflags=flags,
            )
            python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
            if wheelhouse:
                wheels = Path(wheelhouse).resolve()
                if not wheels.is_dir():
                    raise WorkflowError("离线wheel目录不存在")
                command += ["--no-index", "--find-links", str(wheels)]
            command += requirements
            with log.open("w", encoding="utf-8") as stream:
                result = subprocess.run(
                    command, stdout=stream, stderr=subprocess.STDOUT, timeout=1800, creationflags=flags
                )
            if result.returncode:
                raise WorkflowError("依赖安装失败，当前环境未切换；日志：" + str(log))
            info = probe(python, refresh=True)
            for req in requirements:
                requirement_status(info, req)
            return self.select(python)
        except (subprocess.SubprocessError, OSError) as exc:
            raise WorkflowError("创建研究环境失败；当前环境未切换：" + str(exc)) from exc
