"""Agent 编排层（对应增补设计第 3 章）。

核心是一个极简的 ReAct 风格循环：LLM 产出文本或工具调用，
执行工具、把结果回灌，直到给出最终答案或达到步数上限。
所有运行记录 trace，供 StrategyVersion 关联与可复现性验证。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..context.manager import ContextManager
from ..llm.base import Message, Tool, ToolCall
from ..llm.router import ModelRouter


@dataclass
class AgentStep:
    index: int
    kind: str  # "llm" | "tool"
    content: str = ""
    tool_name: str = ""
    tool_args: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    model: str = ""


@dataclass
class AgentRun:
    run_id: str
    role: str
    steps: list[AgentStep] = field(default_factory=list)
    final: str = ""
    status: str = "running"  # running | done | blocked | failed
    context_hash: str = ""
    model: str = ""  # 实际使用的 provider/model
    usage: dict[str, int] = field(default_factory=dict)
    started_at: int = field(default_factory=lambda: int(time.time() * 1000))
    ended_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "role": self.role,
            "status": self.status,
            "context_hash": self.context_hash,
            "model": self.model,
            "usage": self.usage,
            "final": self.final,
            "steps": [s.__dict__ for s in self.steps],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class Agent:
    """通用 Agent：一个角色 + 一套工具 + 一个上下文管理器。"""

    def __init__(
        self,
        *,
        name: str,
        role: str,
        router: ModelRouter,
        context: ContextManager,
        tools: list[Tool] | None = None,
        max_steps: int = 12,
        temperature: float = 0.2,
        model: str | None = None,
        effort: str | None = None,
    ) -> None:
        self.name = name
        self.role = role  # 用于模型路由，如 "code_gen" | "reasoning"
        self.router = router
        self.context = context
        self.tools = {t.name: t for t in (tools or [])}
        self.max_steps = max_steps
        self.temperature = temperature
        self.model = model  # "provider/model"，覆盖路由（角色级配置）
        self.effort = effort or None  # 思考努力程度（角色级配置）

    def run(self, task: str, *, retrieved: list | None = None) -> AgentRun:
        run = AgentRun(run_id=str(uuid.uuid4()), role=self.role)
        tool_specs = [t for t in self.tools.values()]
        messages = self.context.build(task, retrieved=retrieved)
        run.context_hash = self.context.snapshot_hash()

        for i in range(self.max_steps):
            try:
                kw: dict = {
                    "role": self.role,
                    "model": self.model,
                    "temperature": self.temperature,
                    "tools": tool_specs or None,
                }
                if self.effort and _accepts(self.router.chat, "effort"):
                    kw["effort"] = self.effort
                result = self.router.chat(messages, **kw)
            except Exception as e:  # LLM 全失败
                run.status = "blocked"
                run.final = f"LLM 调用失败: {e}"
                run.ended_at = int(time.time() * 1000)
                return run

            run.steps.append(
                AgentStep(index=i, kind="llm", content=result.content, model=f"{result.provider}/{result.model}")
            )
            run.model = f"{result.provider}/{result.model}"
            run.usage = result.usage or run.usage
            messages.append(
                Message(
                    role="assistant",
                    content=result.content,
                    tool_calls=result.tool_calls,
                    provider_blocks=list(getattr(result, "provider_blocks", []) or []),
                    reasoning=getattr(result, "thinking", "") or "",
                )
            )

            if not result.tool_calls:
                run.final = result.content
                run.status = "done"
                run.ended_at = int(time.time() * 1000)
                return run

            document_images = []
            for call in result.tool_calls:
                output = self._execute_tool(call)
                from ..local.documents import preview_images

                try:
                    document_images.extend(preview_images(call.name, json.loads(output)))
                except (ValueError, KeyError, OSError):
                    pass
                run.steps.append(
                    AgentStep(
                        index=i,
                        kind="tool",
                        tool_name=call.name,
                        tool_args=call.arguments,
                        tool_result=output,
                    )
                )
                messages.append(Message(role="tool", content=output, tool_call_id=call.id, name=call.name))
            if document_images:
                messages.append(
                    Message(role="user", content="检查文档页面的排版，修正问题后重新预览。", images=document_images[:8])
                )

        run.status = "done"  # 达到步数上限：最后一步多半是工具调用、没有文字，再要一次不带工具的最终答复
        run.final = run.steps[-1].content if run.steps else ""
        if not run.final.strip() and run.steps:
            messages.append(
                Message(role="user", content="已达到工具调用步数上限。请不要再调用工具，直接根据以上信息给出最终答复。")
            )
            try:
                result = self.router.chat(messages, role=self.role, model=self.model, temperature=self.temperature)
                run.final = result.content or ""
                run.model = f"{result.provider}/{result.model}"
                run.usage = result.usage or run.usage
                run.steps.append(AgentStep(index=self.max_steps, kind="llm", content=run.final, model=run.model))
            except Exception as e:
                run.final = ""
                run.steps.append(AgentStep(index=self.max_steps, kind="llm", content=f"最终答复失败: {e}"))
        run.ended_at = int(time.time() * 1000)
        return run

    def _execute_tool(self, call: ToolCall) -> str:
        tool = self.tools.get(call.name)
        if tool is None:
            return json.dumps({"error": f"未知工具 {call.name}"}, ensure_ascii=False)
        if tool.handler is None:
            return json.dumps({"error": f"工具 {call.name} 未绑定 handler"}, ensure_ascii=False)
        try:
            result = tool.handler(**call.arguments)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)


def tool(name: str, description: str, parameters: dict, handler: Callable | None = None) -> Tool:
    """便捷构造工具。"""
    return Tool(name=name, description=description, parameters=parameters, handler=handler)


def _accepts(fn, name: str) -> bool:
    """路由是否接受某个关键字参数（测试替身、旧插件可能不认识 effort）。"""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
