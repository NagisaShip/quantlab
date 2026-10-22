"""Native desktop shell and independent loopback project instances."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def configure(project):
    root = Path(project).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        QUANTLAB_MODE="local", QUANTLAB_PROJECT=str(root), QAGENT_HOME=str(root / ".quantlab"), QAGENT_SEED_BUILTINS="0"
    )
    os.environ["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return root


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _landing_url(url, first_run):
    """首次打开一个空项目时先落在「设置 → 本机 → 本地项目」做初始化；
    已经有项目的（project.json 已生成）直接进研究工作区，由其恢复上次研究。"""
    return url + ("/#/local" if first_run else "/#/workflows")


def open_project_instance(project):
    import httpx

    root = Path(project).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # 项目初始化标记在服务启动时写入；先记下来，决定这次开窗落在设置页还是首页
    first_run = not (root / ".quantlab/project.json").exists()
    instance = root / ".quantlab/instance.json"
    if instance.exists():
        try:
            old = json.loads(instance.read_text(encoding="utf-8"))
            url = f"http://127.0.0.1:{int(old['port'])}"
            response = httpx.get(url + "/api/local/info", timeout=2, trust_env=False)
            if response.status_code == 200 and Path(response.json()["root"]).resolve() == root:
                return {"url": _landing_url(url, first_run), "reused": True}
        except (OSError, ValueError, httpx.HTTPError):
            pass
    port = free_port()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = root / ".quantlab/launcher.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        proc = subprocess.Popen(
            (
                [sys.executable]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "quant_agent.local.launcher"]
            )
            + [
                "--serve",
                "--project",
                str(root),
                "--port",
                str(port),
            ],
            env=env,
            stdout=stream,
            stderr=stream,
            creationflags=flags,
        )
    url = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if proc.poll() is not None:
            raise RuntimeError("项目服务启动失败，请查看" + str(log))
        try:
            r = httpx.get(url + "/api/local/info", timeout=1, trust_env=False)
            if r.status_code == 200:
                return {"url": _landing_url(url, first_run), "pid": proc.pid, "reused": False}
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("项目仍在启动，可稍后重试；日志：" + str(log))


def serve(project, port):
    from .lifecycle import instance_lock

    root = configure(project)
    with instance_lock(root / ".quantlab"):
        _serve(root, port)


def _serve(project, port):
    import uvicorn

    root = configure(project)
    from ..api.app import create_app
    from ..api.deps import AppServices, set_services
    from ..api.tasks import build_worker
    from ..workflows.environments import write_json

    services = AppServices.build()
    set_services(services)
    from .lifecycle import recover_ui_operations

    recover_ui_operations(services)
    worker = build_worker(services, slots=4, poll_seconds=1)
    threading.Thread(target=worker.run_forever, daemon=True, name="local-worker").start()
    from ..platform import buildinfo

    write_json(
        root / ".quantlab/instance.json",
        {"pid": os.getpid(), "port": port, "started_at": time.time(), **buildinfo.info()},
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(with_scheduler=True), host="127.0.0.1", port=port, log_level="warning")
    )
    services.local.shutdown = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    finally:
        worker.stop_event.set()


class NativeBridge:
    def __init__(self, url):
        # pywebview 6 的 js_api 会把「公开属性」递归展开成 JS 对象（webview.util.get_functions）。
        # window 引用必须放在下划线开头的私有属性里：一旦叫 self.window，它就会顺着
        # Window → 原生 WinForms/WebView2 控件图无限展开，把 GUI 线程饿死，窗口变成「未响应」。
        self._window = None
        self.url = url

    def _trusted(self):
        current = self._window.get_current_url() if self._window else ""
        if not current or not current.startswith(self.url + "/"):
            raise RuntimeError("本机文件选择器仅提供给当前项目页面")

    def choose_folder(self):
        self._trusted()
        import webview

        paths = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return list(paths or [])

    def choose_files(self):
        self._trusted()
        import webview

        paths = self._window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True)
        return list(paths or [])

    def open_project(self, path):
        self._trusted()
        subprocess.Popen(
            (
                [sys.executable]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-m", "quant_agent.local.launcher"]
            )
            + ["--project", str(Path(path).resolve())],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"opening": True}


def desktop(project=None):
    from .prerequisites import desktop_runtime

    runtime = desktop_runtime()
    if runtime["required"] and not runtime["webview2"]:
        raise RuntimeError(
            "桌面窗口需要Microsoft Edge WebView2 Runtime。可从 "
            + runtime["download"]
            + " 安装；离线机器请预装Evergreen独立安装包。"
        )

    import pystray
    import webview
    from PIL import Image, ImageDraw

    if not project:
        import tkinter as tk
        from tkinter import filedialog

        picker = tk.Tk()
        picker.withdraw()
        project = filedialog.askdirectory(title="选择QuantLab项目文件夹（数据保存在该目录）")
        picker.destroy()
        if not project:
            return 0
    opened = open_project_instance(project)
    base = opened["url"].split("/#", 1)[0]
    bridge = NativeBridge(base)
    window = webview.create_window(
        "QuantLab · 本地研究",
        opened["url"],
        js_api=bridge,
        width=1380,
        height=920,
        min_size=(900, 640),
        text_select=True,
    )
    bridge._window = window
    quit_flag = threading.Event()
    image = Image.new("RGB", (64, 64), "#f1f0eb")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((12, 14, 52, 50), 8, fill="#4d54b4")
    draw.text((24, 24), "Q", fill="white")

    def reveal(*_):
        window.show()
        window.restore()

    def exit_app(*_):
        quit_flag.set()
        icon.stop()
        window.destroy()

    icon = pystray.Icon(
        "QuantLab",
        image,
        "QuantLab 本地研究",
        menu=pystray.Menu(
            pystray.MenuItem("打开研究窗口", reveal, default=True),
            pystray.MenuItem("退出窗口（任务继续后台）", exit_app),
        ),
    )
    icon.run_detached()

    def closing():
        if quit_flag.is_set():
            return True
        preference = Path(project) / ".quantlab/desktop-preferences.json"
        action = (
            json.loads(preference.read_text(encoding="utf-8")).get("close_action", "ask")
            if preference.exists()
            else "ask"
        )
        if action == "ask" and os.name == "nt":
            import ctypes

            choice = ctypes.windll.user32.MessageBoxW(
                None,
                "是否隐藏到托盘继续研究？\n是：后台运行；否：退出本项目（有未完成任务时阻止退出）；取消：返回。",
                "QuantLab",
                3 | 32,
            )
            if choice == 2:
                return False
            action = "background" if choice == 6 else "exit"
        if action == "background":
            window.hide()
            return False
        if action == "exit":
            import httpx

            response = httpx.post(
                base + "/api/local/shutdown",
                json={},
                headers={"X-Requested-With": "quantlab-desktop"},
                timeout=5,
                trust_env=False,
            )
            if response.status_code != 200:
                if os.name == "nt":
                    import ctypes

                    ctypes.windll.user32.MessageBoxW(
                        None, "项目仍有任务，已保留服务。请完成或取消任务后退出，或选择后台运行。", "QuantLab", 0
                    )
                return False
        quit_flag.set()
        icon.stop()
        return True

    window.events.closing += closing
    webview.start(
        private_mode=False,
        storage_path=str(Path(project) / ".quantlab/webview"),
        gui="edgechromium" if os.name == "nt" else None,
    )
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--project")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--diagnose", action="store_true")
    args = parser.parse_args()
    if args.diagnose:
        from ..workflows.environments import probe

        print(json.dumps(probe(), ensure_ascii=True))
        return 0
    if args.serve:
        if not args.project:
            parser.error("--serve需要--project")
        serve(args.project, args.port or free_port())
        return 0
    return desktop(args.project)


if __name__ == "__main__":
    raise SystemExit(main())
