"""工作区模型中心：服务商、成员模型权限与限额、用量计量与价格换算。

## 服务商从哪来

平台不再内置任何服务商。模型只有两个来源：

- **工作区共享**（scope=workspace）：工作区管理员登记，密钥属于管理员；成员在被允许
  「使用工作区模型」时，调用请求走管理员的 API。
- **个人**（scope=personal）：成员自己登记，只有本人可见、可用，费用记在本人名下。

同名时个人服务商优先（对本人而言）。配置文件里的 provider 只给命令行用，Web 平台不读。

## 密钥

和交易所凭证一样用 SecretBox 加密后存进工作区 store 的服务商记录里，接口只回显脱敏值。

## 用量与费用

每次成功调用记一条 `llm_usage` 明细：输入 / 输出 token，以及按服务商上填写的价格折算的
费用（微元整数，1 元 = 1,000,000）。价格按「每百万 token」填写，币种与汇率也由管理员填写，
统一折算成人民币；记录时按当时价格折算，事后改价不回溯。

## 限额

限额规则 `{"metric": "tokens" | "cost", "window": 时间段, "limit": 数值}`。时间段有两类：

- 滚动：`1h` / `5h` / `1d` / `7d` / `30d`（从现在往回数）；
- 自然周期：`day`（每日）/ `week`（每周，周一起）/ `month`（每月）/ `total`（累计，永不重置），
  按工作区时区切分。

可以设的地方：

- 服务商上：限制整个工作区（个人服务商则是本人、群组服务商则是全群）在该服务商上的用量；
- 成员上（工作区管理员设置）：限制该成员使用**工作区共享**模型的用量；
- 群组上：工作区管理员给群组分配共享模型额度（全体群成员合计，`ws_limits`）；
  群主给每位群成员设定使用**群组模型**的额度（`member_limits`）。

## 群组模型（scope=group）

群主 / 群管理员可以登记只给本群成员使用的服务商（密钥属于登记人），用量按群组单独记账。

调用前检查，超额的服务商当作不可用跳过（降级链里的下一个候选接着试），错误信息说明原因。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..platform import context
from ..platform.backtest_retention import DEFAULT_MEMBER_RECORD_UNITS
from ..platform.db import Database, now_ms
from ..platform.errors import AppError, ErrorCode
from ..platform.quota import day_start_ms, today
from ..platform.secretbox import SecretError
from ..platform.storage_usage import upload_storage
from ..store import Collections
from .base import LLMError, Message
from .router import AgentConfig, ProviderConfig

PROVIDERS = "llm_providers"
POLICIES = "llm_member_policies"
REQUESTS = "llm_quota_requests"
ROUTING_KEYS = ("default", "code_gen", "reasoning", "report")

# 滚动时间段的秒数；自然周期（day / week / month / total）的值只用于排序，起点见 window_start()
WINDOWS: dict[str, int] = {
    "1h": 3600,
    "5h": 5 * 3600,
    "1d": 86400,
    "day": 86400 + 1,
    "7d": 7 * 86400,
    "week": 7 * 86400 + 1,
    "30d": 30 * 86400,
    "month": 30 * 86400 + 1,
    "total": 10**10,
}
CALENDAR_WINDOWS = ("day", "week", "month", "total")
WINDOW_LABEL = {
    "1h": "1 小时内",
    "5h": "5 小时内",
    "1d": "1 天内",
    "7d": "7 天内",
    "30d": "30 天内",
    "day": "每日",
    "week": "每周",
    "month": "每月",
    "total": "累计",
}
METRIC_LABEL = {
    "tokens": "token",
    "cost": "费用（元）",
    "cpu_seconds": "CPU 秒",
    "backtests": "回测次数",
    "optimize_combinations": "优化组合数",
    "storage_bytes": "上传存储",
    "record_units": "研究记录（单位）",
}
TASK_METRICS = {"cpu_seconds", "backtests", "optimize_combinations"}
MICRO = Decimal(1_000_000)
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{0,30}")


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


def dec(value: Any, default: str = "0") -> Decimal:
    try:
        d = Decimal(str(value).strip() or default)
    except (InvalidOperation, ValueError):
        d = Decimal(default)
    return d if d.is_finite() and d >= 0 else Decimal(default)


def plain(d: Decimal) -> str:
    """十进制的普通写法：100000 而不是 1E+5。"""
    return format(d.normalize(), "f")


def fmt_bytes(value: int | Decimal) -> str:
    """按 1024 进制显示字节数。"""
    n = max(0.0, float(value))
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024
        idx += 1
    return f"{n:.0f} {units[idx]}" if idx == 0 else f"{n:.2f} {units[idx]}"


def clean_limits(rules: Any) -> list[dict[str, Any]]:
    """限额规则去重（同一 metric + window 只留最后一条）并校验。"""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        metric, window = str(r.get("metric", "")), str(r.get("window", ""))
        if metric not in METRIC_LABEL or window not in WINDOWS:
            continue
        # 存储是存量指标，只比较当前占用；把所有存储规则统一到累计口径，
        # 避免同一条配额被 day / week 等窗口重复解释。
        if metric in ("storage_bytes", "record_units"):
            window = "total"
        limit = dec(r.get("limit"))
        if limit <= 0:
            continue
        out[(metric, window)] = {"metric": metric, "window": window, "limit": plain(limit)}
    return sorted(out.values(), key=lambda x: (x["metric"], WINDOWS[x["window"]]))


def window_start(window: str, tz: str = "Asia/Shanghai", now: float | None = None) -> int:
    """限额时间段的起点（毫秒）。滚动段从现在往回数；自然周期按工作区时区切到今天 / 本周一 / 本月 1 日。"""
    import datetime as dt
    from zoneinfo import ZoneInfo

    now = time.time() if now is None else now
    if window == "total":
        return 0
    if window not in CALENDAR_WINDOWS:
        return int((now - WINDOWS[window]) * 1000)
    try:
        zone = ZoneInfo(tz or "Asia/Shanghai")
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    local = dt.datetime.fromtimestamp(now, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "week":
        local -= dt.timedelta(days=local.weekday())
    elif window == "month":
        local = local.replace(day=1)
    return int(local.timestamp() * 1000)


def _int(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def usage_breakdown(usage: dict[str, Any] | None) -> dict[str, int]:
    """各家 usage 归一成 {input, cache_read, cache_write, output}。input 是全部输入（含缓存部分）。

    - OpenAI Chat Completions：prompt_tokens（含缓存）+ prompt_tokens_details.cached_tokens
    - DeepSeek：prompt_tokens + prompt_cache_hit_tokens
    - OpenAI Responses：input_tokens（含缓存）+ input_tokens_details.cached_tokens
    - Anthropic：input_tokens 不含缓存；cache_read_input_tokens / cache_creation_input_tokens 另计
    """
    u = usage or {}
    out = _int(u.get("output_tokens", u.get("completion_tokens")))
    if "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u:
        read, write = _int(u.get("cache_read_input_tokens")), _int(u.get("cache_creation_input_tokens"))
        return {
            "input": _int(u.get("input_tokens")) + read + write,
            "cache_read": read,
            "cache_write": write,
            "output": out,
        }
    total = _int(u.get("input_tokens", u.get("prompt_tokens")))
    details = u.get("input_tokens_details") or u.get("prompt_tokens_details") or {}
    read = _int(details.get("cached_tokens") if isinstance(details, dict) else 0) or _int(
        u.get("prompt_cache_hit_tokens")
    )
    return {"input": total, "cache_read": min(read, total), "cache_write": 0, "output": out}


def usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int]:
    """(全部输入, 输出) token，用于每日配额计量。"""
    b = usage_breakdown(usage)
    return b["input"], b["output"]


def fmt_cost(micro: int | Decimal) -> str:
    return str((Decimal(int(micro)) / MICRO).quantize(Decimal("0.0001")))


class ModelHub:
    def __init__(
        self,
        *,
        store: Any,
        secrets: Any,
        db: Database,
        auth: Any = None,
        audit: Any = None,
        quota: Any = None,
    ) -> None:
        self.store = store  # WorkspaceStore
        self.secrets = secrets
        self.db = db
        self.auth = auth
        self.audit = audit
        self.quota = quota
        self.max_inflight = int(os.environ.get("QAGENT_MODEL_MAX_IN_FLIGHT", "8"))
        if self.max_inflight < 1:
            raise ValueError("QAGENT_MODEL_MAX_IN_FLIGHT must be positive")
        # 服务商侧额度查询缓存：{缓存键: (抓取时间, 结果)}。避免每次打开额度页都打外部接口。
        self._remote_quota_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ 存取
    def _ws(self, workspace_id: str | None = None):
        return self.store.for_workspace(workspace_id or context.current_workspace())

    @staticmethod
    def _id(scope: str, owner_id: str, name: str, group_id: str = "") -> str:
        if scope == "group":
            return f"grp:{group_id}:{name}"
        return f"ws:{name}" if scope == "workspace" else f"me:{owner_id}:{name}"

    def _tz(self, ws: str) -> str:
        if self.auth is None:
            return "Asia/Shanghai"
        try:
            return self.auth.workspace(ws)["settings"].get("timezone") or "Asia/Shanghai"
        except Exception:
            return "Asia/Shanghai"

    def my_groups(self, ws: str, user_id: str | None) -> dict[str, str]:
        """当前成员所在的群组：{group_id: 群角色}。"""
        if not user_id:
            return {}
        rows = self.db.fetchall(
            "SELECT g.id, m.role FROM collab_groups g JOIN collab_group_members m ON m.group_id=g.id "
            "WHERE g.workspace_id=? AND m.user_id=? AND g.archived_at IS NULL",
            (ws, user_id),
        )
        return {r["id"]: r["role"] for r in rows}

    def group_settings(self, group_id: str) -> dict[str, Any]:
        from ..platform.db import jload

        row = self.db.fetchone("SELECT settings FROM collab_groups WHERE id=?", (group_id,))
        return (jload(row["settings"], {}) or {}) if row else {}

    def group_members(self, group_id: str) -> list[str]:
        return [
            r["user_id"]
            for r in self.db.fetchall("SELECT user_id FROM collab_group_members WHERE group_id=?", (group_id,))
        ]

    def can_manage_group(self, ws: str, group_id: str) -> bool:
        ctx = context.current()
        return ctx.role == "admin" or self.my_groups(ws, ctx.user_id).get(group_id) in ("owner", "admin")

    def _records(self, ws: str) -> list[dict[str, Any]]:
        return self._ws(ws).list(PROVIDERS)

    def api_key(self, rec: dict[str, Any]) -> str:
        token = rec.get("key_enc") or ""
        if not token:
            return ""
        try:
            return self.secrets.decrypt(token)
        except SecretError:
            return ""

    # ------------------------------------------------------------------ 成员策略
    def policy(self, ws: str, user_id: str, role: str | None = None) -> dict[str, Any]:
        rec = self._ws(ws).get(POLICIES, user_id) or {}
        default_use = role != "reviewer"
        # 管理员不受成员默认 200 单位限制；只有显式给管理员设置 limits 才继承。
        defaults = [] if role == "admin" else self.default_limits(ws)
        return {
            "user_id": user_id,
            "use_workspace_models": bool(rec.get("use_workspace_models", default_use)),
            "allow_personal_models": bool(rec.get("allow_personal_models", True)),
            # 没单独设过限额的成员用工作区的「新成员默认限额」
            "limits": clean_limits(rec.get("limits") if "limits" in rec else defaults),
            "custom_limits": "limits" in rec,
        }

    def default_limits(self, ws: str) -> list[dict[str, Any]]:
        raw = self._ws(ws).get(Collections.SETTINGS, "llm_default_member_limits") or {}
        # 新工作区未显式配置时，普通成员默认 200 个研究记录单位。
        # 显式保存空列表表示管理员主动选择“不限额”，不能再用默认值覆盖。
        if "limits" not in raw:
            return [{"metric": "record_units", "window": "total", "limit": str(DEFAULT_MEMBER_RECORD_UNITS)}]
        return clean_limits(raw.get("limits"))

    def set_default_limits(self, ws: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._ws(ws).upsert(Collections.SETTINGS, {"id": "llm_default_member_limits", "limits": clean_limits(rules)})
        return self.default_limits(ws)

    def set_policy(self, ws: str, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self._ws(ws).get(POLICIES, user_id) or {"id": user_id}
        for k in ("use_workspace_models", "allow_personal_models"):
            if k in patch and patch[k] is not None:
                cur[k] = bool(patch[k])
        if "limits" in patch and patch["limits"] is not None:
            cur["limits"] = clean_limits(patch["limits"])
        self._ws(ws).upsert(POLICIES, cur)
        return self.policy(ws, user_id, self._role(ws, user_id))

    def _role(self, ws: str, user_id: str) -> str | None:
        if self.auth is None:
            return None
        row = self.db.fetchone(
            "SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND status='active'", (ws, user_id)
        )
        return row["role"] if row else None

    # ------------------------------------------------------------------ 服务商
    def visible(self, ws: str, user_id: str | None, role: str | None) -> list[dict[str, Any]]:
        """当前成员能看到的服务商记录：工作区共享（管理员总能看到）+ 本人的个人服务商。"""
        pol = self.policy(ws, user_id or "", role) if user_id else {"use_workspace_models": True}
        groups = self.my_groups(ws, user_id)
        out = []
        for rec in self._records(ws):
            if rec.get("scope") == "personal":
                if user_id and rec.get("owner_id") == user_id:
                    out.append(rec)
            elif rec.get("scope") == "group":
                if rec.get("group_id") in groups:
                    out.append(rec)
            elif role == "admin" or pol["use_workspace_models"]:
                out.append(rec)
        order = {"workspace": 0, "group": 1, "personal": 2}
        out.sort(key=lambda r: (order.get(r.get("scope", "workspace"), 0), r.get("created_at", 0)))
        return out

    def find(self, ws: str, user_id: str | None, role: str | None, name: str) -> dict[str, Any] | None:
        recs = [r for r in self.visible(ws, user_id, role) if r.get("name") == name]
        recs.sort(key=lambda r: {"personal": 0, "group": 1}.get(r.get("scope", ""), 2))  # 个人 > 群组 > 工作区
        return recs[0] if recs else None

    def upsert(self, payload: dict[str, Any], *, scope: str) -> dict[str, Any]:
        ctx = context.current()
        ws = context.current_workspace()
        user_id = ctx.user_id or ""
        group_id = str(payload.get("group_id") or "") if scope == "group" else ""
        if scope == "workspace" and ctx.role != "admin":
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以登记工作区共享的模型服务商")
        if scope == "group" and (not group_id or not self.can_manage_group(ws, group_id)):
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有群主或群管理员可以登记群组模型服务商")
        if scope == "personal":
            if ctx.role == "reviewer":
                raise AppError(ErrorCode.PERMISSION_DENIED, "审阅者（只读）不能登记模型服务商")
            if not self.policy(ws, user_id, ctx.role)["allow_personal_models"]:
                raise AppError(ErrorCode.PERMISSION_DENIED, "管理员没有允许你添加个人模型服务商")
        name = str(payload.get("name", "")).strip().lower()
        if not NAME_RE.fullmatch(name):
            raise AppError(ErrorCode.INVALID_PARAMETER, "服务商 ID 只能包含小写字母、数字、-、_，且以字母或数字开头")
        base_url = str(payload.get("base_url", "")).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise AppError(ErrorCode.INVALID_PARAMETER, "API 地址需要以 http:// 或 https:// 开头")
        models = list(dict.fromkeys(m.strip() for m in payload.get("models") or [] if str(m).strip()))
        if not models:
            raise AppError(ErrorCode.INVALID_PARAMETER, "至少填写一个模型")
        rid = self._id(scope, user_id, name, group_id)
        # 成员看到的服务商按名字合并：工作区与群组之间、群组与群组之间不能重名；个人不能与工作区重名
        clash = []
        for r in self._records(ws):
            rs = r.get("scope", "workspace")
            if r.get("name") != name or r.get("id") == rid:
                continue
            if scope == "personal" and rs in ("personal", "group"):
                continue
            if rs == "personal" and (scope == "group" or r.get("owner_id") != user_id):
                continue
            clash.append(rs)
        if clash:
            where = {"workspace": "工作区共享服务商", "group": "群组服务商", "personal": "你的个人服务商"}[clash[0]]
            raise AppError(ErrorCode.INVALID_PARAMETER, f"已有同名的{where}「{name}」，换一个 ID")
        cur = self._ws(ws).get(PROVIDERS, rid) or {}
        settings = {}
        for m in models:
            st = (payload.get("model_settings") or {}).get(m)
            if not isinstance(st, dict):
                continue
            inputs = [
                i
                for i in dict.fromkeys(st.get("inputs") or ["text"])
                if i in ("text", "image", "audio", "video", "file")
            ]
            clean = {
                "label": str(st.get("label") or "").strip()[:60],
                "request_model": ""
                if str(st.get("request_model") or "").strip() == m
                else str(st.get("request_model") or "").strip()[:200],
                "context_window": int(st["context_window"]) if str(st.get("context_window") or "").isdigit() else None,
                "effort": st.get("effort") if st.get("effort") in ("low", "medium", "high", "max") else "",
                "inputs": inputs if inputs and inputs != ["text"] else None,
                "price_in": plain(dec(st.get("price_in"))) if dec(st.get("price_in")) > 0 else "",
                "price_out": plain(dec(st.get("price_out"))) if dec(st.get("price_out")) > 0 else "",
                "price_cache": plain(dec(st.get("price_cache"))) if dec(st.get("price_cache")) > 0 else "",
                "price_cache_write": plain(dec(st.get("price_cache_write")))
                if dec(st.get("price_cache_write")) > 0
                else "",
            }
            clean = {k: v for k, v in clean.items() if v}
            if clean:
                settings[m] = clean
        currency = (
            payload.get("currency") if payload.get("currency") in ("CNY", "USD") else (cur.get("currency") or "CNY")
        )
        fx = dec(payload.get("fx_rate"), "0")
        rec = {
            "id": rid,
            "name": name,
            "scope": scope,
            "owner_id": user_id if scope == "personal" else "",
            "group_id": group_id,
            "created_by": cur.get("created_by") or user_id,
            "type": payload.get("type")
            if payload.get("type") in ("anthropic", "openai_responses")
            else "openai_compatible",
            "base_url": base_url,
            "models": models,
            "model_settings": settings,
            "label": str(payload.get("label") or "").strip()[:40],
            "currency": currency,
            "fx_rate": plain(fx) if fx > 0 else ("1" if currency == "CNY" else "7.2"),
            "limits": clean_limits(payload.get("limits")),
            "key_enc": cur.get("key_enc", ""),
            "key_masked": cur.get("key_masked", ""),
        }
        new_key = str(payload.get("api_key") or "").strip()
        if new_key:
            rec["key_enc"] = self.secrets.encrypt(new_key)
            rec["key_masked"] = mask(new_key)
        if cur.get("created_at"):
            rec["created_at"] = cur["created_at"]
        saved = self._ws(ws).upsert(PROVIDERS, rec)
        if self.audit is not None:
            self.audit.record(
                "config.provider_upsert",
                object_type="llm_provider",
                object_id=rid,
                details={"scope": scope, "base_url": base_url, "models": models, "key_changed": bool(new_key)},
            )
        return saved

    def delete(self, name: str, *, scope: str, group_id: str = "") -> bool:
        ctx = context.current()
        ws = context.current_workspace()
        if scope == "workspace" and ctx.role != "admin":
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以删除工作区共享的模型服务商")
        if scope == "group" and not (group_id and self.can_manage_group(ws, group_id)):
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有群主或群管理员可以删除群组模型服务商")
        rid = self._id(scope, ctx.user_id or "", name, group_id)
        ok = self._ws(ws).delete(PROVIDERS, rid)
        if ok and self.audit is not None:
            self.audit.record("config.provider_delete", object_type="llm_provider", object_id=rid)
        return ok

    def view(self, rec: dict[str, Any]) -> dict[str, Any]:
        """对外视图：去掉密文，只给脱敏值。"""
        has_key = bool(rec.get("key_enc")) or self._keyless(rec)
        return {
            "name": rec["name"],
            "label": rec.get("label", ""),
            "scope": rec.get("scope", "workspace"),
            "owner_id": rec.get("owner_id", ""),
            "group_id": rec.get("group_id", ""),
            "type": rec.get("type", "openai_compatible"),
            "base_url": rec.get("base_url", ""),
            "models": rec.get("models", []),
            "model_settings": rec.get("model_settings", {}),
            "currency": rec.get("currency", "CNY"),
            "fx_rate": rec.get("fx_rate", "1"),
            "limits": rec.get("limits", []),
            "has_key": has_key,
            "key_masked": rec.get("key_masked", ""),
            "key_source": "已加密保存" if rec.get("key_enc") else ("本地服务无需密钥" if has_key else "未配置密钥"),
        }

    @staticmethod
    def _keyless(rec: dict[str, Any]) -> bool:
        host = re.sub(r"^https?://", "", rec.get("base_url", "")).split("/")[0].split(":")[0]
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    # ------------------------------------------------------------------ 路由
    def routing(self, ws: str, user_id: str | None) -> dict[str, Any]:
        store = self._ws(ws)
        base = store.get(Collections.SETTINGS, "llm_routing") or {}
        mine = (store.get(Collections.SETTINGS, f"llm_routing:{user_id}") or {}) if user_id else {}
        out: dict[str, Any] = {}
        for k in ROUTING_KEYS:
            out[k] = mine.get(k) or base.get(k) or ""
        out["fallback"] = (
            mine.get("fallback")
            if isinstance(mine.get("fallback"), list) and mine.get("fallback")
            else (base.get("fallback") if isinstance(base.get("fallback"), list) else [])
        )
        # 自动重试次数：个人设置优先，其次工作区，都没设由路由器用默认值
        for layer in (mine, base):
            if isinstance(layer.get("retries"), int):
                out["retries"] = layer["retries"]
                break
        return out

    def routing_layers(self, ws: str, user_id: str | None) -> dict[str, dict[str, Any]]:
        store = self._ws(ws)
        strip = lambda d: {k: v for k, v in (d or {}).items() if k in (*ROUTING_KEYS, "fallback", "retries")}  # noqa: E731
        return {
            "workspace": strip(store.get(Collections.SETTINGS, "llm_routing")),
            "personal": strip(store.get(Collections.SETTINGS, f"llm_routing:{user_id}")) if user_id else {},
        }

    def set_routing(self, payload: dict[str, Any], *, scope: str) -> None:
        ctx = context.current()
        ws = context.current_workspace()
        if scope == "workspace" and ctx.role != "admin":
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以修改工作区默认路由")
        rid = "llm_routing" if scope == "workspace" else f"llm_routing:{ctx.user_id}"
        rec = {
            "id": rid,
            **{k: str(payload.get(k) or "") for k in ROUTING_KEYS},
            "fallback": list(payload.get("fallback") or []),
        }
        if isinstance(payload.get("retries"), int):
            rec["retries"] = max(0, min(5, payload["retries"]))
        self._ws(ws).upsert(Collections.SETTINGS, rec)

    # ------------------------------------------------------------------ 给路由器的配置
    def scoped_config(self, base: AgentConfig) -> AgentConfig:
        ctx = context.current()
        ws = ctx.workspace_id
        if not ws:
            return AgentConfig(
                providers={},
                routing={},
                limits=base.limits,
                trace=base.trace,
                notify=base.notify,
                exchange=base.exchange,
            )
        user_id = ctx.user_id if ctx.user_id and ctx.user_id != "system" else None
        providers: dict[str, ProviderConfig] = {}
        # 先放工作区共享，再放群组，最后放个人：同名时个人覆盖
        order = {"workspace": 0, "group": 1, "personal": 2}
        for rec in sorted(self.visible(ws, user_id, ctx.role), key=lambda r: order.get(r.get("scope", ""), 0)):
            key = self.api_key(rec) or ("not-needed" if self._keyless(rec) else "")
            providers[rec["name"]] = ProviderConfig(
                name=rec["name"],
                type=rec.get("type", "openai_compatible"),
                base_url=rec.get("base_url", ""),
                api_key=key,
                models=list(rec.get("models") or []),
                model_settings=dict(rec.get("model_settings") or {}),
                scope=rec.get("scope", "workspace"),
                owner_id=rec.get("owner_id", ""),
                group_id=rec.get("group_id", ""),
                currency=rec.get("currency", "CNY"),
                fx_rate=rec.get("fx_rate", "1"),
                limits=list(rec.get("limits") or []),
            )
        return AgentConfig(
            providers=providers,
            routing=self.routing(ws, user_id),
            limits=base.limits,
            trace=base.trace,
            notify=base.notify,
            exchange=base.exchange,
        )

    # ------------------------------------------------------------------ 计量
    @staticmethod
    def cost_micro(
        pc: ProviderConfig, model: str, inp: int, out: int, cache_read: int = 0, cache_write: int = 0
    ) -> int:
        """inp 是全部输入（含缓存）。缓存命中 / 写入按各自单价，没填就按普通输入价。"""
        return ModelHub._priced_cost(ModelHub._price_snapshot(pc, model), inp, out, cache_read, cache_write)

    @staticmethod
    def _price_snapshot(pc: ProviderConfig, model: str) -> dict[str, str]:
        st = pc.setting(model)
        rate = dec(pc.fx_rate, "1") if pc.currency != "CNY" else Decimal(1)
        p_in = dec(st.get("price_in"))
        p_read = dec(st.get("price_cache")) if dec(st.get("price_cache")) > 0 else p_in
        p_write = dec(st.get("price_cache_write")) if dec(st.get("price_cache_write")) > 0 else p_in
        return {
            "input": str(p_in * rate),
            "cache_read": str(p_read * rate),
            "cache_write": str(p_write * rate),
            "output": str(dec(st.get("price_out")) * rate),
        }

    @staticmethod
    def _priced_cost(prices: dict[str, str], inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> int:
        plain_in = max(0, inp - cache_read - cache_write)
        # 价格是「每百万 token」：tokens × 价格 / 1e6 元 = tokens × 价格 微元
        micro = (
            Decimal(plain_in) * Decimal(prices["input"])
            + Decimal(cache_read) * Decimal(prices["cache_read"])
            + Decimal(cache_write) * Decimal(prices["cache_write"])
            + Decimal(out) * Decimal(prices["output"])
        )
        return int(micro.to_integral_value())

    def record(self, pc: ProviderConfig, model: str, usage: dict[str, Any] | None) -> None:
        ctx = context.current()
        ws = ctx.workspace_id
        if not ws:
            return
        b = usage_breakdown(usage)
        cost = self.cost_micro(pc, model, b["input"], b["output"], b["cache_read"], b["cache_write"])
        self.db.execute(
            "INSERT INTO llm_usage (workspace_id, user_id, scope, provider, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, cost_micro, created_at, group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ws,
                ctx.user_id or "system",
                pc.scope,
                pc.name,
                model,
                b["input"],
                b["output"],
                b["cache_read"],
                b["cache_write"],
                cost,
                now_ms(),
                pc.group_id or None,
            ),
        )

    def _admission_constraints(
        self, pc: ProviderConfig, ws: str, user_id: str, role: str | None
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Model quota scopes applied to one request; all are checked in one DB transaction."""
        scopes: list[tuple[list[dict[str, Any]], dict[str, Any]]] = []
        provider_where: dict[str, Any] = {"provider": pc.name, "scope": pc.scope}
        if pc.scope == "personal":
            provider_where["user_id"] = pc.owner_id
        if pc.scope == "group":
            provider_where["group_id"] = pc.group_id
        scopes.append((pc.limits, provider_where))
        human = bool(user_id and user_id != "system")
        if pc.scope == "group" and human and pc.group_id:
            scopes.append(
                (
                    self.group_settings(pc.group_id).get("member_limits") or [],
                    {"scope": "group", "group_id": pc.group_id, "user_id": user_id},
                )
            )
        if pc.scope == "workspace" and human:
            scopes.append((self.policy(ws, user_id, role)["limits"], {"scope": "workspace", "user_id": user_id}))
            for group_id in self.my_groups(ws, user_id):
                scopes.append(
                    (
                        self.group_settings(group_id).get("ws_limits") or [],
                        {"scope": "workspace", "user_ids": self.group_members(group_id)},
                    )
                )
        return [
            (rule, where)
            for rules, where in scopes
            for rule in clean_limits(rules)
            if rule["metric"] in {"tokens", "cost"}
        ]

    @staticmethod
    def _admission_sum(tx: Any, table: str, ws: str, since_ms: int, metric: str, where: dict[str, Any]) -> int:
        column = (
            ("input_tokens + output_tokens" if metric == "tokens" else "cost_micro")
            if table == "llm_usage"
            else ("reserved_tokens" if metric == "tokens" else "reserved_micro")
        )
        sql = f"SELECT COALESCE(SUM({column}),0) FROM {table} WHERE workspace_id=? AND created_at>=?"
        args: list[Any] = [ws, since_ms]
        if table == "llm_admissions":
            sql += " AND status IN ('RESERVED','IN_FLIGHT','UNSETTLED')"
        for key in ("provider", "scope", "user_id", "group_id"):
            if where.get(key):
                sql += f" AND {key}=?"
                args.append(where[key])
        if "user_ids" in where:
            members = where["user_ids"]
            if not members:
                return 0
            sql += f" AND user_id IN ({','.join('?' * len(members))})"
            args.extend(members)
        return int(tx.scalar(sql, tuple(args)) or 0)

    def admit(
        self,
        pc: ProviderConfig,
        model: str,
        *,
        request_id: str,
        reserved_tokens: int,
        reserved_micro: int,
        run_id: str | None = None,
        step_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically reserve every applicable provider/member/group limit."""
        ctx = context.current()
        ws, user_id = ctx.workspace_id, ctx.user_id or "system"
        if not ws or not request_id or reserved_tokens < 0 or reserved_micro < 0:
            raise ValueError("workspace, request_id and nonnegative estimates are required")
        constraints = self._admission_constraints(pc, ws, user_id, ctx.role)
        with self.db.tx() as tx:
            if self.db.dialect == "postgres":
                tx.execute("SELECT pg_advisory_xact_lock(7243002, hashtext(?))", (ws,))
            existing = tx.fetchone("SELECT * FROM llm_admissions WHERE request_id=?", (request_id,))
            if existing:
                identity = (existing["workspace_id"], existing["user_id"], existing["provider"], existing["model"])
                if identity != (ws, user_id, pc.name, model):
                    raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "模型请求 ID 已用于其他调用")
                if (
                    existing["reserved_tokens"],
                    existing["reserved_micro"],
                    existing["run_id"],
                    existing["step_id"],
                    existing["attempt_id"],
                ) != (reserved_tokens, reserved_micro, run_id, step_id, attempt_id):
                    raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "模型请求 ID 的预算或步骤身份不一致")
                return existing
            stamp = now_ms()
            in_flight = int(
                tx.scalar(
                    "SELECT COUNT(*) FROM llm_admissions WHERE workspace_id=? "
                    "AND status IN ('RESERVED','IN_FLIGHT','UNSETTLED') AND response_received_at IS NULL",
                    (ws,),
                )
                or 0
            )
            if in_flight >= self.max_inflight:
                raise AppError(
                    ErrorCode.QUOTA_EXHAUSTED,
                    "模型并发请求已达上限，请等待在途请求结束或核对后重试",
                    details={"limit": self.max_inflight, "in_flight": in_flight},
                )
            quota_day = today(stamp)
            if self.quota is not None:
                active = tx.fetchone(
                    "SELECT COUNT(*) AS calls,COALESCE(SUM(reserved_tokens),0) AS tokens "
                    "FROM llm_admissions WHERE workspace_id=? AND created_at>=? AND created_at<? "
                    "AND status IN ('RESERVED','IN_FLIGHT','UNSETTLED')",
                    (ws, day_start_ms(quota_day), day_start_ms(quota_day) + 86400_000),
                )
                self.quota.check_with_reserved(
                    tx,
                    "llm_calls",
                    1,
                    reserved=int((active or {}).get("calls") or 0),
                    workspace_id=ws,
                    day=quota_day,
                )
                self.quota.check_with_reserved(
                    tx,
                    "llm_tokens",
                    reserved_tokens,
                    reserved=int((active or {}).get("tokens") or 0),
                    workspace_id=ws,
                    day=quota_day,
                )
            for rule, where in constraints:
                since = window_start(rule["window"], self._tz(ws))
                spent = self._admission_sum(tx, "llm_usage", ws, since, rule["metric"], where)
                pending = self._admission_sum(tx, "llm_admissions", ws, since, rule["metric"], where)
                requested = reserved_tokens if rule["metric"] == "tokens" else reserved_micro
                limit = int(dec(rule["limit"]) * (MICRO if rule["metric"] == "cost" else 1))
                if spent + pending + requested > limit:
                    raise AppError(
                        ErrorCode.QUOTA_EXHAUSTED,
                        "模型额度不足，无法预留本次请求",
                        details={
                            "metric": rule["metric"],
                            "limit": limit,
                            "spent": spent,
                            "reserved": pending,
                            "requested": requested,
                        },
                    )
            tx.execute(
                "INSERT INTO llm_admissions(request_id,run_id,step_id,attempt_id,workspace_id,user_id,scope,"
                "provider,model,group_id,reserved_tokens,reserved_micro,status,created_at,pricing_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    run_id,
                    step_id,
                    attempt_id,
                    ws,
                    user_id,
                    pc.scope,
                    pc.name,
                    model,
                    pc.group_id or None,
                    reserved_tokens,
                    reserved_micro,
                    "RESERVED",
                    stamp,
                    json.dumps(self._price_snapshot(pc, model), separators=(",", ":")),
                ),
            )
            return tx.fetchone("SELECT * FROM llm_admissions WHERE request_id=?", (request_id,))

    def mark_dispatched(self, request_id: str) -> bool:
        """Consume the one dispatch right before calling an external provider."""
        with self.db.tx() as tx:
            row = tx.fetchone("SELECT status FROM llm_admissions WHERE request_id=?", (request_id,))
            if row is None:
                raise KeyError(request_id)
            return bool(
                tx.execute(
                    "UPDATE llm_admissions SET status='IN_FLIGHT' WHERE request_id=? AND status='RESERVED'",
                    (request_id,),
                )
            )

    def admission(self, request_id: str) -> dict[str, Any] | None:
        return self.db.fetchone("SELECT * FROM llm_admissions WHERE request_id=?", (request_id,))

    def settle_admission(
        self,
        pc: ProviderConfig,
        model: str,
        request_id: str,
        usage: dict[str, Any] | None,
        *,
        response_confirmed: bool = False,
    ) -> bool:
        """Exactly one usage row on confirmed settlement; missing usage stays reserved."""
        with self.db.tx() as tx:
            row = tx.fetchone("SELECT * FROM llm_admissions WHERE request_id=?", (request_id,))
            if row is None:
                raise KeyError(request_id)
            if row["provider"] != pc.name or row["model"] != model:
                raise ValueError("admission provider/model mismatch")
            if row["status"] == "SETTLED":
                if usage is not None:
                    b = usage_breakdown(usage)
                    prices = json.loads(row["pricing_json"]) if row["pricing_json"] else self._price_snapshot(pc, model)
                    cost = self._priced_cost(prices, b["input"], b["output"], b["cache_read"], b["cache_write"])
                    recorded = tx.fetchone(
                        "SELECT input_tokens,output_tokens,cache_read_tokens,cache_write_tokens "
                        "FROM llm_usage WHERE admission_id=?",
                        (request_id,),
                    )
                    if recorded is None or (b["input"], b["output"], b["cache_read"], b["cache_write"], cost) != (
                        recorded["input_tokens"],
                        recorded["output_tokens"],
                        recorded["cache_read_tokens"],
                        recorded["cache_write_tokens"],
                        row["actual_micro"],
                    ):
                        raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "模型请求已按不同用量结算")
                return False
            if row["status"] == "RELEASED":
                raise ValueError("released admission cannot be settled")
            if usage is None:
                tx.execute(
                    "UPDATE llm_admissions SET status='UNSETTLED',"
                    "response_received_at=COALESCE(response_received_at,?) WHERE request_id=?",
                    (now_ms() if response_confirmed else None, request_id),
                )
                return False
            b = usage_breakdown(usage)
            prices = json.loads(row["pricing_json"]) if row["pricing_json"] else self._price_snapshot(pc, model)
            cost = self._priced_cost(prices, b["input"], b["output"], b["cache_read"], b["cache_write"])
            tx.execute(
                "INSERT INTO llm_usage(workspace_id,user_id,scope,provider,model,input_tokens,output_tokens,"
                "cache_read_tokens,cache_write_tokens,cost_micro,created_at,group_id,admission_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["workspace_id"],
                    row["user_id"],
                    row["scope"],
                    row["provider"],
                    row["model"],
                    b["input"],
                    b["output"],
                    b["cache_read"],
                    b["cache_write"],
                    cost,
                    now_ms(),
                    row["group_id"],
                    request_id,
                ),
            )
            actual_tokens = b["input"] + b["output"]
            tx.execute(
                "UPDATE llm_admissions SET status='SETTLED',actual_tokens=?,actual_micro=?,"
                "over_tokens=?,over_micro=?,settled_at=?,response_received_at=COALESCE(response_received_at,?) WHERE request_id=?",
                (
                    actual_tokens,
                    cost,
                    max(0, actual_tokens - row["reserved_tokens"]),
                    max(0, cost - row["reserved_micro"]),
                    now_ms(),
                    now_ms(),
                    request_id,
                ),
            )
            if self.quota is not None:
                # The provider usage row, admission status and daily workspace
                # counters commit together. Replaying settlement returns before
                # this block, so a retry cannot count the call twice.
                quota_day = today(row["created_at"])
                self.quota.add_in_transaction(tx, "llm_calls", 1, workspace_id=row["workspace_id"], day=quota_day)
                self.quota.add_in_transaction(
                    tx, "llm_tokens", actual_tokens, workspace_id=row["workspace_id"], day=quota_day
                )
            return True

    def release_admission(self, request_id: str) -> bool:
        """Release only after the provider confirms that the request incurred no usage."""
        with self.db.tx() as tx:
            row = tx.fetchone("SELECT status FROM llm_admissions WHERE request_id=?", (request_id,))
            if row is None:
                raise KeyError(request_id)
            if row["status"] in {"SETTLED", "RELEASED"}:
                return False
            tx.execute(
                "UPDATE llm_admissions SET status='RELEASED',actual_tokens=0,actual_micro=0,"
                "settled_at=?,response_received_at=COALESCE(response_received_at,?) WHERE request_id=?",
                (now_ms(), now_ms(), request_id),
            )
            return True

    def _sum(
        self,
        ws: str,
        since_ms: int,
        *,
        provider: str | None = None,
        scope: str | None = None,
        user_id: str | None = None,
        owner_only: bool = False,
        group_id: str | None = None,
        user_ids: list[str] | None = None,
        include_reserved: bool = False,
    ) -> tuple[int, int]:
        sql = (
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS t, COALESCE(SUM(cost_micro), 0) AS c "
            "FROM llm_usage WHERE workspace_id=? AND created_at>=?"
        )
        args: list[Any] = [ws, since_ms]
        if provider:
            sql += " AND provider=?"
            args.append(provider)
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        if user_id:
            sql += " AND user_id=?"
            args.append(user_id)
        if group_id:
            sql += " AND group_id=?"
            args.append(group_id)
        if user_ids is not None:
            if not user_ids:
                return 0, 0
            sql += f" AND user_id IN ({','.join('?' * len(user_ids))})"
            args.extend(user_ids)
        row = self.db.fetchone(sql, tuple(args)) or {}
        tokens, cost = int(row.get("t") or 0), int(row.get("c") or 0)
        if include_reserved:
            where = (
                {"provider": provider, "scope": scope, "user_id": user_id, "group_id": group_id, "user_ids": user_ids}
                if user_ids is not None
                else {
                    "provider": provider,
                    "scope": scope,
                    "user_id": user_id,
                    "group_id": group_id,
                }
            )
            tokens += self._admission_sum(self.db, "llm_admissions", ws, since_ms, "tokens", where)
            cost += self._admission_sum(self.db, "llm_admissions", ws, since_ms, "cost", where)
        return tokens, cost

    def _task_usage(
        self,
        ws: str,
        since_ms: int,
        metric: str,
        *,
        user_id: str | None = None,
        user_ids: list[str] | None = None,
    ) -> Decimal:
        """成员在回测 / 优化任务上的资源用量（从 runs 表实时汇总）。"""
        import json

        sql = (
            "SELECT kind, state, created_at, started_at, ended_at, manifest FROM runs "
            "WHERE workspace_id=? AND created_at>=?"
        )
        args: list[Any] = [ws, since_ms]
        if user_id:
            sql += " AND created_by=?"
            args.append(user_id)
        if user_ids is not None:
            if not user_ids:
                return Decimal(0)
            sql += f" AND created_by IN ({','.join('?' * len(user_ids))})"
            args.extend(user_ids)
        rows = self.db.fetchall(sql, tuple(args))
        now = now_ms()
        total = Decimal(0)
        for row in rows:
            kind = str(row.get("kind") or "")
            if metric == "backtests" and kind in ("backtest", "template_backtest"):
                total += 1
            elif metric == "cpu_seconds" and kind in ("backtest", "template_backtest", "optimize"):
                started = int(row.get("started_at") or 0)
                if started:
                    end = int(row.get("ended_at") or now)
                    total += Decimal(max(0, end - started)) / 1000
            elif metric == "optimize_combinations" and kind == "optimize":
                try:
                    manifest = json.loads(row.get("manifest") or "{}")
                except (TypeError, ValueError):
                    manifest = {}
                total += max(1, _int(manifest.get("combinations") or 1))
        return total

    def rule_status(
        self, ws: str, rules: list[dict[str, Any]], *, include_reserved: bool = False, **where: Any
    ) -> list[dict[str, Any]]:
        rules = clean_limits(rules)
        tz = self._tz(ws) if any(r["window"] in CALENDAR_WINDOWS for r in rules) else "Asia/Shanghai"
        out = []
        for r in rules:
            since = window_start(r["window"], tz)
            if r["metric"] in TASK_METRICS:
                used = self._task_usage(
                    ws,
                    since,
                    r["metric"],
                    user_id=where.get("user_id"),
                    user_ids=where.get("user_ids"),
                )
            elif r["metric"] == "storage_bytes":
                # 存储是当前存量，不按 window 起点回溯。
                used = Decimal(upload_storage(self.db, ws, where.get("user_id"))["used_bytes"])
            elif r["metric"] == "record_units":
                # 研究记录也是当前存量：按记录倍数折算，PIN 不影响占用。
                from ..platform import backtest_retention as BR

                targets = [where.get("user_id")] if where.get("user_id") else list(where.get("user_ids") or [])
                used = sum(
                    (Decimal(str(BR.record_usage(self._ws(ws), str(uid))["total_units"])) for uid in targets if uid),
                    Decimal(0),
                )
            elif r["metric"] == "tokens":
                tokens, _ = self._sum(ws, since, include_reserved=include_reserved, **where)
                used = Decimal(tokens)
            else:
                _, cost = self._sum(ws, since, include_reserved=include_reserved, **where)
                used = Decimal(cost) / MICRO
            limit = dec(r["limit"])
            pct = int(min(Decimal(999), used * 100 / limit)) if limit > 0 else 0
            display_used = (
                used.quantize(Decimal("0.0001"))
                if r["metric"] == "cost"
                else Decimal(int(used))
                if r["metric"] == "storage_bytes"
                else used
            )
            label = (
                f"存储空间上限 ≤ {fmt_bytes(limit)}"
                if r["metric"] == "storage_bytes"
                else f"研究记录上限 ≤ {r['limit']} 单位"
                if r["metric"] == "record_units"
                else f"{WINDOW_LABEL[r['window']]} {METRIC_LABEL[r['metric']]} ≤ {r['limit']}"
            )
            out.append(
                {
                    **r,
                    "used": str(display_used),
                    "percent": pct,
                    "exceeded": used >= limit,
                    "label": label,
                }
            )
        return out

    def guard(self, pc: ProviderConfig, model: str) -> str | None:
        """超额返回原因（路由器据此跳过该服务商），否则 None。"""
        ctx = context.current()
        ws = ctx.workspace_id
        if not ws:
            return None
        where: dict[str, Any] = {"provider": pc.name, "scope": pc.scope}
        if pc.scope == "personal":
            where["user_id"] = pc.owner_id
        if pc.scope == "group":
            where["group_id"] = pc.group_id
        model_rules = [r for r in pc.limits if r.get("metric") not in ("storage_bytes", "record_units")]
        for st in self.rule_status(ws, model_rules, include_reserved=True, **where):
            if st["exceeded"]:
                return f"已达服务商限额（{st['label']}）"
        human = bool(ctx.user_id and ctx.user_id != "system")
        if pc.scope == "group" and human and pc.group_id:
            rules = [
                r
                for r in (self.group_settings(pc.group_id).get("member_limits") or [])
                if r.get("metric") not in ("storage_bytes", "record_units")
            ]
            for st in self.rule_status(
                ws, rules, include_reserved=True, scope="group", group_id=pc.group_id, user_id=ctx.user_id
            ):
                if st["exceeded"]:
                    return f"已达群主给你设定的群组模型限额（{st['label']}）"
        if pc.scope == "workspace" and human:
            pol = self.policy(ws, ctx.user_id, ctx.role)
            model_limits = [r for r in pol["limits"] if r.get("metric") not in ("storage_bytes", "record_units")]
            for st in self.rule_status(ws, model_limits, include_reserved=True, scope="workspace", user_id=ctx.user_id):
                if st["exceeded"]:
                    return f"已达你的成员限额（{st['label']}），可以在「额度与用量」里申请提升"
            # 工作区管理员分给群组的共享模型额度：全体群成员合计
            for gid in self.my_groups(ws, ctx.user_id):
                rules = [
                    r
                    for r in (self.group_settings(gid).get("ws_limits") or [])
                    if r.get("metric") not in ("storage_bytes", "record_units")
                ]
                if not rules:
                    continue
                for st in self.rule_status(
                    ws, rules, include_reserved=True, scope="workspace", user_ids=self.group_members(gid)
                ):
                    if st["exceeded"]:
                        return f"你所在群组的共享模型额度已用完（{st['label']}）"
        return None

    def storage_snapshot(self, ws: str, user_id: str | None = None) -> dict[str, Any]:
        """当前成员（或整个工作区）的上传存储统计。"""
        return upload_storage(self.db, ws, user_id)

    def enforce_storage_upload(
        self, ws: str, user_id: str, role: str | None, used_bytes: int, pending_bytes: int
    ) -> None:
        """上传前校验该成员的所有存储规则；任一条超限即拒绝。

        存储规则统一按累计口径（current stock），多条规则同时生效。管理员不受成员
        配额约束，与其它成员限额保持同一权限口径。
        """
        if role == "admin" or not user_id:
            return
        rules = [r for r in self.policy(ws, user_id, role)["limits"] if r.get("metric") == "storage_bytes"]
        for rule in rules:
            limit = dec(rule.get("limit"))
            if limit <= 0:
                continue
            if Decimal(used_bytes) + Decimal(pending_bytes) <= limit:
                continue
            raise AppError(
                ErrorCode.QUOTA_EXHAUSTED,
                f"存储空间配额已用完：已用 {fmt_bytes(used_bytes)}，上限 {fmt_bytes(limit)}，"
                f"本次上传需要 {fmt_bytes(pending_bytes)}。请删除不需要的图片或附件，或联系管理员调整限额。",
                details={
                    "metric": "storage_bytes",
                    "label": f"存储空间上限 ≤ {fmt_bytes(limit)}",
                    "unit": "字节",
                    "used": str(used_bytes),
                    "limit": plain(limit),
                    "requested": str(pending_bytes),
                    "quota_key": "member_storage_bytes",
                },
            )

    def _usage_bucket(self, ws: str, since_ms: int, **where: Any) -> dict[str, Any]:
        sql = (
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, COALESCE(SUM(cost_micro), 0) AS cost_micro, "
            "MAX(created_at) AS last_used FROM llm_usage WHERE workspace_id=? AND created_at>=?"
        )
        args: list[Any] = [ws, since_ms]
        for key in ("provider", "scope", "user_id", "group_id"):
            if where.get(key):
                sql += f" AND {key}=?"
                args.append(where[key])
        row = self.db.fetchone(sql, tuple(args)) or {}
        return {
            "calls": int(row.get("calls") or 0),
            "tokens": int(row.get("tokens") or 0),
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
            "cost": fmt_cost(int(row.get("cost_micro") or 0)),
            "last_used": int(row.get("last_used") or 0),
        }

    @staticmethod
    def _parse_iso_ms(value: Any) -> int | None:
        if not value:
            return None
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
        except (TypeError, ValueError, OverflowError):
            return None

    def _fmt_quota_time(self, ms: int | None, tz: str) -> str:
        if not ms:
            return ""
        try:
            zone = ZoneInfo(tz)
        except Exception:
            zone = timezone.utc
        return datetime.fromtimestamp(ms / 1000, zone).strftime("%m-%d %H:%M")

    @staticmethod
    def _opencode_endpoint(base: str) -> str:
        """只接受 opencode.ai 的 HTTPS Go 推理地址，避免把密钥发到其他主机。"""
        try:
            parts = urlsplit(str(base or "").strip())
        except ValueError:
            return ""
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or (host != "opencode.ai" and not host.endswith(".opencode.ai")):
            return ""
        path = parts.path.rstrip("/")
        if path == "/inference/go":
            path += "/v1"
        if not path.startswith("/inference/go/"):
            return ""
        return f"https://{parts.netloc}{path}/usage"

    def _opencode_usage(self, ws: str, rec: dict[str, Any], *, refresh: bool = False) -> dict[str, Any] | None:
        """读取 OpenCode Go 的真实 5 小时 / 周 / 月额度。

        OpenCode 的推理密钥可直接访问 ``/inference/go/v1/usage``；这里只做只读查询，
        不把密钥写入缓存键或日志。接口失败时返回 available=False，由界面回退到平台记账。
        """
        endpoint = self._opencode_endpoint(str(rec.get("base_url") or ""))
        if not endpoint:
            return None
        key = self.api_key(rec)
        if not key:
            return {"available": False, "reason": "未配置密钥", "updated_at": now_ms(), "windows": {}}
        cache_key = hashlib.sha256(f"{endpoint}\0{key}".encode()).hexdigest()
        cached = None if refresh else self._remote_quota_cache.get(cache_key)
        if cached:
            ttl = 60 if cached[1].get("available") else 20
            if time.time() - cached[0] < ttl:
                return cached[1]
        out: dict[str, Any]
        try:
            req = urllib.request.Request(
                endpoint,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Accept": "application/json",
                    "User-Agent": "quant-agent/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = json.loads(resp.read(65536).decode("utf-8", "replace"))
            usage = payload.get("usage") if isinstance(payload, dict) else None
            windows: dict[str, Any] = {}
            for src, (name, label) in (
                ("rolling", ("5h", "5 小时")),
                ("weekly", ("week", "本周")),
                ("monthly", ("month", "本月")),
            ):
                item = usage.get(src) if isinstance(usage, dict) else None
                if not isinstance(item, dict):
                    continue
                try:
                    used = max(0.0, min(100.0, float(item.get("percent") or 0)))
                except (TypeError, ValueError):
                    used = 0.0
                resets_at = self._parse_iso_ms(item.get("resetsAt"))
                windows[name] = {
                    "label": label,
                    "used_pct": round(used, 1),
                    "remaining_pct": round(max(0.0, 100.0 - used), 1),
                    "resets_at": resets_at,
                    "resets_at_text": self._fmt_quota_time(resets_at, self._tz(ws)),
                    "status": str(item.get("status") or "ok"),
                }
            if not windows:
                raise ValueError("响应中没有额度窗口")
            fetched_at = now_ms()
            out = {
                "available": True,
                "source": "OpenCode Go usage",
                "timezone": self._tz(ws),
                "updated_at": fetched_at,
                "updated_at_text": self._fmt_quota_time(fetched_at, self._tz(ws)),
                "windows": windows,
            }
        except Exception as exc:  # 网络、鉴权或响应格式异常都不应影响额度页其余内容
            if isinstance(exc, urllib.error.HTTPError):
                reason = f"HTTP {exc.code}"
            elif isinstance(exc, urllib.error.URLError):
                reason = "无法连接 OpenCode"
            elif isinstance(exc, ValueError):
                reason = str(exc)
            else:
                reason = "额度接口响应异常"
            out = {"available": False, "reason": reason, "updated_at": now_ms(), "windows": {}}
        self._remote_quota_cache[cache_key] = (time.time(), out)
        return out

    def provider_quota_cards(
        self, ws: str, user_id: str | None, role: str | None, *, refresh: bool = False
    ) -> dict[str, Any]:
        """按模型来源汇总 5 小时 / 周 / 月 / 累计用量，供额度页卡片展示。"""
        now = time.time()
        tz = self._tz(ws)
        starts = {w: window_start(w, tz, now) for w in ("5h", "week", "month", "total")}
        cards = []
        for rec in self.visible(ws, user_id, role):
            where: dict[str, Any] = {"provider": rec["name"], "scope": rec.get("scope", "workspace")}
            if where["scope"] == "personal":
                where["user_id"] = rec.get("owner_id")
            elif where["scope"] == "group":
                where["group_id"] = rec.get("group_id")
            buckets = {w: self._usage_bucket(ws, starts[w], **where) for w in starts}
            limits = list(rec.get("limits") or [])
            status = self.rule_status(ws, limits, **where)
            has_key = bool(rec.get("key_enc")) or self._keyless(rec)
            base = str(rec.get("base_url") or "")
            billing = "subscription" if self._opencode_endpoint(base) else "api"
            remote = self._opencode_usage(ws, rec, refresh=refresh) if billing == "subscription" else None
            remote_used = [float(w.get("used_pct") or 0) for w in (remote or {}).get("windows", {}).values()]
            if not has_key:
                state = "missing"
            elif any(x["exceeded"] for x in status) or any(x >= 100 for x in remote_used):
                state = "exhausted"
            elif any(x["percent"] >= 80 for x in status) or any(x >= 80 for x in remote_used):
                state = "near"
            else:
                state = "active"
            total = buckets["total"]
            cards.append(
                {
                    "name": rec["name"],
                    "label": rec.get("label") or rec["name"],
                    "scope": rec.get("scope", "workspace"),
                    "type": rec.get("type", "openai_compatible"),
                    "models": list(rec.get("models") or []),
                    "billing": billing,
                    "has_key": has_key,
                    "state": state,
                    "windows": {w: {k: v for k, v in b.items() if k != "last_used"} for w, b in buckets.items()},
                    "total": total,
                    "limits": status,
                    "remote": remote,
                    "avg_cost_per_call": round(float(dec(total["cost"]) / max(1, total["calls"])), 4),
                    "last_used": total["last_used"],
                }
            )
        cards.sort(key=lambda c: (0 if c["scope"] == "workspace" else 1, c["label"].lower()))
        return {
            "cards": cards,
            "note": "OpenCode 来源优先读取服务商实时额度（5 小时 / 本周 / 本月及刷新时间）；其余来源按平台实际调用记录汇总，服务商侧余额不可用时不会推算或虚构。",
        }

    def task_guard(self, kind: str, manifest: dict[str, Any] | None = None) -> str | None:
        """成员启动计算任务前的资源限额检查：回测次数、CPU 秒、优化组合数。"""
        ctx = context.current()
        ws = ctx.workspace_id
        user_id = ctx.user_id
        if not ws or not user_id or user_id == "system" or ctx.role == "admin":
            return None
        if kind in ("backtest", "template_backtest"):
            metrics = {"cpu_seconds", "backtests"}
            requested = {"backtests": Decimal(1)}
        elif kind == "optimize":
            metrics = {"cpu_seconds", "optimize_combinations"}
            requested = {"optimize_combinations": max(1, _int((manifest or {}).get("combinations") or 1))}
        else:
            return None
        rules = [r for r in self.policy(ws, user_id, ctx.role)["limits"] if r.get("metric") in metrics]
        for st in self.rule_status(ws, rules, scope="workspace", user_id=user_id):
            used = dec(st["used"])
            limit = dec(st["limit"])
            need = Decimal(requested.get(st["metric"], 0))
            if used + need > limit:
                return f"你的{METRIC_LABEL[st['metric']]}额度不足（{st['label']}），请等待额度重置或申请提升"
        return None

    def stats(self, ws: str, *, days: int, user_id: str | None = None) -> dict[str, Any]:
        """用量统计。user_id 为空时是整个工作区（管理员视角）。"""
        since = now_ms() - days * 86400_000
        where = "workspace_id=? AND created_at>=?"
        args: list[Any] = [ws, since]
        if user_id:
            where += " AND user_id=?"
            args.append(user_id)
        by_model = self.db.fetchall(
            "SELECT provider, scope, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, "
            "SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_write_tokens) AS cache_write_tokens, "
            f"SUM(output_tokens) AS output_tokens, SUM(cost_micro) AS cost_micro FROM llm_usage WHERE {where} "
            "GROUP BY provider, scope, model ORDER BY cost_micro DESC, calls DESC",
            tuple(args),
        )
        by_user = self.db.fetchall(
            "SELECT user_id, COUNT(*) AS calls, SUM(input_tokens + output_tokens) AS tokens, "
            "SUM(cost_micro) AS cost_micro "
            f"FROM llm_usage WHERE {where} GROUP BY user_id ORDER BY cost_micro DESC, tokens DESC",
            tuple(args),
        )
        rows = self.db.fetchall(
            f"SELECT created_at, input_tokens, output_tokens, cost_micro FROM llm_usage WHERE {where}", tuple(args)
        )
        daily: dict[str, dict[str, int]] = {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(int(r["created_at"]) / 1000))
            d = daily.setdefault(day, {"tokens": 0, "cost_micro": 0, "calls": 0})
            d["tokens"] += int(r["input_tokens"] or 0) + int(r["output_tokens"] or 0)
            d["cost_micro"] += int(r["cost_micro"] or 0)
            d["calls"] += 1
        series = []
        for i in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400))
            d = daily.get(day, {"tokens": 0, "cost_micro": 0, "calls": 0})
            series.append({"day": day, "tokens": d["tokens"], "calls": d["calls"], "cost": fmt_cost(d["cost_micro"])})
        norm = lambda r: {  # noqa: E731
            **{k: v for k, v in r.items() if k != "cost_micro"},
            "cost": fmt_cost(r.get("cost_micro") or 0),
            **(
                {
                    "input_tokens": int(r["input_tokens"] or 0),
                    "output_tokens": int(r["output_tokens"] or 0),
                    "cache_read_tokens": int(r.get("cache_read_tokens") or 0),
                    "cache_write_tokens": int(r.get("cache_write_tokens") or 0),
                }
                if "input_tokens" in r
                else {}
            ),
        }
        total_tokens = sum(int(r["input_tokens"] or 0) + int(r["output_tokens"] or 0) for r in by_model)
        total_cost = sum(int(r["cost_micro"] or 0) for r in by_model)
        total_in = sum(int(r["input_tokens"] or 0) for r in by_model)
        total_out = sum(int(r["output_tokens"] or 0) for r in by_model)
        total_read = sum(int(r.get("cache_read_tokens") or 0) for r in by_model)
        total_write = sum(int(r.get("cache_write_tokens") or 0) for r in by_model)
        return {
            "days": days,
            "total": {
                "calls": sum(int(r["calls"]) for r in by_model),
                "tokens": total_tokens,
                "cost": fmt_cost(total_cost),
                # 上下文构成：input_tokens 是全部输入（含缓存读写），output_tokens 是输出。
                # 前端用 max(0, input - cache_read - cache_write) 得到未缓存输入，再分色展示。
                "input_tokens": total_in,
                "output_tokens": total_out,
                "cache_read_tokens": total_read,
                "cache_write_tokens": total_write,
                # 缓存命中率：命中的输入 / 全部输入
                "cache_hit_rate": round(total_read / total_in, 4) if total_in else None,
            },
            "by_model": [norm(r) for r in by_model],
            "by_user": [norm(r) for r in by_user],
            "daily": series,
            "storage": upload_storage(self.db, ws, user_id),
            "tasks": {
                metric: plain(self._task_usage(ws, since, metric, user_id=user_id))
                for metric in ("backtests", "cpu_seconds", "optimize_combinations")
            },
        }

    # ------------------------------------------------------------------ 额度申请
    def requests(self, ws: str, user_id: str | None = None) -> list[dict[str, Any]]:
        items = self._ws(ws).list(REQUESTS)
        return [r for r in items if not user_id or r.get("user_id") == user_id]

    def create_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        ctx = context.current()
        ws = context.current_workspace()
        rule = clean_limits([payload])
        if not rule:
            raise AppError(ErrorCode.INVALID_PARAMETER, "请选择计量方式、时间段并填写希望的额度")
        pending = [r for r in self.requests(ws, ctx.user_id) if r.get("status") == "pending"]
        if len(pending) >= 3:
            raise AppError(ErrorCode.INVALID_PARAMETER, "你已有 3 个待处理的申请，等管理员处理后再提交")
        return self._ws(ws).upsert(
            REQUESTS,
            {
                **rule[0],
                "user_id": ctx.user_id,
                "user_name": ctx.user_name or ctx.user_email,
                "reason": str(payload.get("reason") or "").strip()[:300],
                "status": "pending",
                "created_ms": now_ms(),
            },
        )

    def decide_request(self, rid: str, approve: bool, note: str = "") -> dict[str, Any]:
        ctx = context.current()
        ws = context.current_workspace()
        if ctx.role != "admin" and not ctx.is_admin:
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有管理员可以处理额度申请")
        req = self._ws(ws).get(REQUESTS, rid)
        if not req or req.get("status") != "pending":
            raise AppError(ErrorCode.NOT_FOUND, "申请不存在或已处理")
        if approve:
            pol = self.policy(ws, req["user_id"], self._role(ws, req["user_id"]))
            rules = [r for r in pol["limits"] if not (r["metric"] == req["metric"] and r["window"] == req["window"])]
            rules.append({"metric": req["metric"], "window": req["window"], "limit": req["limit"]})
            self.set_policy(ws, req["user_id"], {"limits": rules})
        req.update(
            {
                "status": "approved" if approve else "denied",
                "note": note[:200],
                "decided_by": ctx.user_id,
                "decided_ms": now_ms(),
            }
        )
        return self._ws(ws).upsert(REQUESTS, req)

    # ------------------------------------------------------------------ 旧版迁移
    def migrate_legacy(self, global_store: Any, secrets: dict[str, str]) -> int:
        """旧版本把服务商存在部署级 global store：迁到部署管理员拥有的工作区，作为工作区共享服务商。"""
        if global_store is None or global_store.get(Collections.SETTINGS, "llm_hub_migrated"):
            return 0
        legacy = global_store.list(Collections.PROVIDERS)
        routing = global_store.get(Collections.SETTINGS, "routing") or {}
        moved = 0
        if legacy or routing:
            rows = self.db.fetchall(
                "SELECT w.id FROM workspaces w JOIN users u ON u.id = w.owner_id "
                "WHERE u.is_admin=1 AND w.status='active'"
            )
            for row in rows:
                store = self.store.for_workspace(row["id"])
                for p in legacy:
                    name = p.get("name")
                    if not name or store.get(PROVIDERS, f"ws:{name}"):
                        continue
                    key = secrets.get(name, "")
                    store.upsert(
                        PROVIDERS,
                        {
                            "id": f"ws:{name}",
                            "name": name,
                            "scope": "workspace",
                            "owner_id": "",
                            "type": p.get("type", "openai_compatible"),
                            "base_url": p.get("base_url", ""),
                            "models": p.get("models", []),
                            "model_settings": p.get("model_settings", {}),
                            "currency": "CNY",
                            "fx_rate": "1",
                            "limits": [],
                            "key_enc": self.secrets.encrypt(key) if key else "",
                            "key_masked": mask(key),
                        },
                    )
                    moved += 1
                if routing and not store.get(Collections.SETTINGS, "llm_routing"):
                    # 旧路由可能指向已不再内置的服务商（openai、local 等）：只保留迁过来的
                    known = {p.get("name") for p in legacy}
                    keep = lambda t, known=known: isinstance(t, str) and t.partition("/")[0] in known  # noqa: E731
                    store.upsert(
                        Collections.SETTINGS,
                        {
                            "id": "llm_routing",
                            **{k: routing.get(k, "") if keep(routing.get(k)) else "" for k in ROUTING_KEYS},
                            "fallback": [t for t in routing.get("fallback") or [] if keep(t)],
                        },
                    )
        global_store.upsert(Collections.SETTINGS, {"id": "llm_hub_migrated", "moved": moved})
        return moved


class ModelHubAdmission:
    """Give legacy ModelRouter calls the same atomic admission authority as Runtime."""

    def __init__(self, hub: ModelHub) -> None:
        self.hub = hub

    def reserve(self, pc: ProviderConfig, model: str, messages: list[Message], max_tokens: int | None) -> str:
        estimated_input = max(
            1,
            sum(len(message.content) for message in messages) // 3
            + sum(len(message.images) * 1024 for message in messages),
        )
        estimated_output = max(1, max_tokens or 1024)
        request_id = f"legacy:{uuid.uuid4().hex}"
        try:
            self.hub.admit(
                pc,
                model,
                request_id=request_id,
                reserved_tokens=estimated_input + estimated_output,
                reserved_micro=self.hub.cost_micro(pc, model, estimated_input, estimated_output),
            )
        except AppError as exc:
            if exc.code == ErrorCode.QUOTA_EXHAUSTED:
                raise LLMError(exc.message) from exc
            raise
        if not self.hub.mark_dispatched(request_id):
            raise RuntimeError("new model admission could not be dispatched")
        return request_id

    def settle(self, pc: ProviderConfig, model: str, request_id: str, usage: dict[str, Any] | None) -> None:
        self.hub.settle_admission(pc, model, request_id, usage, response_confirmed=True)

    def unknown(self, pc: ProviderConfig, model: str, request_id: str) -> None:
        self.hub.settle_admission(pc, model, request_id, None)


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
