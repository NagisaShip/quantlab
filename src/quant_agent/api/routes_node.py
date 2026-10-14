"""运行节点：健康状况、手机看板数据、聊天机器人（配置、绑定码、回调）。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..notify import bot as B
from ..platform import context, watchdog
from .deps import get_services

router = APIRouter(prefix="/api", tags=["运行节点"])


class BotIn(BaseModel):
    kind: str
    name: str = ""
    creds: dict[str, str] = Field(default_factory=dict)


@router.get("/node/health", summary="节点健康：心跳、失联实验、时钟偏差、限频")
def node_health() -> dict:
    """节点心跳、失联实验、交易所时钟偏差、限频状态。"""
    return watchdog.health(get_services())


@router.get("/node/board", summary="手机看板：运行中的实验与节点状态（只读）")
def node_board() -> dict:
    """手机看板：运行中的实验一览（只读）+ 节点状态。"""
    s = get_services()
    ws = context.current_workspace()
    bot = B.BotService(s)
    rows = []
    for r in bot._sessions(ws):
        pts = r.get("equity_points") or []
        ret = pts[-1]["v"] / pts[0]["v"] - 1 if len(pts) > 1 and pts[0].get("v") else None
        port = r.get("portfolio") or {}
        rows.append(
            {
                "id": r["id"],
                "strategy": r.get("strategy_name"),
                "execution": r.get("execution") or "paper",
                "state": r["run_state"],
                "return": ret,
                "equity": port.get("total_value"),
                "positions": [
                    {"security": k, "amount": (v or {}).get("amount")}
                    for k, v in (port.get("positions") or {}).items()
                    if float((v or {}).get("amount") or 0)
                ],
                "halted": bool((r.get("demo") or {}).get("halted")),
                "last_update": r.get("last_update"),
                "spark": [p["v"] for p in pts[-60:]],
                "last_fill": (r.get("fills") or [None])[-1],
            }
        )
    alerts = s.db.fetchall(
        "SELECT title, severity, created_at FROM alerts WHERE workspace_id=? "
        "AND status != 'skipped' ORDER BY id DESC LIMIT 5",
        (ws,),
    )
    return {"sessions": rows, "node": watchdog.health(s), "alerts": alerts}


# ---------------------------------------------------------------------- 机器人配置
@router.get("/bot", summary="聊天机器人列表与已绑定的聊天")
def bot_list() -> dict:
    s = get_services()
    ws = context.current_workspace()
    return {
        "bots": B.list_bots(s, ws),
        "kinds": B.KINDS,
        "bindings": [
            {k: b.get(k) for k in ("id", "platform", "user_id", "bound_at")} for b in B.BotService(s).bindings(ws)
        ],
    }


@router.post("/bot", summary="添加聊天机器人（飞书 / 企业微信 / Telegram）")
def bot_create(body: BotIn) -> dict:
    if not context.current().is_ws_admin:
        raise HTTPException(403, "只有工作区管理员可以配置机器人")
    try:
        return {
            "bot": B.save_bot(
                get_services(),
                context.current_workspace(),
                context.current().user_id or "",
                body.kind,
                body.name,
                body.creds,
            )
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/bot/{bot_id}", summary="删除聊天机器人")
def bot_delete(bot_id: str) -> dict:
    if not context.current().is_ws_admin:
        raise HTTPException(403, "只有工作区管理员可以删除机器人")
    return {"ok": B.delete_bot(get_services(), context.current_workspace(), bot_id)}


@router.post("/bot/bind-code", summary="生成一次性绑定码")
def bot_bind_code() -> dict:
    """一次性绑定码（10 分钟有效）：在聊天里发「绑定 123456」把那个聊天绑到你。"""
    return B.BotService(get_services()).new_bind_code(context.current_workspace(), context.current().user_id or "")


@router.delete("/bot/bindings/{binding_id}", summary="解除聊天绑定")
def bot_unbind(binding_id: str) -> dict:
    return {"ok": B.BotService(get_services()).unbind(context.current_workspace(), binding_id)}


# ---------------------------------------------------------------------- 回调（无登录，靠各家签名校验）
def _cfg(kind: str, bot_id: str) -> dict[str, str]:
    got = B.creds_of(get_services(), bot_id)
    if got is None or got[0]["kind"] != kind:
        raise HTTPException(404, "机器人不存在")
    return got[1]


@router.post("/bot/hook/feishu/{bot_id}", summary="飞书事件回调")
async def hook_feishu(bot_id: str, request: Request) -> Any:
    cfg = _cfg("feishu", bot_id)
    try:
        body = json.loads(await request.body() or b"{}")
        return B.Feishu(cfg).callback(B.BotService(get_services()), body)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.get("/bot/hook/wecom/{bot_id}", summary="企业微信回调地址校验")
def hook_wecom_verify(bot_id: str, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> Any:
    try:
        return PlainTextResponse(B.WeCom(_cfg("wecom", bot_id)).verify_url(msg_signature, timestamp, nonce, echostr))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc


@router.post("/bot/hook/wecom/{bot_id}", summary="企业微信消息回调")
async def hook_wecom(bot_id: str, request: Request, msg_signature: str, timestamp: str, nonce: str) -> Any:
    try:
        B.WeCom(_cfg("wecom", bot_id)).callback(
            B.BotService(get_services()),
            msg_signature,
            timestamp,
            nonce,
            (await request.body()).decode("utf-8", "replace"),
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return PlainTextResponse("success")
