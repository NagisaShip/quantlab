"""上下文管理（对应增补设计第 6 章）。

四层：会话 / 项目 / 长期记忆 / 运行。
核心机制：token 预算、超长摘要压缩、按需检索、运行快照哈希。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..llm.base import Message


class Embedder(Protocol):
    def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...


@dataclass
class ContextBudget:
    system_tokens: int = 4_000
    retrieved_tokens: int = 8_000
    history_tokens: int = 16_000
    reserved_tokens: int = 2_000

    @property
    def total(self) -> int:
        return self.system_tokens + self.retrieved_tokens + self.history_tokens + self.reserved_tokens


def _approx_tokens(text: str) -> int:
    """粗略估算：中文约 1 字 1 token，英文约 4 字符 1 token。"""
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    other = len(text) - cjk
    return cjk + other // 4 + 1


@dataclass
class MemoryItem:
    key: str
    text: str
    kind: str = "note"  # decision | conclusion | note
    ts: int = 0
    vector: list[float] | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class LongTermMemory:
    """长期记忆：可插拔后端。默认内存实现，生产可换 pgvector / Chroma。"""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._embedder = embedder

    def add(self, item: MemoryItem) -> None:
        self._items[item.key] = item

    def all(self) -> list[MemoryItem]:
        return list(self._items.values())

    def retrieve(self, query: str, k: int = 5) -> list[MemoryItem]:
        """无 embedder 时退化为关键词匹配；有则做余弦相似度。"""
        if self._embedder is None:
            return [it for it in self._items.values() if any(w in it.text for w in query.split())][:k]
        qv = self._embedder.embed([query], model="text-embedding")[0]
        scored = [(_cosine(qv, it.vector), it) for it in self._items.values() if it.vector is not None]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:k]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class ContextManager:
    """组装发给 LLM 的最终上下文，并管理压缩。"""

    def __init__(
        self,
        *,
        system_prompt: str,
        budget: ContextBudget | None = None,
        memory: LongTermMemory | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.budget = budget or ContextBudget()
        self.memory = memory or LongTermMemory()
        self.history: list[Message] = []
        self._last_snapshot_hash: str = ""

    def append(self, msg: Message) -> None:
        self.history.append(msg)

    def compress(self, summarizer) -> None:
        """用 summarizer(messages) -> str 把超长历史压成一条摘要。

        保留：最近的 N 条原始消息 + 一条摘要。决策与结论优先保留。
        """
        budget = self.budget.history_tokens
        total = sum(_approx_tokens(m.content) for m in self.history)
        if total <= budget:
            return
        keep = self.history[-4:]
        older = self.history[:-4]
        summary = summarizer(older)
        self.history = [Message(role="system", content=f"[历史摘要] {summary}"), *keep]

    def build(self, current_task: str, retrieved: list[MemoryItem] | None = None) -> list[Message]:
        retrieved = retrieved or []
        system = self.system_prompt + f"\n\n[上下文预算] 总 {self.budget.total} tokens"
        messages = [Message(role="system", content=system)]
        if retrieved:
            mem_text = "\n".join(f"- ({it.kind}) {it.text}" for it in retrieved)
            messages.append(Message(role="system", content=f"[相关长期记忆]\n{mem_text}"))
        messages.extend(self.history)
        messages.append(Message(role="user", content=current_task))
        return messages

    def snapshot_hash(self) -> str:
        """冻结上下文快照哈希，保证可复现。"""
        blob = json.dumps(
            {
                "system": self.system_prompt,
                "history": [m.to_dict() for m in self.history],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self._last_snapshot_hash = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return self._last_snapshot_hash
