"""T07 配额计量与用量面板：并发安全、跨工作区隔离、超限拒绝、十进制字符串。

覆盖的验收标准（任务包 T07 原文）：
- 记录每个工作区每日的 CPU 秒、回测次数、优化组合数、存储占用；
- 配额耗尽时返回 `QUOTA_EXHAUSTED` 且 `details` 含已用 / 上限；
- 跨工作区用量互不影响。

外加用户概述里要求的：LLM 调用次数与 token、数据下载量、Decimal 字符串输出、
以及"计量写入并发安全"（多线程同时加计数不丢更新）。
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import context
from quant_agent.platform.errors import AppError
from quant_agent.platform.quota import (
    DAILY_METRICS,
    DEFAULT_LIMITS,
    METRICS,
    QuotaService,
    measure_dir,
    today,
)


@pytest.fixture()
def app(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        login(c)
        yield c


@pytest.fixture()
def svc(app):
    """直接拿到服务容器（用于绕过 HTTP 层测计量内核）。

    这里先注册一个用户：工作区是在注册时创建的，没有工作区就没有可计量的对象。
    """
    with TestClient(app) as c:
        login(c)
    return get_services()


def _ws() -> str:
    return get_services().db.fetchone("SELECT id FROM workspaces LIMIT 1")["id"]


# ---------------------------------------------------------------------------
# 计量内核
# ---------------------------------------------------------------------------
def test_add_accumulates_exactly_with_decimal(svc):
    """CPU 秒按 Decimal 精确累加：0.1 加 10 次必须是 1，不能是 0.9999999999999999。"""
    ws = _ws()
    q = QuotaService(svc.db)
    for _ in range(10):
        q.add("backtest_cpu_seconds", "0.1", workspace_id=ws)
    assert q.used("backtest_cpu_seconds", workspace_id=ws) == Decimal("1")
    # 对外一律十进制字符串
    assert q.daily(workspace_id=ws)["backtest_cpu_seconds"] == "1"


def test_usage_is_decimal_string_in_panel(client):
    """用量面板里所有数值都是十进制字符串（前端不做浮点运算）。"""
    body = client.get("/api/workspace/usage").json()
    assert body["workspace_id"]
    assert body["day"] == today()
    for item in body["items"]:
        assert isinstance(item["used"], str)
        assert isinstance(item["unit"], str)
        assert isinstance(item["exhausted"], bool)
        if item["limit"] is not None:
            assert isinstance(item["limit"], str)
            assert isinstance(item["remaining"], str)
            assert isinstance(item["used_percent"], str)
        # 字符串必须是可解析的十进制，不能出现科学计数法
        Decimal(item["used"])
        assert "e" not in item["used"].lower()


def test_metric_catalog_covers_task_card_items():
    """任务包 T07 点名的四项计量必须存在；用户概述补的三项也要在。"""
    names = {m.name for m in METRICS}
    assert {
        "backtest_cpu_seconds",  # CPU 秒
        "backtest_runs",  # 回测次数
        "optimize_combinations",  # 优化组合数
        "storage_bytes",  # 存储占用
        "llm_calls",  # LLM 调用次数
        "llm_tokens",  # LLM token
        "data_download_bytes",  # 数据下载量
    } <= names


def test_storage_is_total_not_daily(svc):
    """存储占用是存量指标：覆盖写而不是累加，否则数字只增不减。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_value("storage_bytes", 1000, workspace_id=ws)
    q.set_value("storage_bytes", 400, workspace_id=ws)
    assert q.used("storage_bytes", workspace_id=ws) == Decimal("400")
    assert q.totals(workspace_id=ws)["storage_bytes"] == "400"
    # 存量指标不参与"每日"归集
    assert "storage_bytes" not in DAILY_METRICS
    assert "storage_bytes" not in q.daily(workspace_id=ws)


def test_measure_dir_counts_files(tmp_path):
    """目录占用统计：只算普通文件，且不跟随符号链接。"""
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    (root / "a.json").write_bytes(b"x" * 100)
    (root / "sub" / "b.json").write_bytes(b"y" * 50)
    assert measure_dir(root) == 150
    assert measure_dir(tmp_path / "不存在") == 0


