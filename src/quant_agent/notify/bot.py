"""双向聊天机器人：在飞书 / 企业微信 / Telegram 里查状态、问原因、一键停止。

命令（中英文都认）：

- `帮助` / `help`
- `绑定 123456`：设置页生成的一次性绑定码，把这个聊天绑到你的账号（只有绑定过的聊天能下命令）
- `状态` / `status`：节点心跳 + 运行中的实验（编号、收益、持仓、熔断）
- `为什么 2` / `why 2`：第 2 个实验最近几笔成交的原因、最近的风控事件
- `停止 2` / `stop 2`：停止，保留持仓；`平仓 2` / `close 2`：停止并平掉全部持仓；编号可写「全部」/ all
- 停止和平仓都要二次确认：机器人回一个 4 位确认码，2 分钟内回 `确认 4821` 才执行

执行走的是页面上同一个停止 / 平仓接口（以绑定用户的身份、记审计），所以和在网页上点按钮效果一样。
机器人只认这几条命令，不接受自由文本指令，也没有启动、加仓、提现之类的能力。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import struct
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

BINDINGS = "bot_bindings"
BIND_CODES = "bot_bind_codes"
BIND_ATTEMPTS = "bot_bind_attempts"
PENDING = "bot_pending"
BOTS = "bot_configs"
CONFIRM_MS = 120_000
BIND_MS = 600_000

KINDS = {
    "telegram": {"label": "Telegram", "fields": ["bot_token"]},
    "feishu": {"label": "飞书", "fields": ["app_id", "app_secret", "verification_token", "encrypt_key"]},
    "wecom": {"label": "企业微信（自建应用）", "fields": ["corp_id", "agent_id", "secret", "token", "aes_key"]},
}
OPTIONAL = {"encrypt_key"}

HELP = (
    "可以发：\n状态 —— 运行中的实验\n为什么 编号 —— 最近成交的原因\n停止 编号 —— 停止（保留持仓）\n"
    "平仓 编号 —— 停止并平仓\n编号可以写「全部」。停止和平仓都要回复确认码才执行。"
)


def gstore(services: Any) -> Any:
    """机器人状态不属于某个工作区（回调进来时还不知道是谁），单独放在数据目录的 bot/ 下。"""
    from ..store import JsonStore

    st = getattr(services, "_bot_store", None)
    if st is None:
        st = JsonStore(Path(services.home) / "bot")
        try:
            services._bot_store = st
        except Exception:
            pass
    return st


def _now() -> int:
    return int(time.time() * 1000)


def _pct(x: Any) -> str:
    return "—" if x is None else f"{float(x) * 100:+.2f}%"


class BotService:
    """平台侧的命令处理，和具体聊天软件无关。"""

    def __init__(self, services: Any, *, now: Callable[[], int] = _now) -> None:
        self.services = services
        self.store = gstore(services)
        self.now = now

    # ------------------------------------------------------------------ 绑定
    def new_bind_code(self, workspace_id: str, user_id: str) -> dict[str, Any]:
        code = f"{secrets.randbelow(10**6):06d}"
        doc = {"id": code, "workspace_id": workspace_id, "user_id": user_id, "expires_at": self.now() + BIND_MS}
        self.store.upsert(BIND_CODES, doc)
        return {"code": code, "expires_at": doc["expires_at"]}

    def bindings(self, workspace_id: str) -> list[dict[str, Any]]:
        return [b for b in self.store.list(BINDINGS) if b.get("workspace_id") == workspace_id]

    def unbind(self, workspace_id: str, binding_id: str) -> bool:
        b = self.store.get(BINDINGS, binding_id)
        if not b or b.get("workspace_id") != workspace_id:
            return False
        self.store.delete(BINDINGS, binding_id)
        return True

    def _role(self, workspace_id: str, user_id: str) -> str | None:
        row = self.services.db.fetchone(
            "SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND status='active'",
            (workspace_id, user_id),
        )
        return row["role"] if row else None

    # ------------------------------------------------------------------ 入口
    def handle(self, platform: str, chat_id: str, text: str, *, sender: str = "") -> str:
        text = (text or "").strip()
        text = re.sub(r"^@\S+\s*", "", text)  # 群里 @机器人 的前缀
        key = f"{platform}:{chat_id}"
        m = re.match(r"^(绑定|bind)\s*(\d{6})$", text, re.I)
        if m:
            return self._bind(key, platform, chat_id, m.group(2), sender)
        b = self.store.get(BINDINGS, key)
        if b is None:
            return "这个聊天还没绑定。到平台「设置 → 聊天机器人」生成绑定码，然后发「绑定 123456」。"
        role = self._role(b["workspace_id"], b["user_id"])
        if role is None:
            return "绑定的账号已不在这个工作区，请重新绑定。"
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("帮助", "help", "/help", "/start", "?", "？"):
            return HELP
        if cmd in ("状态", "status", "/status"):
            return self.status(b["workspace_id"])
        if cmd in ("为什么", "why", "/why"):
            return self.why(b["workspace_id"], arg)
        if cmd in ("停止", "stop", "/stop", "平仓", "close", "/close", "停止并平仓"):
            if role not in ("admin", "member"):
                return "你在这个工作区是审阅者，只能查看，不能停止实验。"
            close = cmd in ("平仓", "close", "/close", "停止并平仓")
            return self._ask_confirm(key, b, arg, close)
        if cmd in ("确认", "confirm", "/confirm"):
            return self._confirm(key, b, arg)
        return "没看懂。" + HELP

    def _bind(self, key: str, platform: str, chat_id: str, code: str, sender: str) -> str:
        fail_key = f"fail:{key}"
        fail = self.store.get(BIND_ATTEMPTS, fail_key) or {}
        if fail.get("expires_at", 0) < self.now():
            fail = {"id": fail_key, "count": 0, "expires_at": 0}
        if fail.get("count", 0) >= 5:
            return "绑定尝试次数过多，请 10 分钟后再试，或在平台重新生成绑定码。"
        doc = self.store.get(BIND_CODES, code)
        if not doc or doc["expires_at"] < self.now():
            fail.update(count=fail.get("count", 0) + 1, expires_at=self.now() + BIND_MS)
            self.store.upsert(BIND_ATTEMPTS, fail)
            return "绑定码不对或已过期（10 分钟有效），请在平台重新生成。"
        self.store.delete(BIND_CODES, code)
        self.store.delete(BIND_ATTEMPTS, fail_key)
        self.store.upsert(
            BINDINGS,
            {
                "id": key,
                "platform": platform,
                "chat_id": chat_id,
                "sender": sender,
                "workspace_id": doc["workspace_id"],
                "user_id": doc["user_id"],
                "bound_at": self.now(),
            },
        )
        self.services.audit.record(
            "bot.bind", object_type="bot", object_id=key, workspace_id=doc["workspace_id"], user_id=doc["user_id"]
        )
        return "绑定好了。" + HELP

    # ------------------------------------------------------------------ 查询
    def _sessions(self, workspace_id: str) -> list[dict[str, Any]]:
        from ..store import Collections

        rows = self.services.db.fetchall(
            "SELECT id, state, title FROM runs WHERE workspace_id=? AND kind='paper' AND state IN "
            "('RUNNING','PAUSED','RECOVERING','QUEUED') ORDER BY created_at",
            (workspace_id,),
        )
        out = []
        for r in rows:
            doc = self.services.store.for_workspace(workspace_id).get(Collections.PAPER_SESSIONS, r["id"]) or {}
            out.append(
                {
                    **doc,
                    "id": r["id"],
                    "run_state": r["state"],
                    "strategy_name": doc.get("strategy_name") or r.get("title") or r["id"],
                }
            )
        return out

    def _pick(self, workspace_id: str, arg: str) -> list[dict[str, Any]] | str:
        rows = self._sessions(workspace_id)
        if not rows:
            return "现在没有运行中的实验。"
        if arg in ("全部", "所有", "all", "*"):
            return rows
        if arg.isdigit() and 1 <= int(arg) <= len(rows):
            return [rows[int(arg) - 1]]
        hit = [r for r in rows if r["id"] == arg]
        if hit:
            return hit
        return f"请写编号（1–{len(rows)}）或「全部」。发「状态」查看编号。"

    def status(self, workspace_id: str) -> str:
        from ..platform import watchdog

        h = watchdog.health(self.services)
        rows = self._sessions(workspace_id)
        lines = [
            f"节点：{h['workers_alive']} 个在线" + (f"，{len(h['stale_runs'])} 个实验失联" if h["stale_runs"] else "")
        ]
        for c in h["clock"]:
            if c["level"] != "ok":
                lines.append(f"时钟：{c['exchange']} 偏差 {c['offset_ms']} 毫秒")
        if not rows:
            lines.append("没有运行中的实验。")
        for i, r in enumerate(rows, 1):
            pts = r.get("equity_points") or []
            ret = pts[-1]["v"] / pts[0]["v"] - 1 if len(pts) > 1 and pts[0].get("v") else None
            pos = [
                k
                for k, v in ((r.get("portfolio") or {}).get("positions") or {}).items()
                if float((v or {}).get("amount") or 0)
            ]
            kind = "模拟盘" if r.get("execution") == "demo" else "纸面"
            flags = []
            if (r.get("demo") or {}).get("halted"):
                flags.append("已熔断")
            if r["run_state"] != "RUNNING":
                flags.append(
                    {"PAUSED": "已暂停", "RECOVERING": "恢复中", "QUEUED": "排队中"}.get(r["run_state"], r["run_state"])
                )
            lines.append(
                f"{i}. {r['strategy_name']}（{kind}）收益 {_pct(ret)}，持仓 {'、'.join(pos) or '无'}"
                + (f"，{'、'.join(flags)}" if flags else "")
            )
        return "\n".join(lines)

    def why(self, workspace_id: str, arg: str) -> str:
        picked = self._pick(workspace_id, arg or "1")
        if isinstance(picked, str):
            return picked
        r = picked[0]
        lines = [f"{r['strategy_name']}："]
        fills = (r.get("fills") or [])[-5:]
        if not fills:
            lines.append("还没有成交。")
        for f in reversed(fills):
            when = time.strftime("%m-%d %H:%M", time.gmtime(int(f.get("ts") or 0) / 1000))
            side = "买入" if f.get("side") == "buy" else "卖出"
            lines.append(f"{when} UTC {side} {f.get('security')} @ {f.get('price')}，原因：{f.get('reason') or '—'}")
        events = [e for e in (r.get("events") or []) if e.get("level") in ("warning", "error")][-3:]
        for e in events:
            lines.append(f"注意：{e.get('message')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 停止
    def _ask_confirm(self, key: str, b: dict[str, Any], arg: str, close: bool) -> str:
        picked = self._pick(b["workspace_id"], arg)
        if isinstance(picked, str):
            return picked
        code = f"{secrets.randbelow(9000) + 1000}"
        self.store.upsert(
            PENDING,
            {
                "id": key,
                "code": code,
                "ids": [r["id"] for r in picked],
                "close": close,
                "expires_at": self.now() + CONFIRM_MS,
            },
        )
        what = "停止并平掉全部持仓" if close else "停止（保留持仓）"
        names = "、".join(r["strategy_name"] for r in picked)
        return f"要{what}：{names}。\n2 分钟内回复「确认 {code}」执行。"

    def _confirm(self, key: str, b: dict[str, Any], code: str) -> str:
        p = self.store.get(PENDING, key)
        if not p or p["expires_at"] < self.now():
            return "没有待确认的操作（或已超过 2 分钟），请重新发停止 / 平仓。"
        if code != p["code"]:
            return "确认码不对。"
        self.store.delete(PENDING, key)
        from ..api.routes_studio import liquidate_paper, stop_paper
        from ..platform import context

        role = self._role(b["workspace_id"], b["user_id"]) or "member"
        done, failed = [], []
        with context.bind(b["workspace_id"], user_id=b["user_id"], role=role):
            for sid in p["ids"]:
                try:
                    out = (liquidate_paper if p["close"] else stop_paper)(sid)
                    done.append(out["session"].get("strategy_name") or sid)
                except Exception as exc:
                    failed.append(f"{sid}：{exc}")
        msg = ("已发出平仓并停止：" if p["close"] else "已停止：") + ("、".join(done) or "无")
        if failed:
            msg += "\n失败：" + "；".join(failed)
        if p["close"]:
            msg += "\n平仓成交以实验页为准，发「状态」可再看一眼。"
        return msg


# ======================================================================== 传输层
class Telegram:
    """长轮询 getUpdates：服务器不需要公网回调地址。"""

    base = "https://api.telegram.org"

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.c = httpx.Client(base_url=f"{self.base}/bot{token}", timeout=15.0, transport=transport)
        self.offset = 0

    def poll(self, bot: BotService) -> int:
        body = self.c.get("/getUpdates", params={"offset": self.offset, "timeout": 0}).json()
        n = 0
        for u in body.get("result") or []:
            self.offset = max(self.offset, int(u["update_id"]) + 1)
            msg = u.get("message") or {}
            text, chat = msg.get("text"), (msg.get("chat") or {}).get("id")
            if not text or chat is None:
                continue
            reply = bot.handle("telegram", str(chat), text, sender=str((msg.get("from") or {}).get("id", "")))
            self.send(str(chat), reply)
            n += 1
        return n

    def send(self, chat_id: str, text: str) -> None:
        self.c.post("/sendMessage", json={"chat_id": chat_id, "text": text})


class Feishu:
    """飞书自建应用：事件订阅回调 + 用 tenant_access_token 发消息。"""

    base = "https://open.feishu.cn"

    def __init__(self, cfg: dict[str, str], *, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        self.c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._tok: tuple[str, float] | None = None

    def decrypt(self, encrypted: str) -> dict[str, Any]:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.sha256(self.cfg["encrypt_key"].encode()).digest()
        raw = base64.b64decode(encrypted)
        dec = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
        data = dec.update(raw[16:]) + dec.finalize()
        return json.loads(data[: -data[-1]].decode())

    def callback(self, bot: BotService, body: dict[str, Any]) -> dict[str, Any]:
        if "encrypt" in body:
            if not self.cfg.get("encrypt_key"):
                raise PermissionError("收到加密事件，但没有配置 Encrypt Key")
            body = self.decrypt(body["encrypt"])
        token = body.get("token") or (body.get("header") or {}).get("token")
        if not secrets.compare_digest(str(token or ""), self.cfg.get("verification_token", "")):
            raise PermissionError("Verification Token 不匹配")
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge")}
        ev = body.get("event") or {}
        msg = ev.get("message") or {}
        if (body.get("header") or {}).get("event_type") == "im.message.receive_v1" and msg.get(
            "message_type"
        ) == "text":
            text = json.loads(msg.get("content") or "{}").get("text", "")
            chat = msg.get("chat_id", "")
            sender = ((ev.get("sender") or {}).get("sender_id") or {}).get("open_id", "")
            self.send(chat, bot.handle("feishu", chat, text, sender=sender))
        return {}

    def _token(self) -> str:
        if self._tok and self._tok[1] > time.time():
            return self._tok[0]
        r = self.c.post(
            "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.cfg["app_id"], "app_secret": self.cfg["app_secret"]},
        ).json()
        self._tok = (r["tenant_access_token"], time.time() + int(r.get("expire", 7200)) - 300)
        return self._tok[0]

    def send(self, chat_id: str, text: str) -> None:
        self.c.post(
            "/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {self._token()}"},
            json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text})},
        )


class WeCom:
    """企业微信自建应用：回调消息按官方方案 AES 解密，回复用应用消息接口发给发消息的人。"""

    base = "https://qyapi.weixin.qq.com"

    def __init__(self, cfg: dict[str, str], *, transport: httpx.BaseTransport | None = None) -> None:
        self.cfg = cfg
        self.c = httpx.Client(base_url=self.base, timeout=10.0, transport=transport)
        self._tok: tuple[str, float] | None = None

    def _sig(self, timestamp: str, nonce: str, encrypted: str) -> str:
        return hashlib.sha1("".join(sorted([self.cfg["token"], timestamp, nonce, encrypted])).encode()).hexdigest()

    def _aes(self) -> bytes:
        return base64.b64decode(self.cfg["aes_key"] + "=")

    def decrypt(self, encrypted: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = self._aes()
        dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        data = dec.update(base64.b64decode(encrypted)) + dec.finalize()
        data = data[: -data[-1]]
        n = struct.unpack(">I", data[16:20])[0]
        msg, corp = data[20 : 20 + n].decode(), data[20 + n :].decode()
        if corp != self.cfg["corp_id"]:
            raise PermissionError("CorpID 不匹配")
        return msg

    def encrypt(self, text: str) -> str:
        """只给测试用：按官方格式加密（随机 16 字节 + 长度 + 明文 + CorpID，PKCS7 32 字节对齐）。"""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = self._aes()
        raw = uuid.uuid4().bytes + struct.pack(">I", len(text.encode())) + text.encode() + self.cfg["corp_id"].encode()
        pad = 32 - len(raw) % 32
        raw += bytes([pad]) * pad
        enc = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
        return base64.b64encode(enc.update(raw) + enc.finalize()).decode()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        if not secrets.compare_digest(self._sig(timestamp, nonce, echostr), msg_signature):
            raise PermissionError("签名不对")
        return self.decrypt(echostr)

    def callback(self, bot: BotService, msg_signature: str, timestamp: str, nonce: str, xml: str) -> None:
        enc = _xml(xml, "Encrypt")
        if not secrets.compare_digest(self._sig(timestamp, nonce, enc), msg_signature):
            raise PermissionError("签名不对")
        inner = self.decrypt(enc)
        if _xml(inner, "MsgType") != "text":
            return
        user = _xml(inner, "FromUserName")
        self.send(user, bot.handle("wecom", user, _xml(inner, "Content"), sender=user))

    def _token(self) -> str:
        if self._tok and self._tok[1] > time.time():
            return self._tok[0]
        r = self.c.get(
            "/cgi-bin/gettoken", params={"corpid": self.cfg["corp_id"], "corpsecret": self.cfg["secret"]}
        ).json()
        self._tok = (r["access_token"], time.time() + int(r.get("expires_in", 7200)) - 300)
        return self._tok[0]

    def send(self, user: str, text: str) -> None:
        self.c.post(
            "/cgi-bin/message/send",
            params={"access_token": self._token()},
            json={"touser": user, "msgtype": "text", "agentid": int(self.cfg["agent_id"]), "text": {"content": text}},
        )


def _xml(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", xml, re.S)
    return m.group(1) if m else ""


# ======================================================================== 配置
def _secrets(services: Any) -> Any:
    from .alerts import ChannelSecrets

    return ChannelSecrets(services.secrets, services.home)


def save_bot(
    services: Any, workspace_id: str, user_id: str, kind: str, name: str, creds: dict[str, str]
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"只支持 {'、'.join(KINDS)}")
    need = [f for f in KINDS[kind]["fields"] if f not in OPTIONAL]
    missing = [f for f in need if not (creds.get(f) or "").strip()]
    if missing:
        raise ValueError(f"缺少：{'、'.join(missing)}")
    clean = {f: (creds.get(f) or "").strip() for f in KINDS[kind]["fields"]}
    doc = {
        "id": "bot_" + uuid.uuid4().hex[:12],
        "workspace_id": workspace_id,
        "kind": kind,
        "name": name or KINDS[kind]["label"],
        "secret_ref": _secrets(services).write(clean),
        "created_by": user_id,
        "created_at": _now(),
        "enabled": True,
    }
    gstore(services).upsert(BOTS, doc)
    return bot_view(doc)


def bot_view(doc: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "secret_ref"}
    out["label"] = KINDS.get(doc["kind"], {}).get("label", doc["kind"])
    out["hook_path"] = f"/api/bot/hook/{doc['kind']}/{doc['id']}" if doc["kind"] != "telegram" else None
    return out


def list_bots(services: Any, workspace_id: str) -> list[dict[str, Any]]:
    return [bot_view(d) for d in gstore(services).list(BOTS) if d.get("workspace_id") == workspace_id]


def delete_bot(services: Any, workspace_id: str, bot_id: str) -> bool:
    d = gstore(services).get(BOTS, bot_id)
    if not d or d.get("workspace_id") != workspace_id:
        return False
    _secrets(services).delete(d.get("secret_ref"))
    gstore(services).delete(BOTS, bot_id)
    return True


def creds_of(services: Any, bot_id: str) -> tuple[dict[str, Any], dict[str, str]] | None:
    d = gstore(services).get(BOTS, bot_id)
    if not d or not d.get("enabled"):
        return None
    return d, _secrets(services).read(d.get("secret_ref"))


_TG: dict[str, Telegram] = {}


def poll_telegram(services: Any, *, transport: httpx.BaseTransport | None = None) -> int:
    """worker 周期钩子：轮询所有 Telegram 机器人的新消息。"""
    n = 0
    bot = BotService(services)
    for d in gstore(services).list(BOTS):
        if d.get("kind") != "telegram" or not d.get("enabled"):
            continue
        tg = _TG.get(d["id"])
        if tg is None:
            creds = _secrets(services).read(d.get("secret_ref"))
            tg = _TG[d["id"]] = Telegram(creds["bot_token"], transport=transport)
        try:
            n += tg.poll(bot)
        except Exception:
            continue
    return n
