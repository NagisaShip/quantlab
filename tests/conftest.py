"""测试公共设施：每个测试进程使用独立的数据目录与加密主密钥；客户端以已登录的管理员身份发请求。"""

from __future__ import annotations

import base64
import os

import pytest

# 加密主密钥：测试固定一把，避免在用户主目录生成密钥文件
os.environ.setdefault("QAGENT_SECRET_KEY", base64.urlsafe_b64encode(b"q" * 32).decode())
os.environ.setdefault("QAGENT_BACKGROUND", "0")
# 测试用 inline worker：在请求线程里走与独立 worker 完全相同的认领 / 执行 / 检查点代码
os.environ.setdefault("QAGENT_WORKER", "inline")
os.environ.setdefault("QAGENT_PAPER_TICK_SECONDS", "0")
# 测试里不连交易所 WebSocket：实时模拟一律走 REST（被测试替换的）行情
os.environ.setdefault("QAGENT_STREAM", "0")
# 平台默认停用合成行情；测试离线运行，需要可复现的合成数据
os.environ.setdefault("QAGENT_ALLOW_SYNTHETIC", "1")
# 联网搜索的免密钥来源（必应 / 百度 / 新闻）在测试里关闭，不访问外网
os.environ.setdefault("QAGENT_FREE_SEARCH", "0")
# 默认资产由专门的种子测试显式开启，避免所有既有用例的试验计数被默认数据污染
os.environ.setdefault("QAGENT_SEED_BUILTINS", "0")

# 在 PostgreSQL 上跑全套测试：QAGENT_TEST_PG=postgresql://user@host:port/db
# 每个 AppServices.build() 使用一个全新 schema，测试之间互不影响。
_PG = os.environ.get("QAGENT_TEST_PG", "").strip()
if _PG:
    import uuid

    import psycopg

    from quant_agent.api import deps as _deps

    _SCHEMAS: dict[str, str] = {}

    def _pg_url(home):
        # 同一个数据目录（模拟重启）复用同一个 schema
        if str(home) in _SCHEMAS:
            return _SCHEMAS[str(home)]
        schema = "t_" + uuid.uuid4().hex[:12]
        with psycopg.connect(_PG, autocommit=True) as conn:
            conn.execute(f"CREATE SCHEMA {schema}")
        sep = "&" if "?" in _PG else "?"
        _SCHEMAS[str(home)] = f"{_PG}{sep}options=-csearch_path%3D{schema}"
        return _SCHEMAS[str(home)]

    _deps.default_url = _pg_url

ADMIN = {"email": "admin@example.com", "password": "Passw0rd!", "name": "管理员"}


def login(
    client,
    *,
    email: str = ADMIN["email"],
    password: str = ADMIN["password"],
    name: str = ADMIN["name"],
    invite_code: str | None = None,
):
    """注册（首个用户成为管理员）或登录，并加上写请求必需的 X-Requested-With 头。"""
    client.headers.update({"X-Requested-With": "pytest"})
    r = client.post(
        "/api/auth/register", json={"email": email, "password": password, "name": name, "invite_code": invite_code}
    )
    if r.status_code != 200:
        r = client.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
    me = client.get("/api/auth/me").json()
    client.headers.update({"X-Workspace-Id": me["workspace"]["id"]})
    return me


@pytest.fixture()
def anyio_backend():
    return "asyncio"
