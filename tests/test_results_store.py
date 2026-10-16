"""T06 运行结果对象存储：落盘路径与工作区隔离、10 万 bar 行大小、分页与原输出逐元素一致、降级格式。

本机**没有安装 pyarrow**（`import pyarrow` 抛 ModuleNotFoundError），所以这里实测的是
`data/cache.py` 同款降级路径：落盘格式是 `jsonl`（列式 JSON Lines），不是真 Parquet。
测试因此刻意**断言实际格式**（`result_ref["format"]`），而不是假设它一定是 parquet——
真 Parquet 写入路径在本机未被验证过。
"""

from __future__ import annotations

import json
import os

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform.results import HAS_PARQUET, result_format

#: T06 验收阈值：runs 表单行必须小于 64KB
ROW_LIMIT = 64 * 1024

#: 10 万根 bar
BARS = 100_000

#: 小规模真实回测（端到端走一遍真实 worker 路径）
SIMPLE = """
PARAMS = {"fast": 6, "slow": 24}

def initialize(ctx):
    run_daily(step, time="close")

def step(ctx):
    s = ctx.securities[0]
    closes = history(s, PARAMS["slow"], "close")
    if len(closes) < PARAMS["slow"]:
        return
    fast = sum(closes[-PARAMS["fast"]:]) / PARAMS["fast"]
    slow = sum(closes) / PARAMS["slow"]
    order_target_percent(s, 0.8 if fast > slow else 0.0)
"""
SPEC = {"source": "synthetic", "bars_limit": 400, "warmup_bars": 30}


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


def _ws(svc) -> str:
    return svc.db.fetchone("SELECT id FROM workspaces LIMIT 1")["id"]


def _store(svc, ws: str):
    """`svc.store` 是按请求上下文路由的工作区存储；测试里显式绑定工作区再取。"""
    return svc.store.for_workspace(ws)


def _completed_run(svc, ws: str, *, kind: str = "backtest", title: str = "十万根 bar 回测") -> str:
    """登记一个 run 并把它推到 COMPLETED（结果落盘由测试自己触发）。"""
    run = svc.runs.create(workspace_id=ws, user_id="u", kind=kind, mode="backtest", title=title)
    claimed = svc.runs.claim("w", kinds=[kind])
    assert claimed and claimed["id"] == run["id"]
    assert svc.runs.set_state(run["id"], "w", "COMPLETED", summary={"status": "done"})
    return run["id"]


def _big_result(run_id: str, bars: int = BARS) -> dict:
    """造一份"十万根 bar"的回测结果：逐 bar 净值、时间戳、回撤、单位净值各 bars 个点。"""
    step = 3_600_000  # 1 小时
    return {
        "id": run_id,
        "run_id": run_id,
        "strategy": "十万根 bar 回测",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "initial_cash": 10_000.0,
        "mode": "backtest",
        "metrics": {"total_return": 0.42, "sharpe": 1.31, "max_drawdown": 0.18, "trades": 137},
        "equity_curve": [10_000.0 + i * 0.5 for i in range(bars)],
        "timestamps": [1_700_000_000_000 + i * step for i in range(bars)],
        "drawdown": [round((i % 500) / 1000.0, 6) for i in range(bars)],
        "unit_nav": [1.0 + i * 1e-6 for i in range(bars)],
        "benchmark_curve": [1.0 + i * 2e-7 for i in range(bars)],
        "fills": [{"fill_id": f"f{i}", "security": "BTC/USDT", "amount": "0.01", "price": "30000"} for i in range(50)],
        "fills_total": 50,
        "ledger": {"balanced": True, "trial_balance": "0"},
    }


def _row_bytes(svc, run_id: str) -> int:
    """把 `runs` 表里这一行序列化后的字节数——真正量一下，不靠感觉。"""
    row = svc.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
    assert row is not None
    return len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. 10 万根 bar：runs 表单行 < 64KB，大数组真的搬出了数据库
