"""A tiny MCP stdio server for tests: initialize, tools/list, tools/call."""

import json
import sys


def reply(rid, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        reply(
            rid,
            {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake", "version": "0.0.1"}, "capabilities": {}},
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        reply(rid, {"tools": [{"name": "echo", "description": "echo text", "inputSchema": {"type": "object"}}]})
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        reply(rid, {"content": [{"type": "text", "text": f"echo: {args.get('text', '')}"}], "isError": False})
    elif rid is not None:
        reply(rid, error={"code": -32601, "message": f"unknown method {method}"})
