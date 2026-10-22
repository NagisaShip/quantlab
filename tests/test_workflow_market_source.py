"""直连行情数据源卡片：原生执行、交易所自动回退、合成数据标注、离线运行明确失败。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.exchanges.base import Bar
from quant_agent.workflows.contracts import WorkflowError
from quant_agent.workflows.market import market_bars, normalize_symbol
from quant_agent.workflows.registry import Registry
from quant_agent.workflows.runtime import Runner

START = 1_700_000_000_000
STEP = 3_600_000


def _bars(symbol: str = "BTC/USDT", timeframe: str = "1h", n: int = 120) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timeframe=timeframe,
            open_time=START + i * STEP,
            close_time=START + (i + 1) * STEP - 1,
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=10.0 + i,
            closed=True,
        )
        for i in range(n)
    ]


class FakeMarket:
    """最小行情服务替身：按交易所返回预设结果，并记录调用参数。"""

    allow_synthetic = True
    stale: dict = {}

    def __init__(self, results: dict[str, object]):
        self.results = results
        self.calls: list[tuple] = []

    def get_bars(self, symbol, timeframe, *, limit=1500, exchange="binance", start=None, end=None, source="auto"):
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "limit": limit, "exchange": exchange, "source": source}
        )
        item = self.results[exchange]
        if isinstance(item, Exception):
            raise item
        return item


class Services:
    def __init__(self, market):
        self.market = market


@pytest.fixture
def client(tmp_path):
    reset_services()
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)
    with TestClient(create_app()) as c:
        me = login(c)
        yield c, services, me
    reset_services()


def test_catalog_api_lists_direct_market_source(client) -> None:
    c, _services, _me = client
    cards = c.get("/api/workflows/catalog").json()["cards"]
    card = next(d for d in cards if d["kind"] == "source.market")
    assert card["category"] == "数据源" and card["version"] == "1"
    assert card["capabilities"]["native"] == "market_bars" and card["capabilities"]["network"] is True
    kinds = [d["kind"] for d in cards]
    assert "source.csv" in kinds and "source.file" in kinds


def test_symbol_normalization_accepts_common_forms() -> None:
    assert normalize_symbol("btc/usdt") == "BTC/USDT"
    assert normalize_symbol("BTCUSDT") == "BTC/USDT"
    assert normalize_symbol("eth-usdc") == "ETH/USDC"
    with pytest.raises(WorkflowError):
        normalize_symbol("BTC")
    with pytest.raises(WorkflowError):
        normalize_symbol("")


def test_market_bars_returns_rows_and_provenance() -> None:
    symbols = _bars()
    services = Services(FakeMarket({"binance": (symbols, "exchange")}))
    out = market_bars(
        services, {"symbol": "btc-usdt", "timeframe": "1h", "bars": 120, "exchange": "binance", "source": "auto"}
    )
    assert set(out) == {"data", "source"}
    assert len(out["data"]) == 120
    row = out["data"][0]
    assert row["symbol"] == "BTC/USDT" and row["timeframe"] == "1h"
    assert row["close"] == pytest.approx(100.5) and row["closed"] is True
    assert row["time"].endswith("Z")
    report = out["source"]
    assert report["source"] == "exchange" and report["bars"] == 120
    assert report["coverage_start"] == symbols[0].open_time
    assert report["quality"]["duplicates"] == 0 and report["warning"] == ""


def test_auto_exchange_falls_back_to_next_exchange() -> None:
    services = Services(
        FakeMarket({"binance": RuntimeError("blocked"), "okx": (_bars(), "exchange"), "bybit": (_bars(), "exchange")})
    )
    out = market_bars(
        services, {"symbol": "ETH/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"}
    )
    assert out["source"]["exchange"] == "okx"
    assert [c["exchange"] for c in services.market.calls] == ["binance", "okx"]
    assert out["source"]["source_label"] == "交易所真实历史行情"


def test_auto_source_prefers_real_exchange_before_synthetic() -> None:
    """binance 只给合成数据时，继续尝试其它交易所拿真实行情。"""
    services = Services(
        FakeMarket({"binance": (_bars(), "synthetic"), "okx": (_bars(), "exchange"), "bybit": (_bars(), "exchange")})
    )
    out = market_bars(
        services, {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"}
    )
    assert out["source"]["exchange"] == "okx" and out["source"]["source"] == "exchange"


def test_synthetic_fallback_is_labeled_not_silent() -> None:
    services = Services(FakeMarket({"binance": (_bars(), "synthetic")}))
    out = market_bars(
        services, {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "binance", "source": "auto"}
    )
    assert out["source"]["source"] == "synthetic"
    assert "合成" in out["source"]["source_label"]
    assert "不是交易所真实行情" in out["source"]["warning"]


def test_explicit_synthetic_source_is_allowed_and_labeled() -> None:
    services = Services(FakeMarket({"binance": (_bars(), "synthetic")}))
    out = market_bars(
        services, {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "synthetic"}
    )
    assert out["source"]["source"] == "synthetic"
    assert services.market.calls[0]["exchange"] == "binance"
    assert "合成" in out["source"]["source_label"]


def test_all_exchanges_failing_is_explicit() -> None:
    services = Services(
        FakeMarket({"binance": RuntimeError("timeout"), "okx": RuntimeError("429"), "bybit": RuntimeError("403")})
    )
    with pytest.raises(WorkflowError, match="直连行情失败"):
        market_bars(
            services, {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "exchange"}
        )


def test_card_builtin_contract_and_native_execution(tmp_path) -> None:
    registry = Registry()
    definition = registry.get("source.market", "1")
    assert definition["category"] == "数据源"
    assert definition["capabilities"] == {"network": True, "native": "market_bars", "cache": False}
    assert set(definition["outputs"]) == {"data", "source"}
    assert definition["parameters"]["properties"]["exchange"]["enum"] == ["auto", "binance", "okx", "bybit"]

    outputs = {"data": [{"open_time": 1}], "source": {"symbol": "BTC/USDT"}}
    runner = Runner(tmp_path, registry, native={"market_bars": lambda params, *, tick=None, log=None: outputs})
    result = runner._execute(
        definition,
        {},
        {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"},
        7,
        30,
    )
    assert result["ok"] is True and result["outputs"] == outputs


def test_native_card_without_platform_handler_fails_loudly(tmp_path) -> None:
    registry = Registry()
    definition = registry.get("source.market", "1")
    runner = Runner(tmp_path, registry)
    with pytest.raises(WorkflowError, match="离线或独立运行不支持直连操作"):
        runner._execute(
            definition,
            {},
            {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"},
            7,
            30,
        )


def test_runner_run_completes_native_market_card(tmp_path) -> None:
    """原生卡片走正常运行管线：产出不可变工件并记录来源报告。"""
    from quant_agent.workflows.contracts import FORMAT

    registry = Registry()
    flow = {
        "format": FORMAT,
        "title": "直连行情",
        "nodes": [
            {
                "id": "src",
                "kind": "source.market",
                "version": "1",
                "params": {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"},
            }
        ],
        "edges": [],
    }
    outputs = {"data": [{"open_time": 1, "close": 2.0}], "source": {"symbol": "BTC/USDT", "source": "exchange"}}
    calls: list[int] = []

    def handler(params, *, tick=None, log=None):
        calls.append(1)
        return outputs

    runner = Runner(tmp_path, registry, native={"market_bars": handler})
    first = runner.run(flow)
    second = runner.run(flow)
    assert first["status"] == second["status"] == "completed"
    node = first["nodes"]["src"]
    assert node["status"] == "completed"
    assert set(node["outputs"]) == {"data", "source"}
    assert node["outputs"]["data"]["hash"] != node["outputs"]["source"]["hash"]
    # 直连行情声明 cache=False：同样参数连续运行也要重新抓取，避免把旧行情当成本次数据
    assert len(calls) == 2


def test_workflow_run_with_direct_market_source(client, monkeypatch) -> None:
    """API + worker 全链路：原生行情卡片经 handle_workflow 注入执行并落成工件。"""
    from quant_agent.workflows.contracts import FORMAT

    c, services, _me = client
    real = _bars()

    def fake_get_bars(symbol, timeframe, *, limit=1500, exchange="binance", start=None, end=None, source="auto"):
        assert symbol == "BTC/USDT" and timeframe == "1h" and limit == 120 and exchange == "binance"
        return real, "exchange"

    monkeypatch.setattr(services.market, "get_bars", fake_get_bars)
    flow = {
        "format": FORMAT,
        "title": "直连行情全链路",
        "nodes": [
            {
                "id": "src",
                "kind": "source.market",
                "version": "1",
                "params": {
                    "symbol": "BTC/USDT",
                    "timeframe": "1h",
                    "bars": 120,
                    "exchange": "binance",
                    "source": "exchange",
                },
            }
        ],
        "edges": [],
    }
    saved = c.post("/api/workflows", json={"workflow": flow})
    assert saved.status_code == 200, saved.text
    wid = saved.json()["id"]
    run = c.post(f"/api/workflows/{wid}/run", json={})
    assert run.status_code == 200, run.text
    detail = c.get("/api/workflows/runs/" + run.json()["id"]).json()
    assert detail["run"]["state"] == "COMPLETED", detail
    outputs = detail["report"]["nodes"]["src"]["outputs"]
    rows = c.get("/api/workflows/artifacts/" + outputs["data"]["hash"] + "?limit=500").json()["value"]
    report = c.get("/api/workflows/artifacts/" + outputs["source"]["hash"]).json()["value"]
    assert len(rows) == 120 and rows[0]["symbol"] == "BTC/USDT"
    assert report["source"] == "exchange" and report["exchange"] == "binance"


def test_run_export_freezes_market_data_into_csv(tmp_path) -> None:
    """导出「冻结运行」时把直连行情固化成 CSV，替换成文件数据源，离线包不再需要联网。"""
    import json
    import zipfile
    from types import SimpleNamespace

    from quant_agent.workflows.contracts import FORMAT
    from quant_agent.workflows.exporter import build_package
    from quant_agent.workflows.service import _freeze_market_sources

    root = tmp_path / "workflows"
    (root / "files").mkdir(parents=True)
    service = SimpleNamespace(root=root, files=root / "files")
    registry = Registry()
    flow = {
        "format": FORMAT,
        "title": "直连行情",
        "nodes": [
            {
                "id": "src",
                "kind": "source.market",
                "version": "1",
                "params": {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"},
            }
        ],
        "edges": [],
    }
    rows = [
        {
            "time": "2024-01-01T00:00:00Z",
            "open_time": 1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }
    ]
    runner = Runner(
        root,
        registry,
        native={"market_bars": lambda params, *, tick=None, log=None: {"data": rows, "source": {"source": "exchange"}}},
    )
    report = runner.run(flow)
    frozen_flow, frozen = _freeze_market_sources(service, flow, registry, report["id"])
    node = frozen_flow["nodes"][0]
    assert node["kind"] == "source.csv" and node["params"]["max_rows"] == 1
    csv_file = root / "files" / node["params"]["file"]
    assert csv_file.is_file() and "close" in csv_file.read_text(encoding="utf-8")
    assert frozen[0]["rows"] == 1 and frozen_flow["nodes"] and "frozen-market-" in frozen[0]["file"]

    destination = tmp_path / "package.zip"
    manifest = build_package(
        frozen_flow,
        Registry(),
        service.files,
        destination,
        include_wheels=False,
        extra_manifest={"frozen_sources": frozen},
    )
    assert manifest["offline_complete"] is True
    assert manifest["frozen_sources"][0]["rows"] == 1
    with zipfile.ZipFile(destination) as archive:
        exported = json.loads(archive.read("workflow.json"))
        assert exported["nodes"][0]["kind"] == "source.csv"
        names = archive.namelist()
        assert any(name.startswith("files/frozen-market-") and name.endswith(".csv") for name in names)
        assert any(name.startswith("files/frozen-market-") and name.endswith(".json") for name in names)


def test_freeze_refuses_when_source_report_port_is_used(tmp_path) -> None:
    import json
    from types import SimpleNamespace

    from quant_agent.workflows.contracts import FORMAT, compile_workflow
    from quant_agent.workflows.service import _freeze_market_sources

    root = tmp_path / "workflows"
    (root / "files").mkdir(parents=True)
    (root / "runs").mkdir(parents=True)
    service = SimpleNamespace(root=root, files=root / "files")
    registry = Registry()
    flow = {
        "format": FORMAT,
        "title": "来源报告被引用",
        "nodes": [
            {
                "id": "src",
                "kind": "source.market",
                "version": "1",
                "params": {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"},
            },
            {"id": "report", "kind": "report.collect", "version": "1", "params": {}},
        ],
        "edges": [{"source": "src", "source_port": "source", "target": "report", "target_port": "report"}],
    }
    plan = compile_workflow(flow, registry)
    (root / "runs" / "run_frozen.json").write_text(
        json.dumps({"id": "run_frozen", "status": "completed", "workflow_hash": plan["semantic_hash"], "nodes": {}}),
        encoding="utf-8",
    )
    with pytest.raises(WorkflowError, match="来源报告端口"):
        _freeze_market_sources(service, flow, registry, "run_frozen")


def test_direct_market_workflow_cannot_be_exported_as_offline_package(tmp_path) -> None:
    """声明联网的卡片不能冒充「完整离线包」；导出必须明确拒绝。"""
    from quant_agent.workflows.contracts import FORMAT
    from quant_agent.workflows.exporter import build_package

    flow = {
        "format": FORMAT,
        "title": "直连行情",
        "nodes": [
            {
                "id": "src",
                "kind": "source.market",
                "version": "1",
                "params": {"symbol": "BTC/USDT", "timeframe": "1h", "bars": 120, "exchange": "auto", "source": "auto"},
            }
        ],
        "edges": [],
    }
    with pytest.raises(WorkflowError, match="先运行一次"):
        build_package(flow, Registry(), tmp_path / "files", tmp_path / "out.zip", include_wheels=False)


def test_agent_workflow_help_documents_market_source() -> None:
    from quant_agent.workflows.guide import help_for

    help_text = help_for("workflow")
    assert help_text["market_example"]["kind"] == "source.market"
    assert "source.market" in help_text["data"]


def test_card_execute_outside_platform_refuses_to_fake_data() -> None:
    registry = Registry()
    definition = registry.get("source.market", "1")
    namespace: dict = {}
    exec(compile(definition["code"], "<card>", "exec"), namespace)
    with pytest.raises(RuntimeError, match="需要 QuantLab 平台的行情服务"):
        namespace["execute"](object(), {}, {})