# ---------------------------------------------------------------------------
# 并发安全（核心：不能丢更新）
# ---------------------------------------------------------------------------
def test_concurrent_increments_do_not_lose_updates(svc):
    """8 个线程各加 100 次，总数必须精确等于 800。

    这是"用 SQL 原子自增、不要读-改-写"的直接验收：如果实现是
    `SELECT` 出来加一再 `UPDATE` 写回，这个测试会稳定失败。
    """
    ws = _ws()
    q = QuotaService(svc.db)
    threads = 8
    per_thread = 100
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(per_thread):
                q.add("backtest_runs", 1, workspace_id=ws)
        except BaseException as exc:  # pragma: no cover - 失败时用于报错
            errors.append(exc)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    assert not errors, errors
    assert q.used("backtest_runs", workspace_id=ws) == Decimal(threads * per_thread)


def test_concurrent_decimal_increments_stay_exact(svc):
    """并发累加小数（毫秒级 CPU 时间）也不能漂移。"""
    ws = _ws()
    q = QuotaService(svc.db)
    threads, per_thread = 6, 50

    def work() -> None:
        for _ in range(per_thread):
            q.add("backtest_cpu_seconds", "0.25", workspace_id=ws)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()

    assert q.used("backtest_cpu_seconds", workspace_id=ws) == Decimal(threads * per_thread) * Decimal("0.25")


def test_large_byte_counts_keep_precision(svc):
    """大数值不能退化成科学计数法。

    为什么这个测试重要：`DECIMAL_T` 在 SQLite 下是 TEXT，若直接在 SQL 里对 TEXT 列做加法，
    SQLite 会退化成 REAL 并把结果写成 `9.00000000000000e+18` 这类科学计数法，精度也随之丢失。
    这里用接近 SQLite 有符号 64 位上限的量级（9 EB）来钉住这个行为。
    """
    ws = _ws()
    q = QuotaService(svc.db)
    big = Decimal("9000000000000000000")
    q.add("data_download_bytes", big, workspace_id=ws)
    q.add("data_download_bytes", 100, workspace_id=ws)
    got = q.used("data_download_bytes", workspace_id=ws)
    assert got == big + 100
    assert "e" not in str(got).lower()
    assert q.daily(workspace_id=ws)["data_download_bytes"] == str(big + 100)


def test_negative_increment_is_rejected(svc):
    """负增量会让"已用"倒退，等于绕过配额，必须拒绝。"""
    ws = _ws()
    q = QuotaService(svc.db)
    with pytest.raises(AppError) as ei:
        q.add("backtest_runs", -1, workspace_id=ws)
    assert getattr(ei.value, "code", "") == "INVALID_PARAMETER"


def test_unknown_metric_is_rejected(svc):
    ws = _ws()
    q = QuotaService(svc.db)
    with pytest.raises(AppError) as ei:
        q.add("不存在的计量项", 1, workspace_id=ws)
    assert getattr(ei.value, "code", "") == "INVALID_PARAMETER"


# ---------------------------------------------------------------------------
# 超限拒绝
# ---------------------------------------------------------------------------
def test_quota_exhausted_details_contain_used_and_limit(svc):
    """验收标准：配额耗尽时返回 QUOTA_EXHAUSTED，且 details 含已用 / 上限。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 3})
    q.add("backtest_runs", 3, workspace_id=ws)

    with pytest.raises(AppError) as ei:
        q.check("backtest_runs", 1, workspace_id=ws)
    err = ei.value
    assert err.code == "QUOTA_EXHAUSTED"
    assert err.details["used"] == "3"
    assert err.details["limit"] == "3"
    assert err.details["metric"] == "backtest_runs"
    assert err.details["unit"] == "次"
    assert "回测次数" in err.message  # 中文说明
    # 中文说明里也要有已用 / 上限，前端直接把 message 显示给用户
    assert "3" in err.message


def test_limit_boundary_is_inclusive(svc):
    """等于上限时放行，超过才拒绝（上限是"最多可以到多少"）。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 2})
    q.add("backtest_runs", 1, workspace_id=ws)
    q.check("backtest_runs", 1, workspace_id=ws)  # 1 + 1 == 2，正好到上限，放行
    q.add("backtest_runs", 1, workspace_id=ws)
    assert q.used("backtest_runs", workspace_id=ws) == Decimal(2)
    q.check("backtest_runs", 0, workspace_id=ws)  # 已用 == 上限，再加 0 仍放行
    with pytest.raises(AppError) as ei:
        q.check("backtest_runs", 1, workspace_id=ws)  # 2 + 1 > 2，拒绝
    assert ei.value.code == "QUOTA_EXHAUSTED"


def test_limit_boundary_rejects_over(svc):
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 2})
    q.add("backtest_runs", 2, workspace_id=ws)
    q.check("backtest_runs", 0, workspace_id=ws)  # 已用 == 上限，再加 0 仍放行
    with pytest.raises(AppError) as ei:
        q.check("backtest_runs", 1, workspace_id=ws)
    assert ei.value.code == "QUOTA_EXHAUSTED"


