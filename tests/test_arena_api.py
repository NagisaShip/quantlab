"""统一赛道 / 规则策略 / CSV 数据集的 API 测试。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.data import sample_csv


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("arena-store")
    services = AppServices.build(store_root=str(root))
    set_services(services)
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


# ---------------- 赛道 ----------------
def test_track_defaults_and_validation(client):
    r = client.get("/api/arena/track/defaults")
    assert r.status_code == 200
    track = r.json()["track"]
    assert track["market"] == "spot" and track["leverage"] == 1.0 and track["direction"] == "long_only"
    assert track["warmup_bars"] == 100

    r = client.post("/api/arena/track/validate", json={"overrides": {"cost": {"taker_fee": 0.002}}})
    assert r.status_code == 200
    assert r.json()["track"]["cost"]["taker_fee"] == 0.002
    assert len(r.json()["digest"]) == 12

    r = client.post("/api/arena/track/validate", json={"overrides": {"leverage": 5}})
    assert r.status_code == 400
    assert "杠杆" in r.json()["message"]

    r = client.post("/api/arena/track/validate", json={"overrides": {"nope": 1}})
    assert r.status_code == 400


# ---------------- 规则策略 ----------------
def test_rules_meta_and_validation(client):
    r = client.get("/api/arena/rules/meta")
    meta = r.json()
    assert {i["name"] for i in meta["indicators"]} == {"close", "sma_fast", "sma_slow", "rsi", "position"}
    assert "不执行任意 Python 代码" in " ".join(meta["constraints"])
    assert meta["template"]["buy"]

    r = client.post("/api/arena/rules/validate", json={"buy": "close > 100"})
    assert r.status_code == 200
    assert len(r.json()["spec"]["version"]) == 12

    # 任意代码必须被拒
    r = client.post("/api/arena/rules/validate", json={"buy": "__import__('os').system('ls')"})
    assert r.status_code == 400
    assert "函数" in r.json()["message"] or "不支持的语法" in r.json()["message"]

    r = client.post("/api/arena/rules/validate", json={"fast": 48, "slow": 12})
    assert r.status_code == 400 and "必须小于" in r.json()["message"]


def test_rule_save_and_list(client):
    r = client.post("/api/arena/rules/save", json={"name": "我的规则", "team": "测试组"})
    assert r.status_code == 200
    saved = r.json()["strategy"]
    assert saved["kind"] == "rule" and saved["version"]
    r = client.get("/api/arena/rules")
    assert any(x["name"] == "我的规则" for x in r.json()["rules"])
    client.delete(f"/api/strategies/{saved['id']}")


# ---------------- CSV 数据集 ----------------
def test_dataset_upload_validation_and_listing(client):
    csv_text = sample_csv("BTC/USDT", "1h", 300)
    r = client.post(
        "/api/arena/datasets/upload",
        json={"content": csv_text, "filename": "my.csv", "symbol": "BTC/USDT", "timeframe": "1h"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bars"] == 300
    ds_id = body["dataset"]["id"]
    assert body["dataset"]["source"] == "csv"
    assert len(body["dataset"]["digest"]) == 16

    r = client.get("/api/arena/datasets")
    assert any(d["id"] == ds_id for d in r.json()["datasets"])
    # 列表里不得回显整份 CSV 内容
    assert all("content" not in d for d in r.json()["datasets"])

    # 坏 CSV 必须 400 并说明原因（交换两行 → 时间不再连续/升序）
    lines = csv_text.splitlines()
    lines[5], lines[6] = lines[6], lines[5]
    r = client.post(
        "/api/arena/datasets/upload",
        json={"content": "\n".join(lines), "filename": "bad.csv"},
    )
    assert r.status_code == 400
    assert "时间" in r.json()["message"]

    r = client.delete(f"/api/arena/datasets/{ds_id}")
    assert r.json()["ok"] is True


def test_sample_csv_download(client):
    r = client.get("/api/arena/datasets/sample.csv?bars=200")
    assert r.status_code == 200
    assert r.text.startswith("timestamp,open,high,low,close,volume")
    assert len(r.text.splitlines()) == 201


# ---------------- 跑批次 ----------------
def test_run_batch_with_template_and_rule(client):
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars_limit": 900,
        "with_scenarios": True,
        "entrants": [
            {"kind": "template", "template": "ma_trend", "name": "双均线趋势", "team": "趋势研究组"},
            {
                "kind": "rule",
                "name": "趋势过滤 RSI",
                "team": "第四研究组",
                "rule": {"fast": 12, "slow": 48, "buy": "sma_fast > sma_slow", "sell": "sma_fast < sma_slow"},
            },
        ],
    }
    r = client.post("/api/arena/run", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    batch = body["batch"]
    assert batch["count"] == 4  # 2 参赛 + 买入持有基准 + 现金基准
    assert batch["track_digest"]
    assert any("统一赛道" in n for n in batch["notes"])

    for window in ("7", "30"):
        lb = body["leaderboard"][window]
        assert lb["benchmark_return"] is not None
        assert all("excess_return" in row for row in lb["rows"])
        expected_hours = 168.0 if window == "7" else 720.0
        assert all(row["hours"] == expected_hours for row in lb["rows"]), (
            f"{window} 天窗口的观测时长应恰好为 {expected_hours} 小时"
        )
        assert all(row["complete"] for row in lb["rows"])

    assert set(batch["scenarios"]) == {"baseline", "cost_1.5x", "cost_2x", "low_liquidity"}

    # 历史批次可查，且排行榜与刚跑完的一致
    r = client.get("/api/arena/batches")
    assert any(b["id"] == batch["id"] for b in r.json()["batches"])

    r = client.get(f"/api/arena/batches/{batch['id']}/leaderboard?window=30")
    assert r.status_code == 200
    saved = r.json()
    assert saved["batch_id"] == batch["id"]
    assert saved["benchmark_return"] == pytest.approx(body["leaderboard"]["30"]["benchmark_return"])
    assert [row["strategy_id"] for row in saved["rows"]] == [
        row["strategy_id"] for row in body["leaderboard"]["30"]["rows"]
    ]

    r = client.get(f"/api/arena/batches/{batch['id']}/leaderboard?window=7&include_benchmarks=false")
    assert not any(row["is_benchmark"] for row in r.json()["rows"])


def test_run_batch_with_csv_dataset(client):
    csv_text = sample_csv("BTC/USDT", "1h", 900)
    up = client.post("/api/arena/datasets/upload", json={"content": csv_text, "filename": "d.csv"})
    ds_id = up.json()["dataset"]["id"]

    r = client.post(
        "/api/arena/run",
        json={
            "csv_dataset_id": ds_id,
            "entrants": [{"kind": "template", "template": "buy_hold", "name": "买入持有", "team": "某组"}],
        },
    )
    assert r.status_code == 200, r.text
    assert "用户 CSV" in r.json()["batch"]["dataset"]["source_label"]
    client.delete(f"/api/arena/datasets/{ds_id}")


def test_run_batch_rejects_bad_input(client):
    r = client.post("/api/arena/run", json={"entrants": []})
    assert r.status_code == 400

    r = client.post(
        "/api/arena/run",
        json={"entrants": [{"kind": "rule", "rule": {"buy": "eval('1')"}}]},
    )
    assert r.status_code == 400

    r = client.post(
        "/api/arena/run",
        json={"csv_dataset_id": "nope", "entrants": [{"kind": "template", "template": "cash"}]},
    )
    assert r.status_code == 404

    r = client.post("/api/arena/run", json={"entrants": [{"kind": "template", "template": "不存在"}]})
    assert r.status_code == 400


def test_run_batch_with_short_data_reports_why(client):
    """行情不足以覆盖预热时必须说清原因，而不是给一个看不懂的 500。"""
    import csv as _csv
    import io as _io

    # 手工构造 80 行（低于 MIN_BARS=102）
    out = _io.StringIO()
    writer = _csv.writer(out)
    writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    for i in range(80):
        writer.writerow([1_788_000_000_000 + i * 3_600_000, 100, 101, 99, 100, 10])
    r = client.post(
        "/api/arena/datasets/upload",
        json={"content": out.getvalue(), "filename": "short.csv"},
    )
    assert r.status_code == 400
    assert "预热" in r.json()["message"]


def test_exchange_coverage_shortfall_is_surfaced(client):
    """交易所返回的历史深度不足时，必须在批次说明里写明，不能默默跑一段短区间。"""
    payload = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "bars_limit": 5000,  # 请求一个交易所给不满的量
        "entrants": [{"kind": "template", "template": "buy_hold", "name": "买入持有", "team": "某组"}],
    }
    r = client.post("/api/arena/run", json=payload)
    assert r.status_code == 200, r.text
    ds = r.json()["batch"]["dataset"]
    assert ds["coverage_hours"] > 0
    # 无论是否触发警告，实际覆盖时长都必须出现在摘要里
    assert ds["bars"] > 0
    if ds["coverage_warning"]:
        assert any("⚠" in n for n in r.json()["batch"]["notes"])


# ---------------- 窗口口径 ----------------
def test_windows_meta_explains_the_conventions(client):
    r = client.get("/api/arena/windows/meta")
    meta = r.json()
    assert meta["warmup_bars"] == 100
    joined = " ".join(meta["rules"])
    assert "168" in joined and "720" in joined
    assert "不是滚动" in joined
    assert "进行中" in joined
    assert "未满" in meta["status_example"]
