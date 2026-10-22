"""Skills、进程内 hooks 与 MCP 配置管理；MCP 探测/调用需本地模式显式开启。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from ..agents import hooks as hook_registry
from ..agents import mcp_client
from ..agents import skills as skill_store
from .deps import get_services

router = APIRouter(tags=["扩展：技能 / 钩子 / MCP"])

MCP_COLLECTION = "mcp_servers"
MAX_MCP_SERVERS = 50
_MCP_NOTE = (
    "默认只保存 MCP 配置，不启动进程；设置 QUANTLAB_ALLOW_MCP=1 且 server enabled 后，"
    "可用 /probe 与 /call 以受限 stdio 或 HTTP JSON/SSE 协议调用（stdio 仅 python/node/uvx/uv）。"
)


# ---------------------------------------------------------------------------
# skills：只读项目 / 工作区技能目录
# ---------------------------------------------------------------------------
class SkillAttachIn(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=40)


@router.get("/api/skills", summary="列出项目 / 工作区技能（只读 .quantlab/skills/*.md）")
def list_skills() -> dict[str, Any]:
    return skill_store.list_skills(get_services())


@router.get("/api/skills/{name}", summary="查看一个技能的内容（只读）")
def get_skill(name: str) -> dict[str, Any]:
    try:
        return skill_store.read_skill(get_services(), name)
    except skill_store.SkillError as exc:
        raise HTTPException(exc.status, exc.message) from exc


@router.post("/api/skills/{name}/attach", summary="把一个技能追加到 Lab 项目的 instructions（可选注入）")
def attach_skill(name: str, payload: SkillAttachIn) -> dict[str, Any]:
    from ..agents.lab_state import MAX_INSTRUCTIONS_CHARS
    from .routes_lab import _lab, _project_or_404

    try:
        block = skill_store.skill_instructions(get_services(), name)
    except skill_store.SkillError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    lab = _lab()
    project = _project_or_404(lab, payload.project_id)
    current = str(project.get("instructions") or "").strip()
    merged = f"{current}\n\n{block}".strip() if current else block
    truncated = len(merged) > MAX_INSTRUCTIONS_CHARS
    instructions = merged[:MAX_INSTRUCTIONS_CHARS]
    lab.store.patch(project["id"], instructions=instructions)
    return {
        "ok": True,
        "skill": name,
        "project_id": project["id"],
        "chars": len(instructions),
        "truncated": truncated,
        "note": "技能内容已追加到项目 instructions；不会自动执行技能里的任何内容。",
    }


# ---------------------------------------------------------------------------
# hooks：进程内注册表（只读 API，默认不启用、不执行外部命令）
# ---------------------------------------------------------------------------
@router.get("/api/hooks", summary="列出已注册 hooks 与触发点（只读；默认不启用、不执行外部命令）")
def list_hooks() -> dict[str, Any]:
    return hook_registry.list_hooks()


class HookToggleIn(BaseModel):
    enabled: bool


@router.post("/api/hooks/{hook_id}/toggle", summary="启用/停用进程内 hook（仅本地项目或管理员；不能注册任意代码）")
def toggle_hook(hook_id: str, payload: HookToggleIn) -> dict[str, Any]:
    from ..platform import context

    if not getattr(get_services(), "local", None) and not context.current().is_admin:
        raise HTTPException(403, "只有本地项目或工作区管理员可以切换 hook")
    record = hook_registry.set_enabled(hook_id, payload.enabled)
    if record is None:
        raise HTTPException(404, "hook 不存在")
    return {"ok": True, "hook": record, "note": "只切换进程内受信 handler 的开关，不执行外部命令。"}


# ---------------------------------------------------------------------------
# MCP：配置 / 目录 / 健康检查（不启动进程、不建立连接）
# ---------------------------------------------------------------------------
class McpServerIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    transport: Literal["stdio", "http"] = "stdio"
    command: str = Field("", max_length=500, description="stdio 启动命令（仅保存配置，绝不执行）")
    args: list[str] = Field(default_factory=list, max_length=32)
    url: str = Field("", max_length=1000, description="http 传输的服务地址（仅保存配置，不连接）")
    description: str = Field("", max_length=500)
    enabled: bool = Field(False, description="是否启用（当前仅影响健康状态展示，不会启动）")

    @field_validator("command", "url", "description")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("配置里不允许空字符")
        return value.strip()

    @field_validator("args")
    @classmethod
    def _clean_args(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip()[:200] for item in value if str(item).strip()]
        if any("\x00" in item for item in cleaned):
            raise ValueError("参数里不允许空字符")
        return cleaned[:32]

    @model_validator(mode="after")
    def _transport_fields(self) -> McpServerIn:
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio 传输需要 command（只保存配置，不会执行）")
        if self.transport == "http" and not self.url.startswith(("http://", "https://")):
            raise ValueError("http 传输需要 http(s):// 开头的 url（只保存配置，不会连接）")
        return self


def _mcp_health(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "not_probed" if record.get("enabled") else "disabled",
        "started": False,
        "implemented": True,
        "detail": _MCP_NOTE,
    }


def _mcp_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "name": record.get("name") or record.get("id"),
        "transport": record.get("transport") or "stdio",
        "command": record.get("command") or "",
        "args": list(record.get("args") or []),
        "url": record.get("url") or "",
        "description": record.get("description") or "",
        "enabled": bool(record.get("enabled")),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "health": _mcp_health(record),
        "executes_processes": False,
    }


@router.get("/api/mcp/servers", summary="MCP server 目录与健康检查（不启动进程、不建立连接）")
def list_mcp_servers() -> dict[str, Any]:
    rows = get_services().store.list(MCP_COLLECTION)
    servers = [_mcp_view(row) for row in rows]
    servers.sort(key=lambda item: item["name"])
    return {
        "servers": servers,
        "count": len(servers),
        "calls_allowed": bool(getattr(get_services(), "local", None)) and mcp_client.allow_mcp(),
        "max_servers": MAX_MCP_SERVERS,
        "transports": [
            {"id": "stdio", "label": "本地 stdio 进程", "implemented": True},
            {"id": "http", "label": "HTTP JSON / SSE 端点", "implemented": True},
        ],
        "implemented": True,
        "executes_processes": False,
        "note": _MCP_NOTE,
    }


@router.post("/api/mcp/servers", summary="保存一个 MCP server 配置（只存本地，不启动进程）")
def save_mcp_server(payload: McpServerIn) -> dict[str, Any]:
    _mcp_manage()
    store = get_services().store
    existing = store.get(MCP_COLLECTION, payload.name)
    if existing is None and len(store.list(MCP_COLLECTION)) >= MAX_MCP_SERVERS:
        raise HTTPException(400, f"最多保存 {MAX_MCP_SERVERS} 个 MCP server 配置")
    record = {
        "id": payload.name,
        "name": payload.name,
        "transport": payload.transport,
        "command": payload.command,
        "args": payload.args,
        "url": payload.url,
        "description": payload.description,
        "enabled": bool(payload.enabled),
    }
    if existing is not None:
        record["created_at"] = existing.get("created_at")
    saved = store.upsert(MCP_COLLECTION, record)
    # 明确：这里只写配置，不 spawn、不连接、不改 PATH；健康状态永远是未探测
    return {"server": _mcp_view(saved), "note": _MCP_NOTE}


def _mcp_manage() -> None:
    from ..platform import context

    if not getattr(get_services(), "local", None) and not context.current().is_admin:
        raise HTTPException(403, "只有本地项目或工作区管理员可以管理 MCP")


@router.delete("/api/mcp/servers/{name}", summary="删除 MCP 配置")
def delete_mcp_server(name: str) -> dict[str, Any]:
    _mcp_manage()
    if not get_services().store.delete(MCP_COLLECTION, name):
        raise HTTPException(404, "MCP server 不存在")
    return {"ok": True}


class McpCallIn(BaseModel):
    tool: str = Field(..., min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


def _mcp_record(name: str) -> dict[str, Any]:
    """probe/call 的共用安全闸：本地模式 + 显式 allow + server 已启用。"""
    if not getattr(get_services(), "local", None):
        raise HTTPException(403, "MCP 调用仅在本地项目模式可用")
    if not mcp_client.allow_mcp():
        raise HTTPException(403, "MCP 未开启：请设置 QUANTLAB_ALLOW_MCP=1 后重启本地服务")
    record = get_services().store.get(MCP_COLLECTION, name)
    if record is None:
        raise HTTPException(404, "MCP server 不存在")
    if not record.get("enabled"):
        raise HTTPException(400, "MCP server 未启用；先在配置中 enabled=true")
    if record.get("transport") not in ("stdio", "http"):
        raise HTTPException(400, "目前只实现 stdio / HTTP MCP")
    return record


@router.post(
    "/api/mcp/servers/{name}/probe", summary="探测 stdio MCP server：initialize + tools/list（仅本地、显式开启）"
)
def probe_mcp_server(name: str) -> dict[str, Any]:
    record = _mcp_record(name)
    try:
        result = mcp_client.probe_server(record)
    except mcp_client.MCPError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "server": name, **result, "note": "每次 probe 结束后会关闭子进程，不保持常驻连接。"}


@router.post("/api/mcp/servers/{name}/call", summary="调用 stdio MCP server 的一个工具（仅本地、显式开启）")
def call_mcp_tool(name: str, payload: McpCallIn) -> dict[str, Any]:
    record = _mcp_record(name)
    try:
        result = mcp_client.call_tool(record, payload.tool, payload.arguments)
    except mcp_client.MCPError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "server": name, "tool": payload.tool, "result": result}
