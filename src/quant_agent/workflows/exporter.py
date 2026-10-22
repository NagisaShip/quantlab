"""Standalone distribution: runtime, exact card source, data, hash-locked wheels."""

from __future__ import annotations

import hashlib
import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path

from .contracts import WorkflowError, canonical, compile_workflow, params_for
from .files import resolve_input


def build_package(
    workflow,
    registry,
    files,
    destination,
    *,
    include_wheels=True,
    bundles_root=None,
    environment_info=None,
    extra_manifest=None,
):
    from .environments import probe

    environment_info = environment_info or probe()
    compile_workflow(workflow, registry)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = Path(files).resolve()
    definitions = {}
    resources = set(workflow.get("resources", []))

    def collect(flow, depth=0):
        if depth > 8:
            raise WorkflowError("子流程嵌套过深")
        for node in flow["nodes"]:
            d = registry.get(node["kind"], str(node["version"]))
            definitions[(d["kind"], str(d["version"]))] = d
            if d.get("capabilities", {}).get("network"):
                raise WorkflowError(
                    f"「{d['title']}」声明需要联网，不能直接导出为完整离线包；"
                    "请先运行一次工作流，再从这次运行导出「冻结运行」（行情会固化成本地文件）"
                )
            if d.get("portable", True) is False:
                raise WorkflowError(f"{d['title']}尚无独立实现，不能导出为脱离平台运行的完整包")
            for key in d.get("file_params", []):
                if key in node.get("bindings", {}):
                    if not flow.get("resources"):
                        raise WorkflowError("动态文件参数需要在resources中显式列出全部资源")
                elif params_for(node, d).get(key):
                    resources.add(params_for(node, d)[key])
            resources.update(flow.get("resources", []))
            if d.get("subflow"):
                from .contracts import subflow_instance

                collect(subflow_instance(d, params_for(node, d)), depth + 1)

    collect(workflow)
    # Frozen upstream experiment inputs may refer to immutable binary evidence.
    # Include those references recursively rather than exporting dangling paths.
    pending = list(resources)
    seen = set()
    while pending:
        resource = pending.pop()
        if resource in seen:
            continue
        seen.add(resource)
        candidate = resolve_input(files, resource)
        if candidate.suffix == ".json" and candidate.stat().st_size <= 16_000_000:
            import json

            try:
                document = json.loads(candidate.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                continue

            def discover(value):
                if isinstance(value, dict):
                    if isinstance(value.get("file"), str) and re.fullmatch(
                        r"[0-9a-f]{64}", str(value.get("sha256", ""))
                    ):
                        path = (files / value["file"]).resolve()
                        if not path.is_relative_to(files) or not path.is_file():
                            raise WorkflowError("上游证据文件缺失：" + value["file"])
                        h = hashlib.sha256()
                        with path.open("rb") as stream:
                            for b in iter(lambda: stream.read(1024 * 1024), b""):
                                h.update(b)
                        if h.hexdigest() != value["sha256"]:
                            raise WorkflowError("上游证据文件校验失败：" + value["file"])
                        resources.add(value["file"])
                        pending.append(value["file"])
                    else:
                        for child in value.values():
                            discover(child)
                elif isinstance(value, list):
                    for child in value:
                        discover(child)

            discover(document)
    requirements = sorted({r for d in definitions.values() for r in d.get("requirements", [])})
    if requirements:
        requirements = sorted(set(requirements) | {"packaging>=24"})
    if requirements and not include_wheels:
        raise WorkflowError("完整离线导出必须包含依赖安装包；请启用include_wheels")
    with tempfile.TemporaryDirectory(prefix="qwf-export-") as tmp:
        root = Path(tmp)
        runtime = root / "workflow_runtime"
        runtime.mkdir()
        source = Path(__file__).parent
        for name in (
            "__init__.py",
            "contracts.py",
            "registry.py",
            "runtime.py",
            "node_child.py",
            "bundles.py",
            "environments.py",
            "files.py",
        ):
            shutil.copyfile(source / name, runtime / name)
        from .bundles import sources

        modules = {name for d in definitions.values() for name in d.get("bundled_modules", [])}
        for bundle in sources(sorted(modules), bundles_root):
            for item in bundle["files"]:
                target = root / "modules" / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item["code"], encoding="utf-8")
        shutil.copytree(
            source / "nodes",
            runtime / "nodes",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (root / "workflow.json").write_text(canonical(workflow), encoding="utf-8")
        (root / "cards.json").write_text(canonical(list(definitions.values())), encoding="utf-8")
        data = root / "files"
        data.mkdir()
        for name in sorted(resources):
            p = resolve_input(files, name)
            target = (data / name).resolve()
            if not target.is_relative_to(data.resolve()):
                raise WorkflowError("非法资源路径")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
        (root / "requirements.txt").write_text("\n".join(requirements) + "\n", encoding="utf-8")
        wheels = root / "wheelhouse"
        wheels.mkdir()
        locks = []
        if requirements:
            # Resolve only needed wheels, constrained to the environment whose
            # code is being exported. Unrelated installed packages aren't bundled.
            constraints = root / "environment-constraints.txt"
            constraints.write_text(
                "\n".join(sorted({f"{name}=={version}" for name, version in environment_info["packages"].items()}))
                + "\n",
                encoding="utf-8",
            )
            download_command = [
                environment_info["python"],
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--dest",
                str(wheels),
                "-r",
                str(root / "requirements.txt"),
                "--constraint",
                str(constraints),
            ]
            if any(re.split(r"[\[<>=!~]", r)[0] == "torch" for r in requirements):
                installed = environment_info["packages"].get("torch", "")
                if "+cu" in installed:
                    download_command.extend(
                        ["--extra-index-url", "https://download.pytorch.org/whl/" + installed.split("+")[1]]
                    )
            result = subprocess.run(
                download_command,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode:
                raise WorkflowError("依赖安装包下载失败，未生成不完整导出：" + (result.stderr or result.stdout)[-2000:])
            for wheel in sorted(wheels.glob("*.whl")):
                with zipfile.ZipFile(wheel) as z:
                    meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
                    info = Parser().parsestr(z.read(meta).decode("utf-8"))
                digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
                locks.append(f"{info['Name']}=={info['Version']} --hash=sha256:{digest}")
            if not locks:
                raise WorkflowError("未取得依赖wheel，无法保证离线安装")
            constraints.unlink()
        (root / "requirements.lock").write_text(
            "# Generated for the Python/platform in manifest.json\n" + "\n".join(locks) + "\n",
            encoding="utf-8",
        )
        (root / "run.py").write_text(RUN_SCRIPT, encoding="utf-8")
        (root / "setup.bat").write_text(
            '@echo off\r\ncd /d "%~dp0"\r\npython -m venv .venv\r\nif errorlevel 1 exit /b 1\r\n.venv\\Scripts\\python.exe -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock\r\n',
            encoding="utf-8",
        )
        (root / "run.bat").write_text(
            '@echo off\r\ncd /d "%~dp0"\r\n.venv\\Scripts\\python.exe run.py %*\r\n',
            encoding="utf-8",
        )
        (root / "setup.sh").write_text(
            '#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\npython3 -m venv .venv\n.venv/bin/python -m pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock\n',
            encoding="utf-8",
        )
        (root / "README.md").write_text(README, encoding="utf-8")
        (root / "CUSTOM_NODES.md").write_text(SPEC, encoding="utf-8")
        hashes = {}
        for p in sorted(root.rglob("*")):
            if p.is_file():
                h = hashlib.sha256()
                with p.open("rb") as f:
                    for b in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(b)
                hashes[p.relative_to(root).as_posix()] = h.hexdigest()
        manifest = {
            "format": "quantlab.workflow-package/1",
            "python": environment_info["version"],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "offline_complete": True,
            "requirements": requirements,
            "files": hashes,
            "cards": [
                {
                    "kind": d["kind"],
                    "version": d["version"],
                    "code_hash": d["code_hash"],
                }
                for d in definitions.values()
            ],
        }
        if extra_manifest:
            manifest.update(extra_manifest)
        (root / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(root).as_posix())
    return manifest


RUN_SCRIPT = '''"""No QuantLab account, server, database or network required."""
import argparse, hashlib, json, platform
import sys
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
root=Path(__file__).resolve().parent
parser=argparse.ArgumentParser()
parser.add_argument('--target')
parser.add_argument('--check',action='store_true')
args=parser.parse_args()
manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
if manifest['requirements'] and platform.python_version().split('.')[:2]!=manifest['python'].split('.')[:2]:
    raise SystemExit('Dependency wheels target Python '+manifest['python']+'; use matching Python and platform.')
for name,expected in manifest['files'].items():
    p=(root/name).resolve()
    if not p.is_relative_to(root):raise SystemExit('Invalid package path')
    h=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    if h.hexdigest()!=expected:raise SystemExit('Integrity check failed: '+name)
sys.path.insert(0,str(root))
sys.path.insert(0,str(root/'modules'))
from workflow_runtime.registry import Registry
from workflow_runtime.runtime import Runner
cards=json.loads((root/'cards.json').read_text(encoding='utf-8'))
registry=Registry(cards, frozen=True)
flow=json.loads((root/'workflow.json').read_text(encoding='utf-8'))
from workflow_runtime.contracts import compile_workflow
compile_workflow(flow,registry,target=args.target)
if args.check:
    print('Package hashes and workflow contracts passed')
else:
    report=Runner(root/'outputs',registry,files=root/'files').run(flow,target=args.target)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if report['status']!='completed':raise SystemExit(1)
'''

README = """# 独立运行的QuantLab研究工作流

此包包含工作流、准确版本卡片源码、运行器、显式输入文件、依赖声明、带哈希的依赖锁和wheel安装包。
本机需要Python 3.11或以上。使用第三方库时，请使用manifest.json记录的Python主次版本、操作系统和CPU架构。
不需要QuantLab安装、账号、API服务或数据库。此包不附带Python解释器，不宣称跨操作系统通用。

Windows：先运行setup.bat，再运行run.bat。Linux/macOS：sh setup.sh，再运行.venv/bin/python run.py。
安装依赖使用--no-index，只从包内wheelhouse读取，不访问互联网。无第三方依赖时可直接python run.py。
python run.py --check核验内容哈希和连接约束；--target NODE_ID仅运行目标及其上游。
结果保存在outputs/runs，数据工件在outputs/artifacts。首次执行后可在相同包内复用缓存。

此包的内容哈希用于完整性检查，不是发布者身份认证。只执行可信来源的代码；独立进程不等于安全沙箱。
如需修改代码，应在开发副本中更新版本和工作流并重新生成包；不要关闭哈希校验来掩盖来源变化。
"""

SPEC = """# 自定义卡片规范 v1

每张函数卡片是UTF-8 Python文件，包含唯一字面量NODE字典及execute(ctx, inputs, params)函数。
NODE必须声明kind、version、title、inputs、outputs、parameters；用requirements声明全部第三方依赖。
输入输出为具名字典，端口含type、可选role、schema和required。返回值必须含且仅含声明的全部输出。
使用ctx.file(name)读取显式导入文件，ctx.work保存临时文件，ctx.seed取得种子，ctx.log写日志。
声明file_params列出文件参数；动态文件名须在workflow.resources列出全部文件，保证完整导出。
输出为可JSON序列化的数据，不含NaN、Infinity、进程句柄或未经声明的本地路径。
参数schema支持type/properties/required/additionalProperties/enum/minimum/maximum/minLength/maxLength/items/minItems/maxItems/default。
固定版本不得覆盖；修改公共代码增加version，临时修改另存为新的kind。端口改名必须显式重新连接。
声明capabilities.network=True的卡片不能导出为完整离线包；平台专用函数必须提供独立实现才能完整导出。
顶层代码只放导入、定义和常量；不要在导入时联网、读写数据、加载大模型或执行训练。
训练节点只能读取train/validation用途输入，最终test由独立评价节点读取。禁止暗中访问测试标签。
不要原地修改输入，不依赖隐藏全局状态；所有影响结果的配置必须进入参数、资源或版本。
依赖仅接受PyPI包名与版本约束；导出时解析全部传递依赖，保存wheel及哈希锁。
自定义Python是可信代码扩展，子进程隔离仅提供超时和取消边界，不能当作对抗恶意代码的沙箱。
在允许不可信作者的部署中，应使用系统级容器隔离，不向执行环境注入服务凭证。
"""


SPEC += "\n模块可声明level=module或function。端口semantics表示显式语义，role_from保留输入用途；多输入合并保留更严格的测试/验证用途。\nsubflow.parameters将公开参数映射到内部节点parameter，支持一对多。使用bundled_modules显式声明独立实现包；导出器冻结包内全部Python文件。\nctx.save_file保存模型或图像，返回带哈希的二进制工件引用。组件图和图表均由普通卡片契约驱动，不依赖平台专用执行服务。\n"
