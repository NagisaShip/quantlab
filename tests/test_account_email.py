"""账号：用户名注册 / 登录、邮箱验证码绑定（发信走 outbox，不真的发出去）。"""

import email
import email.policy
import re

import pytest

from quant_agent.platform.auth import AuthService
from quant_agent.platform.db import Database
from quant_agent.platform.errors import AppError


@pytest.fixture
def auth(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_HOME", str(tmp_path))
    monkeypatch.setenv("QAGENT_MAIL_OUTBOX", "1")
    monkeypatch.delenv("QAGENT_SMTP_HOST", raising=False)
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    db = Database(f"sqlite:///{tmp_path / 'qa.db'}")
    db.migrate()
    return AuthService(db)


def _last_code(tmp_path) -> str:
    raw = sorted((tmp_path / "outbox").glob("*.eml"))[-1].read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text = msg.get_body(preferencelist=("plain",)).get_content()
    return re.search(r"验证码[：:]\s*(\d{6})", text).group(1)


def test_username_only_register_and_login(auth):
    res = auth.register(username="alice_q", password="secret123abc")
    acc = auth.account(res["user_id"])
    assert acc["username"] == "alice_q" and acc["email"] == "" and not acc["email_verified"]
    token, row = auth.login(email="ALICE_Q", password="secret123abc")
    assert token and row["id"] == res["user_id"]
    with pytest.raises(AppError):
        auth.register(username="alice_q", password="secret123abc")


def test_email_register_derives_username(auth):
    a = auth.register(email="bob@x.com", password="secret123abc")
    b = auth.register(email="bob@y.com", password="secret123abc")
    assert auth.account(a["user_id"])["username"] == "bob"
    assert auth.account(b["user_id"])["username"] == "bob2"
    assert auth.login(email="bob2", password="secret123abc")[1]["id"] == b["user_id"]


def test_bind_email_with_code(auth, tmp_path):
    uid = auth.register(username="carol", password="secret123abc")["user_id"]
    out = auth.send_email_code(uid, "Carol@Example.com")
    assert out["sent"] and out["via"] == "outbox"
    with pytest.raises(AppError, match="秒后再试"):
        auth.send_email_code(uid, "carol@example.com")  # 60 秒冷却
    with pytest.raises(AppError, match="不正确"):
        auth.verify_email_code(uid, "carol@example.com", "000000" if _last_code(tmp_path) != "000000" else "111111")
    acc = auth.verify_email_code(uid, "carol@example.com", _last_code(tmp_path))
    assert acc["email"] == "carol@example.com" and acc["email_verified"]
    assert auth.login(email="carol@example.com", password="secret123abc")[1]["id"] == uid
    # 已绑定的邮箱不能再被别人绑定
    other = auth.register(username="dave", password="secret123abc")["user_id"]
    with pytest.raises(AppError, match="已绑定"):
        auth.send_email_code(other, "carol@example.com")


def test_no_mailer_configured_reports_error(auth, monkeypatch):
    monkeypatch.delenv("QAGENT_MAIL_OUTBOX")
    uid = auth.register(username="erin", password="secret123abc")["user_id"]
    with pytest.raises(AppError, match="发信服务"):
        auth.send_email_code(uid, "erin@example.com")
