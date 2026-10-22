"""MCP / skills / hooks 最小闭环：只读技能、只登记 hooks、MCP 只存配置不启动进程。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents import hooks as hook_registry
from quant_agent.agents import skills as skill_store
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture()
def local_client(tmp_path, monkeypatch):
    project = tmp_path / "研究项目"
    project.mkdir()
    monkeypatch.setenv("QUANTLAB_MODE", "local")
    monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
    monkeypatch.setenv("QUANTLAB_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("QUANTLAB_CREDENTIALS", str(tmp_path / "keys"))
    reset_services()
    set_services(AppServices.build(store_root=str(project / ".quantlab")))
    with TestClient(create_app()) as c:
        c.headers["X-Requested-With"] = "quantlab-test"
        yield c, project, tmp_path
    reset_services()


@pytest.fixture()
def account_client(tmp_path, monkeypatch):
    for env in ("QUANTLAB_MODE", "QUANTLAB_PROJECT", "QUANTLAB_LIBRARY", "QUANTLAB_CREDENTIALS"):
        monkeypatch.delenv(env, raising=False)
    reset_services()
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _skills_dir(client) -> Path:
    body = client.get("/api/skills")
    assert body.status_code == 200, body.text
    root = Path(body.json()["root"])
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write_skill(root: Path, name: str, text: str) -> Path:
    path = root / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# skills：列表 / 详情 / 拒绝穿越与超大文件
# ---------------------------------------------------------------------------
def test_skills_list_and_detail_are_read_only(local_client):
    c, _project, _tmp = local_client
    empty = c.get("/api/skills").json()
    assert empty["count"] == 0 and empty["skills"] == [] and empty["exists"] is False
    assert empty["note"]

    root = _skills_dir(c)
    _write_skill(root, "alpha", "# Alpha 技能\n\n用 RSI 判断超卖，再结合成交量确认。\n")
    _write_skill(root, "beta", "普通说明，没有标题。\n")
    (root / "notes.txt").write_text("不是 markdown", encoding="utf-8")
    (root / ".hidden.md").write_text("# 隐藏", encoding="utf-8")
    (root / "subdir").mkdir()

    listing = c.get("/api/skills").json()
    assert listing["exists"] is True and listing["count"] == 2
    names = {item["name"]: item for item in listing["skills"]}
    assert set(names) == {"alpha", "beta"}
    assert names["alpha"]["title"] == "Alpha 技能"
    assert names["alpha"]["description"].startswith("用 RSI")
    assert names["alpha"]["size"] > 0 and names["alpha"]["modified_at"] > 0
    assert names["beta"]["title"] == "beta" and names["beta"]["description"].startswith("普通说明")
    skipped = {item["name"]: item["reason"] for item in listing["skipped"]}
    assert skipped["notes.txt"] == "not_markdown"
    assert skipped["subdir"] == "directory"
    assert ".hidden.md" in skipped  # stem ".hidden" 不是合法技能名

    detail = c.get("/api/skills/alpha")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["name"] == "alpha" and body["file"] == "alpha.md"
    assert "RSI 判断超卖" in body["content"] and body["size"] == len(body["content"].encode("utf-8"))

    assert c.get("/api/skills/missing").status_code == 404


def test_skills_reject_path_traversal_and_bad_names(local_client):
    c, project, _tmp = local_client
    root = _skills_dir(c)
    _write_skill(root, "ok", "# ok")
    # 技能目录外的机密文件：任何穿越都不应该能读到
    (project / ".quantlab" / "secret.md").write_text("TOPSECRET", encoding="utf-8")

    for url in (
        "/api/skills/..%2Fsecret",
        "/api/skills/..%5Csecret",
        "/api/skills/%2e%2e%2fsecret",
        "/api/skills/ok/..%2F..%2Fsecret",
    ):
        response = c.get(url)
        assert response.status_code in (400, 404), (url, response.status_code)
        assert "TOPSECRET" not in response.text, url
    assert c.get("/api/skills/a/b").status_code == 404
    # 服务端校验本身也拒绝 ".." / "../secret" 这类名字（不依赖前端/客户端是否先做 URL 归一化）
    assert skill_store.validate_name("ok") == "ok"
    for bad in ("..", "../secret", "a/b", "..\\secret", "", "a" * 65):
        with pytest.raises(skill_store.InvalidSkillName):
            skill_store.validate_name(bad)
    assert "TOPSECRET" not in c.get("/api/skills").text


def test_skills_reject_oversized_files(local_client):
    c, _project, _tmp = local_client
    root = _skills_dir(c)
    _write_skill(root, "huge", "x" * (skill_store.MAX_SKILL_BYTES + 100))
    listing = c.get("/api/skills").json()
    assert listing["count"] == 0
    assert {item["name"]: item["reason"] for item in listing["skipped"]}["huge"] == "too_large"
    response = c.get("/api/skills/huge")
    assert response.status_code == 413, response.text
    assert "上限" in response.json()["message"]


def test_skills_are_workspace_scoped_in_account_mode(account_client):
    c = account_client
    body = c.get("/api/skills").json()
    root = Path(body["root"])
    assert root.name == "skills" and "workspaces" in root.parts
    root.mkdir(parents=True, exist_ok=True)
    _write_skill(root, "gamma", "# Gamma\n\n工作区级技能。\n")
    listing = c.get("/api/skills").json()
    assert [item["name"] for item in listing["skills"]] == ["gamma"]
    assert c.get("/api/skills/gamma").json()["content"].startswith("# Gamma")


def test_skill_can_be_attached_to_project_instructions(local_client):
    c, _project, _tmp = local_client
    root = _skills_dir(c)
    _write_skill(root, "alpha", "# Alpha 技能\n\n先看资金费率再看持仓量。\n")
    created = c.post("/api/lab/projects", json={"kind": "research", "title": "研究"})
    assert created.status_code == 200, created.text
    pid = created.json()["project"]["id"]

    attached = c.post("/api/skills/alpha/attach", json={"project_id": pid})
    assert attached.status_code == 200, attached.text
    assert attached.json()["skill"] == "alpha" and attached.json()["truncated"] is False
    project = c.get(f"/api/lab/projects/{pid}").json()["project"]
    assert "【技能：alpha】" in project["instructions"]
    assert "先看资金费率" in project["instructions"]

    assert c.post("/api/skills/missing/attach", json={"project_id": pid}).status_code == 404
    assert c.post("/api/skills/alpha/attach", json={"project_id": "nope"}).status_code == 404


# ---------------------------------------------------------------------------
# hooks：只读注册表，默认不启用、不执行外部命令
# ---------------------------------------------------------------------------
def test_hooks_are_registered_but_disabled_and_read_only(local_client):
    c, _project, _tmp = local_client
    body = c.get("/api/hooks")
    assert body.status_code == 200, body.text
    data = body.json()
    assert set(data["triggers"]) == {"before_tool", "after_tool"}
    assert data["execution"] == "in_process"
    assert data["external_commands"] is False
    assert data["hooks"] and data["enabled"] == []
    assert all(hook["enabled"] is False for hook in data["hooks"])
    assert all(hook["executes_external_commands"] is False for hook in data["hooks"])
    assert {hook["trigger"] for hook in data["hooks"]} <= {"before_tool", "after_tool"}
    assert "不执行外部命令" in data["note"]

    # 只有进程内受信任代码能注册；HTTP 接口是只读的
    assert c.post("/api/hooks", json={}).status_code == 405
    assert c.delete("/api/hooks").status_code == 405

    hook_registry.register_hook("test.custom", name="测试钩子", trigger="before_tool")
    try:
        listed = hook_registry.list_hooks()
        assert any(hook["id"] == "test.custom" for hook in listed["hooks"])
        assert "test.custom" not in listed["enabled"]
    finally:
        assert hook_registry.unregister_hook("test.custom") is True


# ---------------------------------------------------------------------------
# MCP：只保存配置 / 列出目录 / 静态健康状态，不启动进程
# ---------------------------------------------------------------------------
def test_mcp_empty_config_and_catalog(local_client):
    c, _project, _tmp = local_client
    body = c.get("/api/mcp/servers")
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["servers"] == [] and data["count"] == 0
    assert data["implemented"] is True and data["executes_processes"] is False
    assert {item["id"] for item in data["transports"]} == {"stdio", "http"}
    assert all(item["implemented"] is True for item in data["transports"])
    assert "QUANTLAB_ALLOW_MCP=1" in data["note"]


def test_mcp_config_is_saved_but_never_executed(local_client):
    c, _project, tmp = local_client
    marker = tmp / "mcp-should-not-run.txt"
    command = f"{sys.executable} -c \"open(r'{marker}', 'w').write('ran')\""
    saved = c.post(
        "/api/mcp/servers",
        json={
            "name": "demo-local",
            "transport": "stdio",
            "command": command,
            "args": ["--stdio"],
            "description": "仅配置",
        },
    )
    assert saved.status_code == 200, saved.text
    server = saved.json()["server"]
    assert server["name"] == "demo-local" and server["transport"] == "stdio"
    assert server["health"]["status"] == "disabled"
    assert server["health"]["started"] is False and server["health"]["implemented"] is True
    assert server["executes_processes"] is False
    assert not marker.exists(), "保存 MCP 配置时不允许执行 command"

    listed = c.get("/api/mcp/servers").json()
    assert listed["count"] == 1 and listed["servers"][0]["name"] == "demo-local"
    assert not marker.exists(), "读取 MCP 目录时不允许执行 command"

    # 更新配置（enabled=true 只改变健康状态展示，仍然不会启动/连接）
    updated = c.post(
        "/api/mcp/servers",
        json={"name": "demo-local", "transport": "http", "url": "https://example.invalid/mcp", "enabled": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["server"]["health"]["status"] == "not_probed"
    assert updated.json()["server"]["health"]["started"] is False
    assert not marker.exists()


def test_mcp_config_validation(local_client):
    c, _project, _tmp = local_client
    bad_bodies = [
        {"name": "../evil", "transport": "stdio", "command": "x"},
        {"name": "no-command", "transport": "stdio"},
        {"name": "bad-url", "transport": "http", "url": "ftp://example.com/x"},
        {"name": "blank-command", "transport": "stdio", "command": "   "},
    ]
    for body in bad_bodies:
        response = c.post("/api/mcp/servers", json=body)
        assert response.status_code == 422, (body, response.status_code)
    # 合法配置只存本地，响应明确标注未实现
    ok = c.post("/api/mcp/servers", json={"name": "ok", "transport": "http", "url": "http://127.0.0.1:9/mcp"})
    assert ok.status_code == 200
    assert "QUANTLAB_ALLOW_MCP=1" in ok.json()["note"]


def test_mcp_delete_and_capability(local_client, monkeypatch):
    c, _project, _tmp = local_client
    monkeypatch.delenv("QUANTLAB_ALLOW_MCP", raising=False)
    assert c.get("/api/mcp/servers").json()["calls_allowed"] is False
    monkeypatch.setenv("QUANTLAB_ALLOW_MCP", "1")
    assert c.get("/api/mcp/servers").json()["calls_allowed"] is True
    assert c.post("/api/mcp/servers", json={"name": "remove-me", "command": "python"}).status_code == 200
    assert c.delete("/api/mcp/servers/remove-me").status_code == 200
    assert c.delete("/api/mcp/servers/remove-me").status_code == 404
    assert c.get("/api/mcp/servers").json()["count"] == 0


def test_mcp_stdio_probe_and_call_with_allow_flag(local_client, monkeypatch):
    """显式 allow 后可以启动受限 stdio MCP；未 allow 时必须拒绝。"""
    c, _project, _tmp = local_client
    fake = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    body = {
        "name": "fake",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(fake)],
        "enabled": True,
    }
    saved = c.post("/api/mcp/servers", json=body)
    assert saved.status_code == 200, saved.text

    denied = c.post("/api/mcp/servers/fake/probe")
    assert denied.status_code == 403
    body = denied.json()
    assert "QUANTLAB_ALLOW_MCP" in (body.get("detail") or body.get("message") or "")

    monkeypatch.setenv("QUANTLAB_ALLOW_MCP", "1")
    probe = c.post("/api/mcp/servers/fake/probe")
    assert probe.status_code == 200, probe.text
    payload = probe.json()
    assert payload["ok"] is True and payload["tool_count"] == 1
    assert payload["tools"][0]["name"] == "echo"

    called = c.post("/api/mcp/servers/fake/call", json={"tool": "echo", "arguments": {"text": "hello"}})
    assert called.status_code == 200, called.text
    result = called.json()["result"]
    assert "echo: hello" in result["content"][0]["text"]


def test_hooks_can_be_toggled_and_run_in_process(local_client):
    c, _project, _tmp = local_client
    hook_id = "builtin.audit_before_tool"
    enabled = c.post(f"/api/hooks/{hook_id}/toggle", json={"enabled": True})
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["hook"]["enabled"] is True
    listed = c.get("/api/hooks").json()
    assert hook_id in listed["enabled"]

    results = hook_registry.run_hooks("before_tool", {"pid": "p1", "tool": "local_read_text"})
    assert results and results[0]["ok"] is True
    assert any(event.get("tool") == "local_read_text" for event in hook_registry.recent_events())

    off = c.post(f"/api/hooks/{hook_id}/toggle", json={"enabled": False})
    assert off.status_code == 200 and off.json()["hook"]["enabled"] is False
    assert c.post("/api/hooks/not-exist/toggle", json={"enabled": True}).status_code == 404


def test_mcp_http_probe_and_call(local_client, monkeypatch):
    """HTTP transport：initialize / tools/list / tools/call，SSE/JSON 均可。"""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length") or 0)
            message = json.loads(self.rfile.read(length) or b"{}")
            method = message.get("method")
            rid = message.get("id")
            if rid is None:
                self.send_response(202)
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "http-fake"}, "capabilities": {}}
            elif method == "tools/list":
                result = {"tools": [{"name": "echo", "description": "echo"}]}
            elif method == "tools/call":
                text = message.get("params", {}).get("arguments", {}).get("text", "")
                result = {"content": [{"type": "text", "text": f"http-echo: {text}"}], "isError": False}
            else:
                result = {}
            body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c, _project, _tmp = local_client
        url = f"http://127.0.0.1:{server.server_port}/mcp"
        saved = c.post(
            "/api/mcp/servers",
            json={"name": "http-fake", "transport": "http", "url": url, "enabled": True},
        )
        assert saved.status_code == 200, saved.text
        monkeypatch.setenv("QUANTLAB_ALLOW_MCP", "1")
        probe = c.post("/api/mcp/servers/http-fake/probe")
        assert probe.status_code == 200, probe.text
        assert probe.json()["tools"][0]["name"] == "echo"
        called = c.post("/api/mcp/servers/http-fake/call", json={"tool": "echo", "arguments": {"text": "hi"}})
        assert called.status_code == 200, called.text
        assert "http-echo: hi" in called.json()["result"]["content"][0]["text"]
    finally:
        server.shutdown()
        server.server_close()