# ---------------------------------------------------------------------------
def test_hundred_thousand_bars_keeps_runs_row_under_64kb(client):
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    body = _big_result(run_id)

    # 写入前先证明"这份结果确实是大结果"，否则 < 64KB 的断言没有意义
    body_bytes = len(json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"))
    assert body_bytes > 4 * 1024 * 1024, f"测试数据不够大（{body_bytes} 字节），断言不成立"

    st.put_result("run_results", run_id, body, workspace_id=ws)

    # runs 表这一行：数据库里只留摘要与引用
    size = _row_bytes(svc, run_id)
    assert size < ROW_LIMIT, f"runs 表单行 {size} 字节，超过 64KB 上限"

    # blob 里也只留摘要 + 引用：大数组一个都不在
    raw = st._blob_raw("run_results", run_id)
    blob_bytes = len(json.dumps(raw, ensure_ascii=False, default=str).encode("utf-8"))
    assert blob_bytes < ROW_LIMIT, f"结果 blob {blob_bytes} 字节，大数组没搬出去"
    for field in ("equity_curve", "timestamps", "drawdown", "unit_nav", "benchmark_curve"):
        assert field not in raw, f"{field} 仍然留在 blob 里"

    # 大数组确实在文件里，而且文件明显比 blob 大
    ref = st.result_ref("run_results", run_id)
    assert ref is not None and ref["rows"] == BARS
    # 落盘格式可能是压缩的 parquet（比原始 JSON 小得多），所以和 blob 比，而不是和原始 JSON 比
    assert ref["bytes"] > 10 * blob_bytes


def test_result_file_lands_under_results_workspace_dir(client, tmp_path):
    """落盘位置必须是 <数据目录>/results/<workspace_id>/<run_id>.<格式>。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    st.put_result("run_results", run_id, _big_result(run_id, bars=1000), workspace_id=ws)

    ref = st.result_ref("run_results", run_id)
    path = tmp_path / "results" / ws / f"{run_id}.{ref['format']}"
    assert path.exists(), f"结果文件不在预期位置：{path}"
    assert ref["path"] == str(path)
    # 按工作区分目录：路径里必须有工作区 id
    assert ws in ref["path"]


# ---------------------------------------------------------------------------
# 2. 分页读取与原接口输出逐元素一致（最关键的一条）
# ---------------------------------------------------------------------------
def test_paged_series_equals_original_result_elementwise(client):
    """新分页接口逐页取出的数据拼起来，与改动前一次性返回的 result 完全相等。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    body = _big_result(run_id)
    st.put_result("run_results", run_id, body, workspace_id=ws)

    # 老接口：一次性拿到完整结果（向后兼容）
    got = client.get(f"/api/runs/{run_id}/result")
    assert got.status_code == 200, got.text
    payload = got.json()
    original = payload["result"]

    # 逐元素比对：整个 result 与写入前相等
    assert original == body, "老接口返回的 result 与写入前不一致"

    # 分页接口：每页 20000（端点上限），把所有页拼起来
    for name in ("equity_curve", "timestamps", "drawdown", "unit_nav", "benchmark_curve", "fills"):
        collected: list = []
        offset = 0
        while True:
            page = client.get(f"/api/runs/{run_id}/series", params={"name": name, "offset": offset, "limit": 20000})
            assert page.status_code == 200, page.text
            data = page.json()
            collected.extend(data["items"])
            if data["next_offset"] is None:
                assert data["total"] == len(original[name])
                break
            offset = data["next_offset"]
        # 逐元素相等——不是"长度一样"或"抽样一样"
        assert collected == original[name], f"{name} 分页拼接结果与原输出不一致"
        assert len(collected) == len(original[name])


def test_series_without_name_lists_available_arrays(client):
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    st.put_result("run_results", run_id, _big_result(run_id, bars=500), workspace_id=ws)

    listing = client.get(f"/api/runs/{run_id}/series").json()
    assert "equity_curve" in listing["series"] and "fills" in listing["series"]
    assert listing["format"] == result_format()


def test_series_rejects_unknown_name_and_bad_limit(client):
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    st.put_result("run_results", run_id, _big_result(run_id, bars=500), workspace_id=ws)

    bad = client.get(f"/api/runs/{run_id}/series", params={"name": "不存在的数组"})
    assert bad.status_code == 400 and bad.json()["code"] == "INVALID_PARAMETER"
    assert "equity_curve" in bad.json()["details"]["available"]  # 告诉调用方有哪些可读

    too_big = client.get(f"/api/runs/{run_id}/series", params={"name": "equity_curve", "limit": 20001})
    assert too_big.status_code == 422


def test_series_on_run_without_landed_result(client):
    """没有落盘大数据的 run（例如升级前的旧运行）：明确报错，不返回半截数据。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    st.put_blob("run_results", run_id, {"metrics": {"sharpe": 1.0}, "equity_curve": [1, 2, 3]})

    r = client.get(f"/api/runs/{run_id}/series", params={"name": "equity_curve"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    # 老结果仍然能通过老接口原样读到（不因为新机制而丢失）
    assert client.get(f"/api/runs/{run_id}/result").json()["result"]["equity_curve"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# 3. 跨工作区不可见
# ---------------------------------------------------------------------------
def test_results_are_workspace_isolated(app, monkeypatch, tmp_path):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(app) as a, TestClient(app) as b:
        login(a, email="a@example.com", password="Alice123!")
        login(b, email="b@example.com", password="Bobby123!")

        svc = get_services()
        ws_a = a.get("/api/auth/me").json()["workspace"]["id"]
        ws_b = b.get("/api/auth/me").json()["workspace"]["id"]
        st_a = _store(svc, ws_a)
        run_id = _completed_run(svc, ws_a)
        st_a.put_result("run_results", run_id, _big_result(run_id, bars=500), workspace_id=ws_a)

        # 本工作区读得到
        assert a.get(f"/api/runs/{run_id}/result").status_code == 200
        assert a.get(f"/api/runs/{run_id}/series", params={"name": "equity_curve"}).status_code == 200

        # 另一个工作区：结果与分页端点都不可见
        assert b.get(f"/api/runs/{run_id}/result").status_code == 404
        assert b.get(f"/api/runs/{run_id}/series", params={"name": "equity_curve"}).status_code == 404

        # 文件系统层面也隔离：A 的结果落在 <数据目录>/results/<A>/，B 的目录下没有它。
        # 注意 results 根目录是共享的，隔离靠的是**每个工作区一个子目录**。
        ref_a = st_a.result_ref("run_results", run_id)
        assert f"results{os.sep}{ws_a}{os.sep}" in ref_a["path"]
        assert ws_b not in ref_a["path"]
        for candidate in (tmp_path / "results" / ws_b).glob(f"{run_id}.*"):
            raise AssertionError(f"B 工作区目录里出现了 A 的结果文件：{candidate}")


def test_cross_workspace_cannot_read_landed_file(client):
    """即使知道 run_id，换个工作区也读不到落盘文件（按工作区分目录）。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    st.put_result("run_results", run_id, _big_result(run_id, bars=200), workspace_id=ws)

    results = st._results()
    assert results.read(ws, run_id) is not None
    assert results.read("另一个工作区", run_id) is None


# ---------------------------------------------------------------------------
# 4. 降级格式可用，且格式如实记录
# ---------------------------------------------------------------------------
def test_fallback_format_is_used_and_reported(client):
    """本机没有 pyarrow：必须落到降级格式，并在引用里如实写明。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    body = _big_result(run_id, bars=1000)
    st.put_result("run_results", run_id, body, workspace_id=ws)

    ref = st.result_ref("run_results", run_id)
    if HAS_PARQUET:
        assert ref["format"] == "parquet"
    else:
        # 没有 pyarrow 就不能声称写了 Parquet
        assert ref["format"] == "jsonl" != "parquet"
        assert ref["path"].endswith(".jsonl")

    # 降级格式功能等价：数据能完整读回
    assert st.get_result("run_results", run_id) == body
    # 接口也把实际格式告诉调用方
    assert client.get(f"/api/runs/{run_id}/result").json()["result_ref"]["format"] == ref["format"]


def test_roundtrip_is_elementwise_exact_for_all_series(client):
    """摘要 + 大数组读回来必须逐元素等于写入前（含嵌套结构与非等长数组）。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    body = {
        "metrics": {"sharpe": 1.5},
        "equity_curve": [1.0, 2.0, 3.5],
        "timestamps": [10, 20, 30],
        "fills": [{"a": 1}, {"b": 2}],  # 故意比 equity_curve 短：列式对齐不能多补 None
        "fills_total": 2,
        "notes": ["数据来源：合成"],
        "open_position": None,
    }
    st.put_result("run_results", run_id, body, workspace_id=ws)
    assert st.get_result("run_results", run_id) == body
    assert client.get(f"/api/runs/{run_id}/result").json()["result"] == body


def test_checksum_and_rows_are_recorded(client):
    """引用里要有行数与校验和，便于发现落盘文件被改动/截断。"""
    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    run_id = _completed_run(svc, ws)
    st.put_result("run_results", run_id, _big_result(run_id, bars=300), workspace_id=ws)

    ref = st.result_ref("run_results", run_id)
    assert ref["rows"] == 300
    assert len(ref["checksum"]) == 64  # sha256 十六进制
    assert ref["series"]["equity_curve"]["rows"] == 300
    assert ref["bytes"] > 0


# ---------------------------------------------------------------------------
# 5. 真实回测端到端：走一遍 worker 的落盘路径
# ---------------------------------------------------------------------------
def test_real_backtest_result_is_landed_and_pageable(client):
    """真实回测（小规模）也要走落盘路径，并且老接口输出不变。"""
    s = client.post("/api/research/strategies", json={"name": "落盘回测", "code": SIMPLE}).json()["strategy"]
    r = client.post("/api/research/backtests", json={"slug": s["slug"], **SPEC})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    result = r.json()["result"]

    svc = get_services()
    st = _store(svc, _ws(svc))
    ref = st.result_ref("run_results", run_id)
    assert ref is not None, "真实回测的结果没有落盘"
    assert ref["format"] == result_format()

    # blob 里不再有大数组
    raw = st._blob_raw("run_results", run_id)
    assert "equity_curve" not in raw and "timestamps" not in raw

    # 老接口（/result 与 research 接口）输出与落盘前一致
    assert client.get(f"/api/runs/{run_id}/result").json()["result"] == result

    # 分页读净值曲线，拼起来与 result 里的逐元素相等
    collected: list = []
    offset = 0
    while True:
        page = client.get(
            f"/api/runs/{run_id}/series", params={"name": "equity_curve", "offset": offset, "limit": 100}
        ).json()
        collected.extend(page["items"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert collected == result["equity_curve"]


def test_template_backtest_nested_result_is_landed(client):
    """模板回测的大数组嵌在 result["run"] 下，同样要落盘且形状不变。"""
    from quant_agent.api import tasks
    from quant_agent.platform import context
    from quant_agent.platform.worker import TaskCtl

    svc = get_services()
    ws = _ws(svc)
    st = _store(svc, ws)
    payload = {
        "strategy": "ma_trend",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "initial_cash": 10_000.0,
        "bars_limit": 300,
        "exchange": "binance",
    }
    run = svc.runs.create(
        workspace_id=ws,
        user_id="u",
        kind="template_backtest",
        mode="backtest",
        manifest={"payload": payload},
    )
    claimed = svc.runs.claim("w", kinds=["template_backtest"])
    with context.bind(ws):
        tasks.handle_template_backtest(TaskCtl(run=claimed, runs=svc.runs, owner="w", services=svc))
    svc.runs.set_state(run["id"], "w", "COMPLETED", summary={"status": "done"})

    ref = st.result_ref("run_results", run["id"])
    assert ref is not None and ref.get("nested") == "run"

    body = client.get(f"/api/runs/{run['id']}/result").json()["result"]
    # 形状不变：run_id 在外层，大数组嵌在 run 下
    assert "run_id" in body and "equity_curve" in body["run"]
    assert len(body["run"]["equity_curve"]) == len(body["run"]["timestamps"])

    page = client.get(f"/api/runs/{run['id']}/series", params={"name": "equity_curve", "limit": 100}).json()
    assert page["items"] == body["run"]["equity_curve"][:100]


# ---------------------------------------------------------------------------
# 6. S3 兼容后端：只有接口，未实测（不假装测过）
# ---------------------------------------------------------------------------
def test_s3_backend_is_configurable_but_unverified(tmp_path, monkeypatch):
    """S3 后端只保证接口形状与可配置性；本机没有 boto3 / 端点，未做真实读写验证。"""
    from quant_agent.platform.results import LocalResultBackend, S3ResultBackend, build_backend

    # 默认是本地文件系统
    assert isinstance(build_backend(tmp_path), LocalResultBackend)

    # 配置成 s3 就切到 S3 后端（只构造，不发请求）
    monkeypatch.setenv("QAGENT_RESULTS_BACKEND", "s3")
    monkeypatch.setenv("QAGENT_RESULTS_S3_BUCKET", "my-bucket")
    backend = build_backend(tmp_path)
    assert isinstance(backend, S3ResultBackend) and backend.bucket == "my-bucket"
    # 明确标记未实现，而不是静默返回错误数据
    with pytest.raises(NotImplementedError):
        backend.write("k", {}, [])