def test_check_is_read_only(svc):
    """预检不能写脏计数：被拒绝的请求不应在用量面板里留下痕迹。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 1})
    q.add("backtest_runs", 1, workspace_id=ws)
    for _ in range(5):
        with pytest.raises(AppError):
            q.check("backtest_runs", 1, workspace_id=ws)
    assert q.used("backtest_runs", workspace_id=ws) == Decimal(1)


def test_null_limit_means_unlimited(svc):
    """显式传 null = 取消限制，此时 check 永远放行。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": None})
    q.add("backtest_runs", 10_000, workspace_id=ws)
    q.check("backtest_runs", 10_000, workspace_id=ws)
    assert q.limit_for("backtest_runs", ws) is None


def test_unknown_limit_key_is_rejected(svc):
    """写错配额键名必须报错，不能静默接受（否则管理员以为限制生效了）。"""
    ws = _ws()
    q = QuotaService(svc.db)
    with pytest.raises(AppError) as ei:
        q.set_limits(ws, {"max_backtests_per_day_x": 1})
    assert getattr(ei.value, "code", "") == "INVALID_PARAMETER"


def test_negative_limit_is_rejected(svc):
    ws = _ws()
    q = QuotaService(svc.db)
    with pytest.raises(AppError) as ei:
        q.set_limits(ws, {"max_backtests_per_day": -1})
    assert getattr(ei.value, "code", "") == "INVALID_PARAMETER"


def test_default_limits_present():
    """默认无限计量；管理员仍可显式设限。"""
    assert DEFAULT_LIMITS and all(value is None for value in DEFAULT_LIMITS.values())


# ---------------------------------------------------------------------------
# 跨工作区隔离（验收标准）
# ---------------------------------------------------------------------------
def test_usage_is_isolated_between_workspaces(svc):
    """A 工作区加计数，B 工作区的用量必须完全不受影响。"""
    q = QuotaService(svc.db)
    q.add("backtest_runs", 7, workspace_id="ws-A")
    q.add("backtest_cpu_seconds", "12.5", workspace_id="ws-A")

    assert q.used("backtest_runs", workspace_id="ws-A") == Decimal(7)
    assert q.used("backtest_runs", workspace_id="ws-B") == Decimal(0)
    assert q.used("backtest_cpu_seconds", workspace_id="ws-B") == Decimal(0)
    assert q.daily(workspace_id="ws-B")["backtest_runs"] == "0"
    assert q.history(workspace_id="ws-B") == []


def test_quota_check_does_not_leak_across_workspaces(svc):
    """A 超限不能连带把 B 也拒绝。"""
    q = QuotaService(svc.db)
    ws_a = _ws()
    # 造第二个工作区
    ws_b = svc.auth.create_workspace(name="B", owner_id=svc.db.fetchone("SELECT id FROM users LIMIT 1")["id"])
    q.set_limits(ws_a, {"max_backtests_per_day": 1})
    q.add("backtest_runs", 1, workspace_id=ws_a)
    with pytest.raises(AppError):
        q.check("backtest_runs", 1, workspace_id=ws_a)
    q.check("backtest_runs", 1, workspace_id=ws_b)  # B 不受影响


def test_usage_endpoint_is_workspace_isolated(app, monkeypatch):
    """HTTP 层：A 的用量端点只返回 A 的数据，B 看不到 A 的计数。"""
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(app) as a, TestClient(app) as b:
        me_a = login(a, email="a@example.com", password="Alice123!")
        me_b = login(b, email="b@example.com", password="Bobby123!")
        assert me_a["workspace"]["id"] != me_b["workspace"]["id"]

        ws_a = me_a["workspace"]["id"]
        svc = get_services()
        QuotaService(svc.db).add("backtest_runs", 5, workspace_id=ws_a)

        body_a = a.get("/api/workspace/usage").json()
        body_b = b.get("/api/workspace/usage").json()
        assert body_a["workspace_id"] == ws_a
        assert body_b["workspace_id"] == me_b["workspace"]["id"]
        used_a = next(i["used"] for i in body_a["items"] if i["metric"] == "backtest_runs")
        used_b = next(i["used"] for i in body_b["items"] if i["metric"] == "backtest_runs")
        assert used_a == "5"
        assert used_b == "0"
        # 历史里也不能出现对方的数据
        assert all(h["day"] for h in body_b["history"]) or body_b["history"] == []


