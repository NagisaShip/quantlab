"""发信：账号绑定邮箱的验证码邮件。

配置（部署时的环境变量）：

- ``QAGENT_SMTP_HOST`` / ``QAGENT_SMTP_PORT``（默认 465）
- ``QAGENT_SMTP_USER`` / ``QAGENT_SMTP_PASSWORD``
- ``QAGENT_SMTP_FROM``（发件人，默认同 USER）
- ``QAGENT_SMTP_SECURITY``：``ssl``（默认，465 端口）| ``starttls``（587 端口）| ``none``

本地开发没有发信服务时设 ``QAGENT_MAIL_OUTBOX=1``：邮件写成 .eml 文件放进 ``$QAGENT_HOME/outbox/``，
不真的发出去。两者都没配时，发信接口直接报「部署没有配置发信服务」，不会假装发送成功。
"""

from __future__ import annotations

import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

from .errors import AppError, ErrorCode


def configured() -> str:
    """当前的发信方式：smtp | outbox | ""（未配置）。"""
    if os.environ.get("QAGENT_SMTP_HOST"):
        return "smtp"
    if os.environ.get("QAGENT_MAIL_OUTBOX") == "1":
        return "outbox"
    return ""


def _home() -> Path:
    return Path(os.environ.get("QAGENT_HOME", "") or (Path.home() / ".quant_agent"))


def send(to: str, subject: str, text: str, html: str | None = None) -> str:
    """发一封邮件，返回发送方式。失败抛 AppError（不吞掉，调用方要让用户知道没发出去）。"""
    mode = configured()
    if not mode:
        raise AppError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "部署没有配置发信服务（QAGENT_SMTP_HOST），暂时无法发送验证码，请联系部署管理员",
        )
    sender = os.environ.get("QAGENT_SMTP_FROM") or os.environ.get("QAGENT_SMTP_USER") or "quantlab@localhost"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("QuantLab", sender))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain="quantlab")
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    if mode == "outbox":
        box = _home() / "outbox"
        box.mkdir(parents=True, exist_ok=True)
        (box / f"{int(time.time() * 1000)}-{to.replace('@', '_at_')}.eml").write_bytes(bytes(msg))
        return mode
    host = os.environ["QAGENT_SMTP_HOST"]
    port = int(os.environ.get("QAGENT_SMTP_PORT") or 465)
    security = (os.environ.get("QAGENT_SMTP_SECURITY") or ("ssl" if port == 465 else "starttls")).lower()
    user, password = os.environ.get("QAGENT_SMTP_USER"), os.environ.get("QAGENT_SMTP_PASSWORD")
    try:
        if security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if security == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if user and password:
                server.login(user, password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        raise AppError(
            ErrorCode.UPSTREAM_UNAVAILABLE, f"邮件发送失败：{type(exc).__name__}，请稍后再试或联系部署管理员"
        ) from exc
    return mode
