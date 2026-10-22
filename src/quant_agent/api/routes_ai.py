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
from pydantic import BaseModel, ConfigDict, Field

from ..agents.copilot import CopilotResult, StrategyCopilot
from ..platform import context
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
        # 会话级 ACL：记录发起人；历史查询默认只看自己的记录。
        "user_id": context.current().user_id or "",
        "workspace_id": context.current().workspace_id or "",
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
def ai_history(
    limit: int = Query(30, ge=1, le=200),
    scope: Literal["mine", "workspace"] = Query(
        "mine", description="mine=只看自己的 AI 会话；workspace=管理员看全工作区"
    ),
) -> dict:
    """AI 会话历史：默认按发起人过滤；只有管理员可显式查看工作区全部。"""
    rows = get_services().store.list(Collections.AI_SESSIONS, limit=limit * 3 if scope == "workspace" else limit)
    me = context.current().user_id or ""
    if scope == "workspace":
        if not context.current().is_admin:
            raise HTTPException(status_code=403, detail="只有工作区管理员可以查看全部 AI 会话")
        picked = rows
    else:
        # 兼容早期没有 user_id 的记录：只在本地单用户模式显示；账号模式按私有处理。
        local = bool(getattr(get_services(), "local", None))
        picked = [r for r in rows if r.get("user_id") == me or (local and not r.get("user_id"))]
    return {"items": picked[:limit], "scope": scope}


# ---------------------------------------------------------------------------
# 页面内「问 AI」：右侧面板，带页面资料、可选模型与思考努力程度、流式输出
# ---------------------------------------------------------------------------
class AskContextItem(BaseModel):
    """页面附带的一份资料。服务端会再截断（条数、单条、合计），并当作数据而非指令。"""

    kind: str = Field("page", max_length=40)
    title: str = Field("", max_length=400)
    text: str = Field("", max_length=200_000)


class AskResearchWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field("", max_length=100)
    title: str = Field("", max_length=200)
    revision: int | None = Field(None, ge=0, le=1_000_000)
    saved: bool = True
    unsaved: bool = False


class AskResearchNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    title: str = Field("", max_length=200)
    kind: str = Field("", max_length=80)
    status: str = Field("", max_length=40)


class AskResearchRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=100)
    mode: str = Field("", max_length=40)
    status: str = Field("", max_length=40)
    revision: int | None = Field(None, ge=0, le=1_000_000)
    # Display intent, not a security claim. The server uses it only together with
    # the real latest-run lookup; `current=true` alone is never trusted.
    current: bool = False
    historical: bool = False
    follows_latest: bool | None = None
    view_mode: Literal["latest", "frozen"] | None = None


class AskResearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port: str = Field(..., min_length=1, max_length=80)
    type: str = Field(..., min_length=1, max_length=40)
    hash: str = Field(..., min_length=1, max_length=128)
    run_id: str = Field(..., min_length=1, max_length=100)
    node_id: str = Field(..., min_length=1, max_length=100)


class AskResearchContext(BaseModel):
    """Versioned optional envelope. No graph, dataset, binary or base64 fields are accepted."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    workspace_id: str = Field("", max_length=100)
    project_name: str = Field("", max_length=200)
    draft_id: str = Field("", max_length=100)
    workflow: AskResearchWorkflow
    selected_node: AskResearchNode | None = None
    run: AskResearchRun | None = None
    outputs: list[AskResearchOutput] = Field(default_factory=list, max_length=6)


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=8000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=40)
    context: list[AskContextItem] = Field(default_factory=list, max_length=12)
    model: str = Field("", max_length=200)  # provider/model；空则跟随全局路由
    effort: Literal["", "low", "medium", "high", "max"] = ""
    page: str = Field("", max_length=40)  # 当前页面：决定默认加载哪些平台工具
    scenes: list[str] | None = Field(None, max_length=8)  # 用户在「更多」菜单里勾选的工具场景；None = 按页面默认
    research: AskResearchContext | None = None


def _scenes(payload: AskIn) -> list[str]:
    from ..agents import platform_tools as PT

    return PT.scenes_for(payload.page, payload.scenes)


def _research(payload: AskIn) -> dict[str, Any] | None:
    """Resolve the optional envelope against the authenticated request workspace."""
    from ..platform import context as reqctx
    from ..workflows import research_context as RC
    from ..workflows.service import WorkflowService

    if payload.research is None:
        return None
    try:
        return RC.resolve(
            WorkflowService(get_services()),
            payload.research.model_dump(),
            workspace_id=reqctx.current().workspace_id,
        )
    except RC.ResearchContextError as exc:
        raise HTTPException(400, str(exc)) from exc


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
        "research_context": result.research_context or None,
    }


# 多步研究意图：只在请求已带 research 上下文时才提示「升级成研究会话」；普通事实问答不打扰。
SESSION_INTENT_HINTS = (
    "完整研究",
    "帮我做",
    "帮我研究",
    "研究报告",
    "写一份",
    "出一份",
    "报告",
    "回测",
    "实验",
    "分析整个",
    "从数据到结论",
    "任务",
    "多步",
    "完整分析",
    "深入分析",
    "系统研究",
    "研究一下",
    "验证假设",
    "假设检验",
    "流程",
)


def session_suggestion(question: str, has_research: bool) -> tuple[bool, str]:
    """会话升级建议：仅当本轮带研究上下文、且问题呈现多步研究意图时返回 True。

    只给建议，不自动创建项目：前端据此展示「升级成研究会话」入口。
    """
    if not has_research:
        return False, ""
    text = (question or "").strip().lower()
    hit = next((h for h in SESSION_INTENT_HINTS if h in text), "")
    if not hit:
        return False, ""
    return True, (
        f"这个问题包含多步研究意图（命中「{hit}」），建议升级为研究会话："
        "可以把问题、来源资料与运行结果保存到一起，分步骤推进并在之后复用。"
    )


def _suggestion(payload: AskIn) -> dict[str, Any]:
    suggest, reason = session_suggestion(payload.question, payload.research is not None)
    return {"suggest_session": suggest, "reason": reason}


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
        "llm_ready": any(m.get("ready") for m in models),
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
        research_context=_research(payload),
    )
    _log(result, payload.question)
    return {**_ask_view(result), **_suggestion(payload)}


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
    research_context = _research(payload)  # verify in request thread before streaming starts

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
                research_context=research_context,
            )
            try:
                _log(result, question)
            except Exception:  # 留痕失败不影响回答
                pass
            q.put({"type": "final", **_ask_view(result), **_suggestion(payload)})
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
