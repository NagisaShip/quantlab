"""受限 MCP HTTP 客户端：JSON-RPC 2.0 over Streamable HTTP / JSON response.

只支持显式配置的 http(s) URL；不跟随重定向、不使用环境代理，超时短；
transport 仍需本地模式 + QUANTLAB_ALLOW_MCP=1 + enabled。SSE 解析仅取 ``data:`` 行。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPError(RuntimeError):
    pass


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        return json.loads(payload)
                    except ValueError:
                        continue
        raise MCPError("MCP SSE 响应没有可解析的 data JSON")
    try:
        body = response.json()
    except ValueError as exc:
        raise MCPError("MCP HTTP 响应不是 JSON") from exc
    if not isinstance(body, dict):
        raise MCPError("MCP HTTP 响应不是 JSON 对象")
    return body


class MCPHttpClient:
    def __init__(self, url: str, *, timeout: float = 8.0) -> None:
        if not str(url or "").startswith(("http://", "https://")):
            raise MCPError("MCP HTTP 需要 http(s):// URL")
        self.url = str(url)
        self.timeout = max(1.0, min(float(timeout or 8.0), 30.0))
        self._id = 0
        self._session: str = ""
        self.client = httpx.Client(
            timeout=self.timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self.client.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    def _post(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.post(self.url, json=message, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPError(f"MCP HTTP 请求失败：{exc}") from exc
        if response.status_code >= 400:
            raise MCPError(f"MCP HTTP {response.status_code}：{response.text[:300]}")
        session = response.headers.get("mcp-session-id")
        if session:
            self._session = session
        if "id" not in message:
            return {}
        return _parse_response(response)

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        payload = self._post(message)
        if payload.get("error"):
            err = payload["error"]
            raise MCPError(f"MCP 错误：{err.get('message') or err}")
        return payload.get("result") or {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._post(message)

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
        result = self._request("tools/call", {"name": str(name), "arguments": arguments or {}})
        return result if isinstance(result, dict) else {"result": result}
