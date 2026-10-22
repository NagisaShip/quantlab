"""构建标识：后端与桌面壳（Tauri）做版本握手时共用。

为什么需要它：桌面壳可能复用 `.quantlab/instance.json` 里还活着的旧后端。升级后
如果继续复用旧进程，前端会拿到旧接口/旧行为。健康端点带上「版本 + 构建标识 +
启动它的桌面壳版本」，桌面壳就能判断这个进程是不是自己这一版起的：

- `QUANTLAB_DESKTOP_VERSION`：Tauri 启动后端时写入的桌面壳版本；
- `QUANTLAB_BUILD_ID`：打包流水线可选的完整构建号（未设置时用「后端版本 + 桌面壳版本」）。

全部只读环境变量，不影响账号部署；没有桌面壳时返回 `standalone`。
"""

from __future__ import annotations

import os
import platform
import time

from .. import __version__

_STARTED_AT = time.time()


def version() -> str:
    """后端包版本（quant_agent.__version__）。"""
    return __version__


def desktop_version() -> str:
    """启动该后端的桌面壳版本；非桌面启动时为空串。"""
    return (os.environ.get("QUANTLAB_DESKTOP_VERSION") or "").strip()


def build_id() -> str:
    """可复现的构建标识：优先打包注入的完整构建号。"""
    explicit = (os.environ.get("QUANTLAB_BUILD_ID") or "").strip()
    if explicit:
        return explicit
    return f"{__version__}+{desktop_version() or 'standalone'}"


def info() -> dict[str, object]:
    """健康端点 / 本机项目信息里共用的构建信息（不暴露本机安装路径）。"""
    return {
        "version": version(),
        "build": build_id(),
        "desktop_version": desktop_version(),
        "started_at": _STARTED_AT,
        "pid": os.getpid(),
        "python": platform.python_version(),
    }


__all__ = ["build_id", "desktop_version", "info", "version"]