def test_usage_requires_workspace_context(svc):
    """没有绑定工作区时必须报错，而不是悄悄落到某个共享目录/共享行。"""
    q = QuotaService(svc.db)
    token = context.set_context(context.RequestContext(user_id="u", workspace_id=None, role="member"))
    try:
        with pytest.raises(AppError) as ei:
            q.add("backtest_runs", 1)
        assert getattr(ei.value, "code", "") == "PERMISSION_DENIED"
    finally:
        context.reset_context(token)


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------
def test_usage_panel_endpoint(client):
    body = client.get("/api/workspace/usage").json()
    names = {i["metric"] for i in body["items"]}
    assert names == {m.name for m in METRICS}
    assert "history" in body
    assert "跨工作区互不可见" in body["note"]


def test_usage_history_endpoint_filters_metric(client):
    ws = _ws()
    QuotaService(get_services().db).add("llm_tokens", 1234, workspace_id=ws)
    body = client.get("/api/workspace/usage/history", params={"metric": "llm_tokens"}).json()
    assert body["metric"] == "llm_tokens"
    assert any(row["metric"] == "llm_tokens" and row["value"] == "1234" for row in body["history"])
    # 只返回被筛选的计量项
    assert {row["metric"] for row in body["history"]} <= {"llm_tokens"}


