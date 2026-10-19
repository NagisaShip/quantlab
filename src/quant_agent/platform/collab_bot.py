"""QuantLab Bot: lightweight conversational member for groups and the community square.

Group Bot configuration lives in ``collab_groups.settings.bot``; no duplicate member row is
created. Only group owners/admins can add or edit it. Model calls are bound to the administrator
who first added the Bot, so usage and quota are charged there.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..llm.base import LLMError, Message
from .db import jdump, jload, now_ms

log = logging.getLogger(__name__)

BOT_NAME = "QuantLab Bot"
BOT_MENTION_RE = re.compile(r"@\s*quantlab[\s_-]*bot(?![A-Za-z0-9_])", re.IGNORECASE)
BOT_AUTHOR_ID = "bot:quantlab"

BASE_PROMPT = (
    "你是 QuantLab Bot，一位在量化研究平台里工作的同事。你正在被群成员或策略广场的读者 @，"
    "请直接回应对方的问题。\n"
    "像真人同事聊天：先回答，再补充一个最有用的点；通常 1–4 句，尽量不超过 120 个汉字。\n"
    "不要写标题、项目符号、表格、免责声明或长篇报告；不要复述对方的问题；不确定就直说，并给出一个可执行的下一步。\n"
    "不要编造回测、行情或收益数据。"
)
QMOJI_PROMPT = (
    "可以在句末自然地带一个且只带一个 Qmoji，例如 :qbot-hi:、:qbot-think:、:qbot-agree:、"
    ":qbot-laugh:、:qbot-ok:、:qbot-data:。只在合适时使用，不要解释它，也不要堆叠多个。"
)
PERSONALITIES = {
    "friendly": "性格：友好、轻松，像熟悉的同事，偶尔用一点口语。",
    "professional": "性格：专业、克制，先给结论，再给一句依据。",
    "witty": "性格：幽默但不轻浮，最多一句俏皮话，不拿用户开玩笑。",
    "direct": "性格：直接、坦率，不绕弯子，但保持礼貌。",
}
DEFAULT_PERSONALITY = "friendly"


def mentions_bot(text: str) -> bool:
    return bool(BOT_MENTION_RE.search(text or ""))


def bot_member_id(gid: str) -> str:
    return f"bot:{gid}"


def bot_member(gid: str, settings: dict[str, Any] | None) -> dict[str, Any] | None:
    cfg = (settings or {}).get("bot")
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return None
    return {
        "user_id": bot_member_id(gid),
        "name": BOT_NAME,
        "role": "member",
        "joined_at": int(cfg.get("created_at") or now_ms()),
        "kind": "bot",
        "is_bot": True,
        "avatar_color": "#6D5BD0",
    }


def _clean_reply(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^\s*(?:QuantLab\s*Bot|Bot)\s*[:：]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}[-*]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return ""
    found = list(re.finditer(r":qbot-[a-z0-9-]+:", text))
    if len(found) > 1:
        for match in reversed(found[1:]):
            text = text[: match.start()] + text[match.end() :]
    if len(text) > 480:
        cut = max(text.rfind(ch, 0, 480) for ch in ("。", "！", "？", ".", "!", "?"))
        text = text[: cut + 1] if cut >= 100 else text[:480].rstrip("，,；;：: ") + "…"
    return text


class CollabBot:
    def __init__(self, db) -> None:
        self.db = db

    # ------------------------------------------------------------------ config
    def _row(self, gid: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT id, workspace_id, settings FROM collab_groups WHERE id=?", (gid,))

    def config(self, gid: str) -> dict[str, Any] | None:
        row = self._row(gid)
        if not row:
            return None
        cfg = (jload(row.get("settings"), {}) or {}).get("bot")
        return dict(cfg) if isinstance(cfg, dict) else None

    def public_config(self, gid: str) -> dict[str, Any] | None:
        cfg = self.config(gid)
        if not cfg:
            return None
        return {
            "enabled": bool(cfg.get("enabled", True)),
            "name": BOT_NAME,
            "member_id": bot_member_id(gid),
            "model_target": str(cfg.get("model_target") or ""),
            "system_prompt": str(cfg.get("system_prompt") or ""),
            "personality": str(cfg.get("personality") or DEFAULT_PERSONALITY),
            "qmoji_enabled": bool(cfg.get("qmoji_enabled", True)),
            # 回复别人时默认带上 @对方；关掉就只回内容
            "reply_default_mention": bool(cfg.get("reply_default_mention", True)),
            "created_by": str(cfg.get("created_by") or ""),
            "created_at": int(cfg.get("created_at") or 0),
            "updated_at": int(cfg.get("updated_at") or 0),
            "updated_by": str(cfg.get("updated_by") or ""),
        }

    def reply_mentions_enabled(self, workspace_id: str) -> bool:
        """策略广场没有独立的 Bot 配置：沿用工作区里任意一个群组的 Bot 开关。

        一个都没配过时按默认值（开）处理。
        """
        row = self.db.fetchone(
            "SELECT settings FROM collab_groups WHERE workspace_id=? AND settings LIKE '%\"bot\"%' "
            "ORDER BY created_at DESC LIMIT 1",
            (workspace_id,),
        )
        if not row:
            return True
        cfg = (jload(row.get("settings"), {}) or {}).get("bot")
        if not isinstance(cfg, dict):
            return True
        return bool(cfg.get("reply_default_mention", True))

    def save_config(
        self,
        gid: str,
        *,
        actor_id: str,
        enabled: bool = True,
        model_target: str = "",
        system_prompt: str = "",
        personality: str = "",
        qmoji_enabled: bool = True,
        reply_default_mention: bool = True,
    ) -> dict[str, Any]:
        row = self._row(gid)
        if not row:
            from .errors import AppError, ErrorCode

            raise AppError(ErrorCode.NOT_FOUND, "群组不存在")
        settings = jload(row.get("settings"), {}) or {}
        old = settings.get("bot") if isinstance(settings.get("bot"), dict) else {}
        now = now_ms()
        settings["bot"] = {
            "enabled": bool(enabled),
            "model_target": str(model_target or "").strip()[:200],
            "system_prompt": str(system_prompt or "").strip()[:4000],
            "personality": str(personality or DEFAULT_PERSONALITY).strip()[:200],
            "qmoji_enabled": bool(qmoji_enabled),
            "reply_default_mention": bool(reply_default_mention),
            "created_by": str(old.get("created_by") or actor_id),
            "created_at": int(old.get("created_at") or now),
            "updated_at": now,
            "updated_by": actor_id,
        }
        self.db.execute("UPDATE collab_groups SET settings=? WHERE id=?", (jdump(settings), gid))
        return self.public_config(gid) or {}

    def remove_config(self, gid: str) -> bool:
        row = self._row(gid)
        if not row:
            return False
        settings = jload(row.get("settings"), {}) or {}
        if "bot" not in settings:
            return False
        settings.pop("bot", None)
        self.db.execute("UPDATE collab_groups SET settings=? WHERE id=?", (jdump(settings), gid))
        return True

    # ------------------------------------------------------------------ model
    @staticmethod
    def _system_prompt(cfg: dict[str, Any] | None, *, qmoji_default: bool = True) -> str:
        cfg = cfg or {}
        personality = str(cfg.get("personality") or DEFAULT_PERSONALITY)
        parts = [BASE_PROMPT, PERSONALITIES.get(personality, f"性格：{personality}。")]
        custom = str(cfg.get("system_prompt") or "").strip()
        if custom:
            parts.append(f"群组管理员补充的设定：\n{custom}")
        if bool(cfg.get("qmoji_enabled", qmoji_default)):
            parts.append(QMOJI_PROMPT)
        return "\n\n".join(parts)

    def _complete(self, router: Any, system: str, user: str, *, model: str) -> tuple[str, str]:
        messages = [Message(role="system", content=system), Message(role="user", content=user[-12000:])]
        try:
            result = router.chat(
                messages,
                role="default",
                model=model or None,
                temperature=0.65,
                max_tokens=420,
                effort="low",
            )
            text = _clean_reply(result.content)
            if text:
                return text, f"{result.provider}/{result.model}"
        except LLMError as exc:
            log.info("QuantLab Bot model unavailable: %s", exc)
        except Exception:
            log.exception("QuantLab Bot reply failed")
        return "", ""

    @staticmethod
    def _fallback(cfg: dict[str, Any], *, square: bool = False) -> str:
        qmoji = " :qbot-think:" if bool(cfg.get("qmoji_enabled", True)) else ""
        if square:
            return f"我这边暂时没接上模型，先不瞎猜；等平台恢复后再 @ 我一次。{qmoji}"
        return f"模型这会儿没接上，我先记下这个问题；等管理员把模型配置恢复后我再补上。{qmoji}"

    def reply_group(
        self,
        router: Any,
        *,
        workspace_id: str,
        role: str,
        cfg: dict[str, Any],
        history: list[dict[str, Any]],
        trigger_name: str,
        trigger_body: str,
        trigger_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from . import context

        lines: list[str] = []
        for item in history[-10:]:
            body = str(item.get("body") or "").strip()
            kind = item.get("author_kind")
            if not body or kind == "system":
                continue
            who = (
                "你（QuantLab Bot）"
                if kind == "agent" and item.get("author_name") == BOT_NAME
                else str(item.get("author_name") or "成员")
            )
            lines.append(f"{who}：{body[:600]}")
        refs = "、".join(str(r.get("title") or r.get("type") or "引用") for r in (trigger_refs or [])[:3])
        ask = f"{trigger_name}：{trigger_body[:1800]}"
        if refs:
            ask += f"\n（同时引用了：{refs}）"
        user = (
            ("最近群聊（只作上下文，不要逐条复述）：\n" + "\n".join(lines) + "\n\n" if lines else "")
            + "请回复这条 @ 你的消息：\n"
            + ask
        )
        billing_user = str(cfg.get("created_by") or "")
        if not billing_user:
            return {"body": self._fallback(cfg), "mode": "fallback", "model": ""}
        with context.bind(workspace_id, user_id=billing_user, role=role or "member"):
            text, model = self._complete(
                router,
                self._system_prompt(cfg),
                user,
                model=str(cfg.get("model_target") or ""),
            )
        return {
            "body": text or self._fallback(cfg),
            "mode": "llm" if text else "fallback",
            "model": model,
        }

    def reply_square(
        self,
        router: Any,
        *,
        workspace_id: str,
        trigger_user_id: str,
        role: str,
        post: dict[str, Any],
        trigger_body: str,
        parent_author: str = "",
        recent: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from . import context

        cfg = {"qmoji_enabled": True, "personality": DEFAULT_PERSONALITY}
        lines: list[str] = []
        for item in (recent or [])[-8:]:
            body = str(item.get("body") or "").strip()
            author = str(item.get("author") or "读者")
            if body and author != BOT_NAME:
                lines.append(f"{author}：{body[:500]}")
        excerpt = str(post.get("body") or "").strip()[:1800]
        user = (
            f"帖子标题：{post.get('title') or '（无标题）'}\n"
            + (f"帖子摘要：\n{excerpt}\n\n" if excerpt else "")
            + ("最近的讨论：\n" + "\n".join(lines) + "\n\n" if lines else "")
            + (f"你正在回复 {parent_author} 的评论。\n" if parent_author else "")
            + f"请回复这条 @ 你的评论：\n{trigger_body[:1800]}"
        )
        with context.bind(workspace_id, user_id=trigger_user_id, role=role or "member"):
            text, model = self._complete(router, self._system_prompt(cfg), user, model="")
        return {
            "body": text or self._fallback(cfg, square=True),
            "mode": "llm" if text else "fallback",
            "model": model,
        }
