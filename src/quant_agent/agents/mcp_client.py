"""受限 MCP stdio 客户端：只启动本地配置的 MCP server，JSON-RPC 2.0 行协议。

安全边界：
- 只在本地模式、显式启用、且设置 ``QUANTLAB_ALLOW_MCP=1`` 时才会 spawn；
- 命令必须显式来自配置且可执行文件 basename 在允许列表（python/node/uvx/uv）；
- 不使用 shell，不走网络（HTTP/SSE 仍未实现）；
- 每次 probe/call 结束都 terminate 子进程，不做常驻连接。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

MCP_PROTOCOL_VERSION = "2024-11-05"
ALLOWED_COMMANDS = {"python", "python.exe", "python3", "node", "node.exe", "uvx", "uv", "uv.exe"}
MAX_ARGS = 32
MAX_TOOL_NAME = 128


class MCPError(RuntimeError):
    pass


def allow_mcp() -> bool:
    return os.environ.get("QUANTLAB_ALLOW_MCP", "").strip().lower() in ("1", "true", "yes", "on")


def _command_allowed(command: str) -> bool:
    name = Path(command).name.lower()
    return name in ALLOWED_COMMANDS


class MCPStdioClient:
    def __init__(self, command: str, args: list[str] | None = None, *, timeout: float = 8.0) -> None:
        if not _command_allowed(command):
            raise MCPError(f"MCP stdio 命令不在允许列表：{Path(command).name}（只允许 python/node/uvx/uv）")
        self.command = command
        self.args = [str(item) for item in (args or [])][:MAX_ARGS]
        self.timeout = max(1.0, min(float(timeout or 8.0), 30.0))
        self.proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str] = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 1

    def __enter__(self) -> MCPStdioClient:
        self.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def start(self) -> None:
        if not allow_mcp():
            raise MCPError("未开启 MCP：设置 QUANTLAB_ALLOW_MCP=1 后才允许启动本地 MCP server")
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
            )
        except OSError as exc:
            raise MCPError(f"启动 MCP server 失败：{exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._lines.put(line)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            if len(self._stderr) < 50:
                self._stderr.append(line.strip()[:500])

    def _stderr_text(self) -> str:
        return " | ".join(self._stderr[-5:])

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError("MCP server 尚未启动")
        rid = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except OSError as exc:
            raise MCPError(f"写入 MCP server 失败：{exc}") from exc
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                break
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if payload.get("id") != rid:
                continue
            if payload.get("error"):
                err = payload["error"]
                raise MCPError(f"MCP 错误：{err.get('message') or err}")
            return payload.get("result") or {}
        detail = f"；stderr：{self._stderr_text()}" if self._stderr else ""
        raise MCPError(f"MCP 请求超时（{method}，{self.timeout:g}s）{detail}")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self.proc is None or self.proc.stdin is None:
            return
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except OSError:
            pass

    def initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "quantlab", "version": "0.1"},
            },
        )
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = str(name or "").strip()
        if not tool or len(tool) > MAX_TOOL_NAME:
            raise MCPError("工具名无效")
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            proc.stdin and proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


def probe_server(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("transport") == "http":
        from .mcp_http import MCPHttpClient

        client = MCPHttpClient(record.get("url") or "")
        try:
            info = client.initialize()
            tools = client.list_tools()
        finally:
            client.close()
        return {
            "serverInfo": info.get("serverInfo") or {},
            "protocolVersion": info.get("protocolVersion") or MCP_PROTOCOL_VERSION,
            "tools": tools,
            "tool_count": len(tools),
        }
    if record.get("transport") != "stdio":
        raise MCPError("目前只实现 stdio / HTTP MCP；其他传输尚未实现")
    with MCPStdioClient(record.get("command") or "", list(record.get("args") or [])) as client:
        info = client.initialize()
        tools = client.list_tools()
        return {
            "serverInfo": info.get("serverInfo") or {},
            "protocolVersion": info.get("protocolVersion") or MCP_PROTOCOL_VERSION,
            "tools": tools,
            "tool_count": len(tools),
        }


def call_tool(record: dict[str, Any], name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    if record.get("transport") == "http":
        from .mcp_http import MCPHttpClient

        client = MCPHttpClient(record.get("url") or "")
        try:
            client.initialize()
            return client.call_tool(name, arguments)
        finally:
            client.close()
    if record.get("transport") != "stdio":
        raise MCPError("目前只实现 stdio / HTTP MCP；其他传输尚未实现")
    with MCPStdioClient(record.get("command") or "", list(record.get("args") or [])) as client:
        client.initialize()
        return client.call_tool(name, arguments)