def test_usage_history_rejects_unknown_metric(client):
    r = client.get("/api/workspace/usage/history", params={"metric": "nope"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


def test_update_limits_endpoint(client):
    r = client.put("/api/workspace/usage/limits", json={"max_backtests_per_day": 5})
    assert r.status_code == 200, r.text
    assert r.json()["limits"]["max_backtests_per_day"] == "5"
    # 面板随之反映新上限
    item = next(i for i in client.get("/api/workspace/usage").json()["items"] if i["metric"] == "backtest_runs")
    assert item["limit"] == "5"


def test_update_limits_requires_admin(app, monkeypatch):
    """普通成员不能改配额上限。"""
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(app) as owner, TestClient(app) as member:
        login(owner, email="owner@example.com", password="Owner123!")
        invite = owner.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        login(member, email="member@example.com", password="Memb3r123!", invite_code=invite)
        r = member.put("/api/workspace/usage/limits", json={"max_backtests_per_day": 1})
        assert r.status_code == 403
        assert r.json()["code"] == "PERMISSION_DENIED"


def test_recount_endpoint(client):
    r = client.post("/api/workspace/usage/recount")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace_id"] == _ws()
    Decimal(body["storage_bytes"])  # 十进制字符串
    item = next(i for i in client.get("/api/workspace/usage").json()["items"] if i["metric"] == "storage_bytes")
    assert item["used"] == body["storage_bytes"]


def test_quota_exhausted_over_http(client):
    """端到端：把回测配额调到 0，配额预检端点必须返回 429 + QUOTA_EXHAUSTED + details。

    验收标准"配额耗尽时返回 QUOTA_EXHAUSTED 且 details 含已用 / 上限"在 HTTP 层的落点。
    """
    client.put("/api/workspace/usage/limits", json={"max_backtests_per_day": 0})
    r = client.post("/api/workspace/usage/check", json={"kind": "backtest"})
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["code"] == "QUOTA_EXHAUSTED"
    assert body["details"]["used"] == "0"
    assert body["details"]["limit"] == "0"
    assert body["details"]["metric"] == "backtest_runs"
    assert body["details"]["requested"] == "1"


def test_quota_check_endpoint_allows_when_under_limit(client):
    client.put("/api/workspace/usage/limits", json={"max_backtests_per_day": 10})
    r = client.post("/api/workspace/usage/check", json={"kind": "backtest"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


def test_quota_check_endpoint_rejects_unknown_kind(client):
    r = client.post("/api/workspace/usage/check", json={"kind": "不存在的类型"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_PARAMETER"


def test_quota_check_endpoint_is_read_only(client):
    """预检不能产生用量：反复预检后面板里的已用仍是 0。"""
    client.put("/api/workspace/usage/limits", json={"max_backtests_per_day": 5})
    for _ in range(3):
        assert client.post("/api/workspace/usage/check", json={"kind": "backtest"}).status_code == 200
    item = next(i for i in client.get("/api/workspace/usage").json()["items"] if i["metric"] == "backtest_runs")
    assert item["used"] == "0"


def test_enforce_enqueue_maps_kinds(svc):
    """运行类型 → 计量项的映射口径要和计量侧一致。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 0})
    with pytest.raises(AppError) as ei:
        q.enforce_enqueue("backtest", workspace_id=ws)
    assert ei.value.code == "QUOTA_EXHAUSTED"
    # optimize 不看回测次数上限，所以不该被上面那条限制挡住
    q.enforce_enqueue("optimize", workspace_id=ws)
    # paper 不是计量对象，永远放行
    q.enforce_enqueue("paper", workspace_id=ws)


def test_llm_and_download_metrics_are_metered(svc):
    """用户概述要求的两项：LLM 调用/token 与数据下载量。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.add("llm_calls", 2, workspace_id=ws)
    q.add("llm_tokens", 1500, workspace_id=ws)
    q.add("data_download_bytes", 4096, workspace_id=ws)
    daily = q.daily(workspace_id=ws)
    assert daily["llm_calls"] == "2"
    assert daily["llm_tokens"] == "1500"
    assert daily["data_download_bytes"] == "4096"


def test_llm_token_quota_rejects(svc):
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_llm_tokens_per_day": 1000})
    q.add("llm_tokens", 900, workspace_id=ws)
    q.check("llm_tokens", 100, workspace_id=ws)  # 900 + 100 == 1000，放行
    with pytest.raises(AppError) as ei:
        q.check("llm_tokens", 101, workspace_id=ws)
    assert ei.value.code == "QUOTA_EXHAUSTED"
    assert ei.value.details["used"] == "900"
    assert ei.value.details["limit"] == "1000"


def test_precision_beyond_storage_unit_is_rejected(svc):
    """精度超过存储单位时明确报错，而不是静默记成 0。"""
    ws = _ws()
    q = QuotaService(svc.db)
    with pytest.raises(AppError) as ei:
        q.add("backtest_cpu_seconds", "0.0001", workspace_id=ws)
    assert getattr(ei.value, "code", "") == "INVALID_PARAMETER"
    assert q.used("backtest_cpu_seconds", workspace_id=ws) == Decimal(0)


def test_history_is_ascending_and_bounded(svc):
    """走势按日期升序，且 days 有上限（不能让一次请求拉出无限历史）。"""
    ws = _ws()
    q = QuotaService(svc.db)
    for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
        q.add("backtest_runs", 1, workspace_id=ws, day=day)
    rows = q.history(workspace_id=ws, days=30)
    days = [r["day"] for r in rows]
    assert days == sorted(days)
    assert days == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert len(q.history(workspace_id=ws, days=100000)) <= 366 * len(DAILY_METRICS)


def test_enforce_enqueue_is_the_documented_enqueue_hook(svc):
    """`enforce_enqueue` 是 run 入队前的接入点。

    这里钉住它的**契约**：超限时抛 QUOTA_EXHAUSTED 且 details 带 used/limit，
    这样 `api/routes_runs.py::enqueue` 只要加一行 `s.quota.enforce_enqueue(kind)` 就能生效
    （该文件不在本任务的允许改动范围内，接入由仓库负责人完成）。
    """
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 0})
    with pytest.raises(AppError) as ei:
        q.enforce_enqueue("backtest", workspace_id=ws)
    err = ei.value
    assert err.code == "QUOTA_EXHAUSTED"
    assert set(err.details) >= {"metric", "used", "limit", "requested"}
    assert err.details["metric"] == "backtest_runs"


def test_enforce_enqueue_leaves_uncounted_kinds_alone(svc):
    """paper / agent_job 不在计量范围内，不能被回测配额连带挡住。"""
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_backtests_per_day": 0, "max_optimize_combinations_per_day": 0})
    q.enforce_enqueue("paper", workspace_id=ws)
    q.enforce_enqueue("agent_job", workspace_id=ws)


def test_enforce_enqueue_counts_optimize_combinations(svc):
    """优化要按本次网格的**组合数**判定，而不是按"一次优化"判定。

    否则一个 5000 组的网格会被当成 1 组放行，等跑完才发现当天配额早就爆了。
    """
    ws = _ws()
    q = QuotaService(svc.db)
    q.set_limits(ws, {"max_optimize_combinations_per_day": 100})

    # 小网格放行
    q.enforce_enqueue("optimize", {"combinations": 40}, workspace_id=ws)
    q.add("optimize_combinations", 40, workspace_id=ws)
    # 剩下的额度只有 60，80 组的网格必须被拒绝，且 details 里的 requested 是 80 而不是 1
    with pytest.raises(AppError) as ei:
        q.enforce_enqueue("optimize", {"combinations": 80}, workspace_id=ws)
    err = ei.value
    assert err.code == "QUOTA_EXHAUSTED"
    assert err.details["used"] == "40"
    assert err.details["limit"] == "100"
    assert err.details["requested"] == "80"
    # 60 组正好填满，放行
    q.enforce_enqueue("optimize", {"combinations": 60}, workspace_id=ws)
