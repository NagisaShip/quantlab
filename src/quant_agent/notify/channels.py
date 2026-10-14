"""通知层（对应增补设计第 5.4 节）。

统一 Notifier 接口，覆盖企业微信、Server 酱、PushPlus、控制台。
所有通道失败不抛出致命错误，返回 False 由上层入待发队列重试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

DISCLAIMER = "仅为研究结果，非投资建议。"


@dataclass
class Notification:
    title: str
    body: str  # markdown
    channel: str = "console"


@runtime_checkable
class Notifier(Protocol):
    def send(self, note: Notification) -> bool: ...


class ConsoleNotifier:
    """调试用：打到标准输出。"""

    def send(self, note: Notification) -> bool:
        print(f"\n=== [{note.channel}] {note.title} ===\n{note.body}\n")
        return True


class WeComNotifier:
    """企业微信群机器人 Webhook（推荐通道）。"""

    def __init__(self, webhook_url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.webhook_url = webhook_url
        self._client = httpx.Client(timeout=10.0, transport=transport)

    def send(self, note: Notification) -> bool:
        payload = {"msgtype": "markdown", "markdown": {"content": f"### {note.title}\n{note.body}"}}
        try:
            resp = self._client.post(self.webhook_url, json=payload)
            resp.raise_for_status()
            return resp.json().get("errcode", -1) == 0
        except httpx.HTTPError:
            return False


class ServerChanNotifier:
    """Server 酱（个人兜底通道）。send_key 形如 SCT..."""

    def __init__(self, send_key: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.send_key = send_key
        self._client = httpx.Client(timeout=10.0, transport=transport)

    def send(self, note: Notification) -> bool:
        url = f"https://sctapi.ftqq.com/{self.send_key}.send"
        try:
            resp = self._client.post(url, data={"title": note.title, "desp": note.body})
            resp.raise_for_status()
            return resp.json().get("code", -1) == 0
        except httpx.HTTPError:
            return False


class PushPlusNotifier:
    """PushPlus（微信推送）。token 从 pushplus.plus 获取。"""

    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.token = token
        self._client = httpx.Client(timeout=10.0, transport=transport)

    def send(self, note: Notification) -> bool:
        payload = {"token": self.token, "title": note.title, "content": note.body, "template": "markdown"}
        try:
            resp = self._client.post("http://www.pushplus.plus/send", json=payload)
            resp.raise_for_status()
            return resp.json().get("code", -1) == 200
        except httpx.HTTPError:
            return False


class NotificationHub:
    """多通道聚合 + 待发队列：全部失败时入队，下次运行重试。"""

    def __init__(self, notifiers: dict[str, Notifier]) -> None:
        self.notifiers = notifiers
        self.pending: list[Notification] = []

    def send(self, note: Notification) -> bool:
        notifier = self.notifiers.get(note.channel)
        if notifier is None:
            self.pending.append(note)
            return False
        ok = notifier.send(note)
        if not ok:
            self.pending.append(note)
        return ok

    def flush(self) -> int:
        """重试待发队列，返回成功条数。"""
        remaining: list[Notification] = []
        sent = 0
        for note in self.pending:
            notifier = self.notifiers.get(note.channel)
            if notifier and notifier.send(note):
                sent += 1
            else:
                remaining.append(note)
        self.pending = remaining
        return sent


def build_hub(config: dict) -> NotificationHub:
    """从配置 dict 构建 hub。config 示例见 agent.config.toml。"""
    notifiers: dict[str, Notifier] = {"console": ConsoleNotifier()}
    if url := config.get("wecom_webhook"):
        notifiers["wecom"] = WeComNotifier(url)
    if key := config.get("serverchan_key"):
        notifiers["serverchan"] = ServerChanNotifier(key)
    if token := config.get("pushplus_token"):
        notifiers["pushplus"] = PushPlusNotifier(token)
    return NotificationHub(notifiers)
