"""AI 策略助手 API。

所有接口在没有模型密钥时都能工作（确定性降级），返回体里的 `mode` 字段
明确告诉前端这次是 "llm" 还是 "fallback"。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agents.copilot import CopilotResult, StrategyCopilot
from ..store import Collections
from ..strategies.repo import StrategyRepository
from .deps import get_services
from .schemas import AiChatIn, AiCodeIn, AiEditIn, AiFixIn, AiGenerateIn, AiInterpretIn, AiReviewIn, ChatTurn

router = APIRouter(prefix="/api/ai", tags=["AI 策略助手"])


def _copilot() -> StrategyCopilot:
    return StrategyCopilot(get_services().router)


def _log(result: CopilotResult, summary: str) -> dict[str, Any]:
    """留痕：动作、模式、模型、提示词哈希。便于复现与审计。"""
    row = {
        "action": result.action,
        "mode": result.mode,
        "model": result.model,
        "prompt_hash": result.prompt_hash,
        "attempts": result.attempts,
        "checks_ok": (result.checks or {}).get("ok"),
        "summary": summary[:200],
        "elapsed_ms": result.elapsed_ms,
        "created_at": time.time(),
    }
    get_services().store.upsert(Collections.AI_SESSIONS, row)
    return result.to_dict()


def _history(turns) -> list[dict[str, str]]:
    return [t.model_dump() for t in turns]


@router.post("/generate")
def ai_generate(payload: AiGenerateIn) -> dict:
    """自然语言 → 策略代码（自检 + 试跑 + 自动修复）。`save=true` 时直接存为草稿策略。"""
    result = _copilot().generate(
        payload.description,
        securities=[s.strip().upper() for s in payload.securities if s.strip()] or None,
        timeframe=payload.timeframe,
        history=_history(payload.history),
    )
    body = _log(result, payload.description)
    if payload.save and result.code:
        repo = StrategyRepository(get_services().store.root / "strategies")
        name = (payload.name or result.name or "AI 策略")[:60]
        origin = f"模型 {result.model}" if result.mode == "llm" else "模板降级"
        rec = repo.save(
            name=name,
            source=result.code,
            params=result.params,
            team="AI 生成",
            kind="code",
            origin="agent",
            symbol=result.securities[0],
            timeframe=result.timeframe,
            securities=result.securities,
            notes=f"AI 生成（{origin}）：{payload.description[:200]}",
            slug=repo.unique_slug(name),
            prompt_hash=result.prompt_hash,
        )
        body["strategy"] = rec.to_dict(include_source=False)
    return body


@router.post("/edit")
def ai_edit(payload: AiEditIn) -> dict:
    result = _copilot().edit(
        payload.code,
        payload.instruction,
        securities=payload.securities,
        timeframe=payload.timeframe,
        history=_history(payload.history),
    )
    return _log(result, payload.instruction)


@router.post("/fix")
def ai_fix(payload: AiFixIn) -> dict:
    result = _copilot().fix(payload.code, payload.errors, securities=payload.securities, timeframe=payload.timeframe)
    return _log(result, "; ".join(payload.errors)[:200])


@router.post("/explain")
def ai_explain(payload: AiCodeIn) -> dict:
    return _log(_copilot().explain(payload.code), "explain")


@router.post("/interpret")
def ai_interpret(payload: AiInterpretIn) -> dict:
    bt = payload.result
    if payload.backtest_id:
        bt = get_services().store.get_blob("backtests", payload.backtest_id)
        if bt is None:
            raise HTTPException(404, "回测记录不存在")
    if not bt:
        raise HTTPException(400, "请提供 backtest_id 或 result")
    return _log(_copilot().interpret(bt), f"interpret {payload.backtest_id or ''}")


@router.post("/review")
def ai_review(payload: AiReviewIn) -> dict:
    bt = None
    if payload.backtest_id:
        bt = get_services().store.get_blob("backtests", payload.backtest_id)
    code = payload.code or (bt or {}).get("code", "")
    if not code:
        raise HTTPException(400, "请提供代码或回测记录")
    return _log(_copilot().review(code, bt, title=payload.title), f"review {payload.title}")


@router.post("/chat")
def ai_chat(payload: AiChatIn) -> dict:
    return _log(
        _copilot().chat(payload.question, code=payload.code, history=_history(payload.history)), payload.question
    )


@router.get("/history")
def ai_history(limit: int = Query(30, ge=1, le=200)) -> dict:
    return {"items": get_services().store.list(Collections.AI_SESSIONS, limit=limit)}


# ---------------------------------------------------------------------------
# 页面内「问 AI」：右侧面板，带页面资料、可选模型与思考努力程度、流式输出
# ---------------------------------------------------------------------------
class AskContextItem(BaseModel):
    """页面附带的一份资料。服务端会再截断（条数、单条、合计），并当作数据而非指令。"""

    kind: str = Field("page", max_length=40)
    title: str = Field("", max_length=400)
    text: str = Field("", max_length=200_000)


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=8000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)
    context: list[AskContextItem] = Field(default_factory=list, max_length=12)
    model: str = Field("", max_length=200)  # provider/model；空则跟随全局路由
    effort: Literal["", "low", "medium", "high", "max"] = ""
    page: str = Field("", max_length=40)  # 当前页面：决定默认加载哪些平台工具
    scenes: list[str] | None = Field(None, max_length=8)  # 用户在「更多」菜单里勾选的工具场景；None = 按页面默认


def _scenes(payload: AskIn) -> list[str]:
    from ..agents import platform_tools as PT

    return PT.scenes_for(payload.page, payload.scenes)


def _ask_view(result: CopilotResult) -> dict[str, Any]:
    return {
        "mode": result.mode,
        "content": result.content,
        "model": result.model,
        "thinking": result.thinking,
        "code": result.code,
        "checks": result.checks,
        "notes": result.notes,
        "elapsed_ms": result.elapsed_ms,
        "tool_calls": result.tool_calls,
    }


@router.get("/ask/meta", summary="问 AI 面板元信息：可选模型、默认模型、思考努力程度、模型是否就绪")
def ask_meta() -> dict:
    s = get_services()
    current = getattr(s.router, "current_target", None)
    models = s.router.available_models() if hasattr(s.router, "available_models") else []
    return {
        "models": models,
        "default_model": current("default") if callable(current) else "",
        "efforts": [
            {"id": "low", "label": "低", "hint": "快速回答，少想"},
            {"id": "medium", "label": "中", "hint": "大多数任务"},
            {"id": "high", "label": "高", "hint": "复杂策略与排错"},
            {"id": "max", "label": "极高", "hint": "最深入，最慢"},
        ],
        "llm_ready": any(m.get("ready") and m.get("provider") != "local" for m in models),
    }


@router.post("/ask", summary="页面内问 AI（一次性返回）：附带页面资料提问，没有模型时规则降级")
def ask(payload: AskIn) -> dict:
    from ..platform import context as reqctx

    result = _copilot().ask(
        payload.question,
        context=[c.model_dump() for c in payload.context],
        history=_history(payload.history),
        model=payload.model,
        effort=payload.effort,
        scenes=_scenes(payload),
        user_id=reqctx.current().user_id,
    )
    _log(result, payload.question)
    return _ask_view(result)


@router.post("/ask/stream", summary="页面内问 AI（流式 SSE）：逐段推送思考与正文，最后推送定稿")
def ask_stream(payload: AskIn) -> StreamingResponse:
    """SSE 事件（data 为 JSON）：
    - {"type": "delta", "kind": "text" | "thinking", "text": "…"}  模型流式输出
    - {"type": "reset"}  换到下一个候选模型重来，前端清掉半句话
    - {"type": "final", "mode": "llm" | "fallback", "content", "model", "thinking", "code", "notes", …}
    - {"type": "error", "message": "…"}
    最后一条为 `event: done`。
    """
    from ..platform import context as reqctx

    ctx = reqctx.current()  # 后台线程里显式带上本次请求的工作区身份
    q: queue.Queue[dict[str, Any] | None] = queue.Queue()
    question = payload.question
    items = [c.model_dump() for c in payload.context]
    history = _history(payload.history)
    scenes = _scenes(payload)

    def on_tool(rec: dict[str, Any]) -> None:
        q.put({"type": "tool", **rec})

    def on_delta(kind: str, text: str) -> None:
        if kind == "reset":
            q.put({"type": "reset"})
        elif text:
            q.put({"type": "delta", "kind": "thinking" if kind == "thinking" else "text", "text": text})

    def work() -> None:
        reqctx.set_context(ctx)
        try:
            result = _copilot().ask(
                question,
                context=items,
                history=history,
                model=payload.model,
                effort=payload.effort,
                on_delta=on_delta,
                scenes=scenes,
                user_id=ctx.user_id,
                on_tool=on_tool,
            )
            try:
                _log(result, question)
            except Exception:  # 留痕失败不影响回答
                pass
            q.put({"type": "final", **_ask_view(result)})
        except Exception as exc:  # 任何异常都要告诉前端，而不是让流悬空
            q.put({"type": "error", "message": str(exc)[:300] or "生成失败"})
        finally:
            q.put(None)

    threading.Thread(target=work, name="ask-ai", daemon=True).start()

    def gen():
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"  # 注释行：长时间思考时保持连接
                continue
            if item is None:
                yield "event: done\ndata: {}\n\n"
                return
            yield f"data: {json.dumps(item, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---------------------------------------------------------------------------
# 平台工具目录与待确认动作
# ---------------------------------------------------------------------------
@router.get("/tools", summary="「问 AI」可调用的平台工具：场景、用途、何时可用、风险级别")
def ai_tools() -> dict:
    from ..agents import platform_tools as PT

    return PT.catalog()


class ActionDecisionIn(BaseModel):
    approve: bool


@router.post("/actions/{action_id}", summary="确认或取消一个需要确认的 AI 操作（只有发起人可以）")
def ai_action_decide(action_id: str, payload: ActionDecisionIn) -> dict:
    from ..agents import platform_tools as PT
    from ..platform import context as reqctx

    try:
        act = PT.confirm(action_id, user=reqctx.current().user_id, approve=payload.approve)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"action": act}


@router.get("/actions/{action_id}", summary="一个 AI 操作的状态")
def ai_action_get(action_id: str) -> dict:
    from ..agents import platform_tools as PT

    act = get_services().store.get(PT.ACTIONS, action_id)
    if act is None:
        raise HTTPException(404, "操作不存在")
    return {"action": act}
