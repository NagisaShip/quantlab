"""受管 Python 运行时探测（本地 / sidecar 模式诊断）。

只做**只读探测**：报告当前后端进程使用的解释器（路径、版本、实现、平台、是否虚拟环境）、
关键办公 / 科学计算依赖能否导入及其版本，并生成一份带版本号的诊断快照（manifest）。

明确边界：这里不是完整的运行时打包 / 安装实现——不创建虚拟环境、不安装或升级依赖、
不下载解释器、不保证 sidecar 与后端使用不同进程；只回答「当前实际在用什么」。
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
import time
from typing import Any

FORMAT = "quantlab.local-runtime/1"

#: 受管运行时关心的依赖：分发名 -> 导入名（python-docx 的模块名是 docx，容易写错）
MANAGED_DEPENDENCIES: dict[str, str] = {
    "python-docx": "docx",
    "python-pptx": "pptx",
    "PyMuPDF": "fitz",
    "Pillow": "PIL",
    "numpy": "numpy",
    "pandas": "pandas",
}

LIMITATIONS = (
    "只读探测：不创建虚拟环境、不安装/升级依赖、不下载解释器，也不修改当前进程。",
    "报告的是「当前后端进程」的解释器；如果桌面壳另起 sidecar，以其自身进程的探测结果为准。",
    "依赖可用性用 import 判断：能导入不代表版本兼容或原生库完整，只是该模块在当前进程可用。",
    "结果会缓存以缩短 /api/local/info 延迟；安装新依赖后需刷新页面或带 refresh=1 重新探测。",
    "不是完整打包方案；版本钉住、离线 wheelhouse 与独立 sidecar 进程仍由部署 / 打包负责。",
)

_CACHE: dict[str, Any] | None = None


def reset_cache() -> None:
    """清空依赖探测缓存（安装依赖后或测试用）。"""
    global _CACHE
    _CACHE = None


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "")[:limit]


def module_version(module: Any) -> str:
    """尽力读取模块版本：__version__ / VERSION（可能是 tuple）。"""
    value = getattr(module, "__version__", "") or getattr(module, "VERSION", "")
    if isinstance(value, tuple | list):
        value = ".".join(str(part) for part in value)
    return _text(value, 40)


def _probe_dependency(distribution: str) -> dict[str, Any]:
    module_name = MANAGED_DEPENDENCIES[distribution]
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # 缺依赖、二进制不兼容、初始化报错都在这里如实报告
        return {
            "module": module_name,
            "available": False,
            "version": "",
            "error": _text(f"{type(exc).__name__}: {exc}"),
        }
    return {
        "module": module_name,
        "available": True,
        "version": module_version(module),
        "error": "",
    }


def dependency_status(*, use_cache: bool = True) -> dict[str, dict[str, Any]]:
    """逐个探测受管依赖；默认复用上次结果，避免每次 /api/local/info 都重新导入 numpy/pandas。"""
    global _CACHE
    if use_cache and _CACHE is not None:
        return {name: dict(row) for name, row in _CACHE.items()}
    status = {distribution: _probe_dependency(distribution) for distribution in MANAGED_DEPENDENCIES}
    _CACHE = {name: dict(row) for name, row in status.items()}
    return status


def interpreter_info() -> dict[str, Any]:
    """当前解释器的真实信息（不读环境变量，避免把密钥带进响应）。"""
    executable = sys.executable or ""
    try:
        resolved = os.path.realpath(executable) if executable else ""
    except OSError:
        resolved = executable
    return {
        "executable": executable,
        "resolved_executable": resolved,
        "version": platform.python_version(),
        "version_info": list(sys.version_info[:3]),
        "version_text": _text(sys.version),
        "implementation": platform.python_implementation(),
        "compiler": _text(platform.python_compiler()),
        "platform": sys.platform,
        "platform_detail": _text(platform.platform()),
        "architecture": _text(platform.machine(), 40),
        "is_64bit": bool(sys.maxsize > 2**32),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "venv": bool(sys.prefix != sys.base_prefix),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def manifest() -> dict[str, Any]:
    """这份诊断快照的版本化元信息（格式、时间、受管依赖清单、明确限制）。"""
    return {
        "format": FORMAT,
        "generated_at": time.time(),
        "mode": "local",
        "managed": True,
        "required": list(MANAGED_DEPENDENCIES),
        "limitations": list(LIMITATIONS),
    }


def summary(dependencies: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """给 /api/local/info 用的紧凑摘要：解释器要点 + 每个依赖是否可用及版本。"""
    deps = dependencies if dependencies is not None else dependency_status()
    interpreter = interpreter_info()
    available = [name for name, row in deps.items() if row["available"]]
    missing = [name for name, row in deps.items() if not row["available"]]
    return {
        "ok": not missing,
        "executable": interpreter["executable"],
        "version": interpreter["version"],
        "implementation": interpreter["implementation"],
        "platform": interpreter["platform"],
        "venv": interpreter["venv"],
        "available": available,
        "missing": missing,
        "dependencies": {
            name: {"available": row["available"], "version": row["version"]} for name, row in deps.items()
        },
    }


def probe(*, refresh: bool = False) -> dict[str, Any]:
    """完整探测结果：解释器 + 依赖 + 摘要 + manifest（GET /api/local/runtime 的响应体）。"""
    deps = dependency_status(use_cache=not refresh)
    interpreter = interpreter_info()
    return {
        "mode": "local",
        "manifest": manifest(),
        "interpreter": interpreter,
        "dependencies": deps,
        "summary": summary(deps),
    }


__all__ = [
    "FORMAT",
    "LIMITATIONS",
    "MANAGED_DEPENDENCIES",
    "dependency_status",
    "interpreter_info",
    "manifest",
    "module_version",
    "probe",
    "reset_cache",
    "summary",
]
