"""Python 策略的执行隔离（父进程侧）。

plan v2.0：高级 Python 策略“运行在隔离环境，不接触凭证、管理 API 与宿主文件；资源和网络限制之外，
还须提供经验证的执行隔离，不能仅靠语法检查作为安全边界”。

这里的做法（每一项是否生效都会写进运行清单的 isolation 字段，界面如实展示）：
1. 独立子进程，环境变量清空（只留 PATH / PYTHONPATH / 语言设置）；
2. 资源上限：CPU 秒数、地址空间、单文件大小、文件句柄数；
3. 父进程为 root 时降权为 nobody（读不到 0600 的密钥文件和数据库目录）并禁止再派生进程；
4. 权限允许时进入新的网络命名空间（无任何网卡，网络不可达）；
5. 一次性临时工作目录；超时由父进程强制终止。

`QAGENT_SANDBOX=inprocess` 只用于开发调试（报告里会标为“未隔离”）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SRC_ROOT = str(Path(__file__).resolve().parents[2])
NOBODY = 65534


class SandboxError(RuntimeError):
    def __init__(self, message: str, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


def mode() -> str:
    return os.environ.get("QAGENT_SANDBOX", "subprocess")


def limits() -> dict[str, int]:
    return {
        "cpu_seconds": int(os.environ.get("QAGENT_SANDBOX_CPU_SECONDS", "300")),
        "memory_mb": int(os.environ.get("QAGENT_SANDBOX_MEMORY_MB", "1536")),
        "file_mb": 16,
        "open_files": 64,
    }


_PROBE: dict[str, Any] | None = None
_UID_DROP = os.environ.get("QAGENT_SANDBOX_UID_DROP", "auto") != "off"


def probe() -> dict[str, Any]:
    """探测本机能提供哪些隔离（结果缓存）。用于健康页与运行清单。"""
    global _PROBE
    if _PROBE is not None:
        return _PROBE
    rep = {
        "mode": mode(),
        "process": mode() == "subprocess",
        "env_cleared": mode() == "subprocess",
        "rlimits": False,
        "uid_drop": False,
        "no_fork": False,
        "netns": False,
        "limits": limits(),
    }
    if mode() == "subprocess":
        code = (
            "import os,socket,resource,sys\n"
            f"sys.path.insert(0, {SRC_ROOT!r})\n"
            "import quant_agent.sdk, quant_agent.engine.research\n"
            "r={'uid':os.getuid(),'cpu':resource.getrlimit(resource.RLIMIT_CPU)[0]}\n"
            "try:\n s=socket.create_connection(('1.1.1.1',53),timeout=1);r['net']=True\n"
            "except Exception: r['net']=False\n"
            "print(__import__('json').dumps(r))"
        )
        global _UID_DROP
        try:
            p = subprocess.run(
                [sys.executable, "-c", code],
                preexec_fn=_preexec(limits()),
                env=_env(),
                capture_output=True,
                text=True,
                timeout=30,
                cwd=tempfile.gettempdir(),
            )
            if p.returncode != 0 and _UID_DROP and os.name == "posix" and os.getuid() == 0:
                # 降权后读不到代码或依赖（目录权限过严）：不降权，其余隔离照常；报告里如实标注
                _UID_DROP = False
                rep["uid_drop_error"] = (p.stderr or "").strip().splitlines()[-1:] or ["无法以 nobody 身份导入平台代码"]
                p = subprocess.run(
                    [sys.executable, "-c", code],
                    preexec_fn=_preexec(limits()),
                    env=_env(),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=tempfile.gettempdir(),
                )
            r = json.loads(p.stdout.strip().splitlines()[-1])
            rep.update(
                {
                    "rlimits": r.get("cpu", -1) not in (-1, None),
                    "uid_drop": r.get("uid") == NOBODY,
                    "no_fork": r.get("uid") == NOBODY,
                    "netns": not r.get("net", True),
                }
            )
        except Exception as exc:  # pragma: no cover - 取决于宿主
            rep["probe_error"] = str(exc)[:200]
    rep["verified"] = bool(rep["process"] and rep["rlimits"] and rep["uid_drop"] and rep["netns"])
    _PROBE = rep
    return rep


def _env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": SRC_ROOT + os.pathsep + os.environ.get("QAGENT_SANDBOX_PYTHONPATH", ""),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _preexec(lim: dict[str, int]) -> Callable[[], None] | None:
    if os.name != "posix":
        return None

    def apply() -> None:  # 在子进程 exec 之前执行
        import resource

        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (lim["cpu_seconds"], lim["cpu_seconds"] + 5))
        mem = lim["memory_mb"] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim["file_mb"] * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_NOFILE, (lim["open_files"],) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if os.getuid() == 0:
            try:
                os.unshare(os.CLONE_NEWNET)  # 没有网卡 → 网络不可达
            except Exception:
                pass
        if os.getuid() == 0 and _UID_DROP:
            os.setgroups([])
            os.setgid(NOBODY)
            os.setuid(NOBODY)
            try:
                resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))  # nobody 不能再派生进程
            except Exception:
                pass

    return apply


def _python_path() -> str:
    return sys.executable


class _Child:
    def __init__(self, args: list[str], *, timeout: float) -> None:
        self.workdir = tempfile.mkdtemp(prefix="qa-sbx-")
        os.chmod(self.workdir, 0o777)
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [
                _python_path(),
                "-c",
                f"import sys;sys.path.insert(0, {SRC_ROOT!r});from quant_agent.platform.sandbox_exec import main;"
                f"sys.argv=['sbx']+{args!r};raise SystemExit(main())",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            cwd=self.workdir,
            env=_env(),
            preexec_fn=_preexec(limits()),
            bufsize=1,
        )
        self._stderr: list[str] = []
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _drain_err(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                self._stderr.append(line)
                if len(self._stderr) > 200:
                    del self._stderr[:100]
        except (ValueError, OSError):
            # `close()` 关掉管道时本线程可能正阻塞在这里：Python 会抛
            # `ValueError: I/O operation on closed file`，Windows 上也可能抛 OSError。
            # 这不是错误路径（进程正在正常收尾），安静退出即可——
            # 否则异常会从线程里冒到解释器，变成"unraisable exception"噪声。
            return

    def send(self, obj: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError as exc:
            raise SandboxError(self._crash_message()) from exc

    def read_until_result(
        self, on_line: Callable[[dict[str, Any]], None] | None = None, timeout: float | None = None
    ) -> Any:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + (timeout or self.timeout)
        result: dict[str, Any] = {}
        timer = threading.Timer(max(0.1, deadline - time.monotonic()), self.kill)
        timer.daemon = True
        timer.start()
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "result" in msg or "error" in msg:
                    result = msg
                    break
                if on_line:
                    on_line(msg)
        finally:
            timer.cancel()
        if not result:
            if time.monotonic() >= deadline:
                raise SandboxError(f"策略运行超时（{int(timeout or self.timeout)} 秒），已终止隔离进程", "Timeout")
            raise SandboxError(self._crash_message(), "Crashed")
        if "error" in result:
            raise SandboxError(result["error"], result.get("kind", ""))
        return result["result"]

    def _crash_message(self) -> str:
        code = self.proc.poll()
        tail = "".join(self._stderr[-5:])[-400:]
        if code is not None and code < 0:
            return f"隔离进程被终止（信号 {-code}，可能超出 CPU 或内存上限）"
        if "MemoryError" in tail:
            return "策略超出内存上限，隔离进程已终止"
        return f"隔离进程异常退出（{code}）：{tail.strip()}"

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                try:
                    self.send({"op": "exit"})
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.kill()
        finally:
            # 必须显式关掉三个管道并回收子进程，否则：
            # 1. `_drain_err` 那个 daemon 线程会一直阻塞在 `for line in self.proc.stderr` 上，
            #    直到解释器退出时才对**已经 finalize 的句柄**做一次读，于是抛
            #    `OSError: [Errno 22] Invalid argument`（表现为 pytest 的
            #    PytestUnraisableExceptionWarning / 解释器退出噪声，容易掩盖真正的失败）；
            # 2. Windows 上未回收的进程句柄会泄漏。
            # 顺序有意为之：先关 stdin（让子进程看到 EOF 后自己退），再 wait，
            # 最后才关 stdout/stderr —— 反过来会把还在写输出的子进程打断。
            self._close_pipes()
            try:
                if self.proc.poll() is None:
                    self.kill()
                self.proc.wait(timeout=2)
            except Exception:
                pass
            shutil.rmtree(self.workdir, ignore_errors=True)

    def _close_pipes(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def _ensure_probed() -> None:
    if mode() == "subprocess" and _PROBE is None:
        probe()


def run_batch(
    request: dict[str, Any], *, on_line: Callable[[dict[str, Any]], None] | None = None, timeout: float = 600.0
) -> Any:
    """执行一次批处理（backtest / optimize）。

    Python 策略在隔离子进程里执行；规则 DSL 没有用户代码（只解释已校验的语法树），在 worker 进程内执行。
    """
    if mode() == "inprocess" or request.get("lang") == "dsl":
        from . import sandbox_exec as sx

        if request["op"] == "optimize":
            return sx.op_optimize(request, emit=on_line or (lambda _m: None))
        if request["op"] == "stress":
            return sx.op_stress(request, emit=on_line or (lambda _m: None))
        if request["op"] == "checkup":
            return sx.op_checkup(request, emit=on_line or (lambda _m: None))
        return sx.op_backtest(request)
    _ensure_probed()
    child = _Child(["batch"], timeout=timeout)
    try:
        child.send(request)
        return child.read_until_result(on_line)
    finally:
        child.close()


class IsolatedSession:
    """长期存在的隔离会话（实时模拟）：一个 run 对应一个子进程。"""

    def __init__(self, *, timeout: float = 120.0, lang: str = "python") -> None:
        self.timeout = timeout
        if mode() == "inprocess" or lang == "dsl":
            from .sandbox_exec import PaperSessionHost

            self._host: Any = PaperSessionHost()
            self._child = None
        else:
            self._host = None
            _ensure_probed()
            self._child = _Child(["serve"], timeout=timeout)

    def call(self, op: str, **payload: Any) -> Any:
        if self._host is not None:
            return {
                "start": self._host.start,
                "step": self._host.step,
                "apply": self._host.apply,
                "liquidate": self._host.liquidate,
            }[op](payload)
        assert self._child is not None
        self._child.send({"op": op, **payload})
        return self._child.read_until_result()

    def alive(self) -> bool:
        return self._host is not None or (self._child is not None and self._child.proc.poll() is None)

    def close(self) -> None:
        if self._child is not None:
            self._child.close()
