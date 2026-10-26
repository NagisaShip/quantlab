"""因子研究：表达式、评估引擎、因子库、信息源、因子库 API 与 AI 研究室里的因子项目（规则降级与模型两条路径）。"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult, LLMError, ToolCall
from quant_agent.research import factor_expr as FX
from quant_agent.research.datahub import DataHub
from quant_agent.research.factor_eval import evaluate_factor, t_required
from quant_agent.research.factor_lib import FactorLibrary, mutate, pick_seeds
from quant_agent.research.goals import FACTOR
from quant_agent.research.panel import Panel
from quant_agent.store import JsonStore


# ---------------------------------------------------------------------------
# 表达式
# ---------------------------------------------------------------------------
def test_expression_parse_normalize_and_limits():
    node = FX.parse("cs_rank( -ts_mean(funding,9) )")
    assert str(node) == FX.normalize("cs_rank(-ts_mean(funding, 9))")
    assert FX.fields_used(node) == ["funding"]
    assert FX.max_lookback(node) >= 9
    for bad in (
        "ts_mean(close, x)",
        "ts_mean(close, 0)",
        "ts_mean(close, 100000)",
        "foo(close)",
        "__import__('os')",
        "close +",
        "open.close",
        "ts_mean(close, 2.5)",
    ):
        with pytest.raises(FX.FactorExprError):
            FX.parse(bad)
    assert FX.parse("x_my_series * 2")  # 自定义字段允许
    deep = "close" + " + close" * 80
    with pytest.raises(FX.FactorExprError):
        FX.parse(deep)


def test_expression_has_no_lookahead():
    rng = np.random.default_rng(0)
    T, N = 300, 6
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (T, N)), axis=0))
    panel = {"close": close, "volume": rng.uniform(1, 2, (T, N))}
    for expr in (
        "ts_mean(close, 10)",
        "ts_rank(close, 20)",
        "ts_corr(close, volume, 15)",
        "decay_linear(close, 8)",
        "ema(close, 12)",
        "cs_rank(delta(close, 3))",
        "ts_zscore(close, 30)",
    ):
        full = FX.evaluate(expr, panel)
        cut = FX.evaluate(expr, {k: v[:200] for k, v in panel.items()})
        np.testing.assert_allclose(full[:200], cut, equal_nan=True, err_msg=expr)


# ---------------------------------------------------------------------------
# 评估引擎
# ---------------------------------------------------------------------------
def _panel(T=1500, N=10, seed=1, signal=0.0):
    """合成面板：signal > 0 时，过去 5 期收益的反向对下一期收益有预测力。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.01, (T, N))
    for t in range(6, T):
        past = ret[t - 5 : t].sum(axis=0)
        ret[t] += -signal * (past - past.mean())
    close = 100 * np.exp(np.cumsum(ret, axis=0))
    data = {
        "close": close,
        "open": close,
        "high": close * 1.002,
        "low": close * 0.998,
        "volume": rng.uniform(100, 200, (T, N)),
    }
    data["amount"] = data["close"] * data["volume"]
    data["vwap"] = close
    prev = np.vstack([np.full((1, N), np.nan), close[:-1]])
    data["ret"] = close / prev - 1
    with FX._quiet():
        data["mkt_ret"] = np.repeat(np.nanmean(data["ret"], axis=1, keepdims=True), N, axis=1)
    times = np.arange(T, dtype=np.int64) * 14_400_000 + 1_600_000_000_000
    return Panel(times, [f"S{i}/USDT" for i in range(N)], data, "4h", {f"S{i}/USDT": "synthetic" for i in range(N)})


def test_evaluate_factor_finds_planted_signal_and_rejects_noise():
    panel = _panel(signal=0.15)
    good = evaluate_factor("-returns(5)", panel, horizon=1, cost_bps=5)
    s = good["summary"]
    assert s["mode"] == "cross" and s["rank_ic"] > 0.05 and s["ic_t"] > s["t_required"]
    assert len(good["groups"]["mean"]) == 5 and good["groups"]["mean"][-1] > good["groups"]["mean"][0]
    assert good["decay"][0]["rank_ic"] is not None
    assert any(c["key"] == "significance" and c["ok"] for c in good["checks"])

    noise = evaluate_factor("ts_mean(volume, 7)", _panel(signal=0.0, seed=3))
    assert not noise["admitted"]
    assert abs(noise["summary"]["rank_ic"] or 0) < 0.05

    # 多重检验：试验越多门槛越高
    assert t_required(1) < t_required(50) < t_required(1000)

    # 与库内因子高度相关 → 不通过新颖性
    lib = {"已有反转": FX.evaluate("-returns(5)", panel.data)}
    dup = evaluate_factor("-returns(5) * 2", panel, library=lib)
    assert dup["summary"]["max_corr"] > 0.95
    assert not next(c for c in dup["checks"] if c["key"] == "novelty")["ok"]


def test_time_series_mode_for_small_universe():
    panel = _panel(N=2, signal=0.0)
    rep = evaluate_factor("-returns(5)", panel)
    assert rep["summary"]["mode"] == "time_series"


# ---------------------------------------------------------------------------
# 因子库、种子与变换
# ---------------------------------------------------------------------------
def test_factor_library_and_trials(tmp_path):
    store = JsonStore(tmp_path)
    lib = FactorLibrary(store)
    panel = _panel(signal=0.15)
    rep = evaluate_factor("-returns(5)", panel)
    # Isolate library activation with an eligible report; synthetic rejection has its own regression.
    assert not rep["activation_eligible"]
    rep["panel"]["synthetic"] = False
    rec = lib.save({"name": "短期反转", "expression": "-returns( 5 )", "status": "active"}, report=rep)
    assert rec["expression"] == FX.normalize("-returns(5)") and lib.report(rec["id"])["summary"]
    dup = lib.save({"name": "短期反转", "expression": "-returns(10)"})
    assert dup["name"] != "短期反转"
    assert set(lib.library_values(panel)) == {"短期反转"}
    assert lib.library_values(panel, exclude=rec["id"]) == {}
    lib.record_trial("-returns(5)", rep["summary"])
    lib.record_trial("-returns(5)", rep["summary"])
    assert lib.trial_count() == 1
    lib.record_trial("ts_mean(volume, 7)", {"rank_ic": 0.001, "ic_t": 0.2})
    assert lib.failed_ideas()[0]["expression"] == "ts_mean(volume, 7)"
    assert lib.set_status(rec["id"], "retired")["status"] == "retired"
    assert lib.delete(dup["id"])

    seeds = pick_seeds("资金费率太高的币会不会跌", k=2)
    assert "funding" in seeds[0]["expression"]
    outs = mutate("-ts_mean(funding, 9)", 1)
    assert outs and all(FX.parse(e) for e, _ in outs)
    assert all(e != "-ts_mean(funding, 9)" for e, _ in outs)


# ---------------------------------------------------------------------------
# 信息源：时点对齐、连接器、CSV
# ---------------------------------------------------------------------------
def test_datahub_asof_uses_available_at(tmp_path):
    hub = DataHub(JsonStore(tmp_path))
    day = 86_400_000
    # 日数据 ts=第 0 天，第 1 天才发布
    hub.put_series(field="fng", symbol=None, source="t", rows=[(0, day, 10.0), (day, 2 * day, 20.0)])
    key = hub.find("fng", "BTC/USDT")["key"]
    got = hub.asof(key, np.array([day // 2, day, day + 1, 2 * day + 5], dtype=np.int64))
    assert np.isnan(got[0]) and got[1] == 10.0 and got[2] == 10.0 and got[3] == 20.0


def test_datahub_sync_with_mock_transport(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "fundingRate" in url:
            start = int(request.url.params.get("startTime", 0))
            if start > 1_700_000_000_000 + 8 * 3_600_000 * 3:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "fundingTime": 1_700_000_000_000 + i * 28_800_000,
                        "fundingRate": str(0.0001 * (i + 1)),
                    }
                    for i in range(3)
                ],
            )
        if "fng" in url:
            return httpx.Response(
                200,
                json={"data": [{"value": "40", "timestamp": "1700000000"}, {"value": "60", "timestamp": "1700086400"}]},
            )
        return httpx.Response(451, json={"msg": "restricted"})

    hub = DataHub(JsonStore(tmp_path), transport=httpx.MockTransport(handler))
    out = hub.sync("binance_futures", symbols=["BTC/USDT"], start=1_699_990_000_000, end=1_700_200_000_000)
    funding = [d for d in out["datasets"] if d["field"] == "funding"]
    assert funding and funding[0]["rows"] == 3
    assert out["errors"]  # 其它端点 451：记录错误，不影响已成功的
    rows = hub.series(funding[0]["key"])
    assert rows[0][1] >= rows[0][0]  # available_at ≥ ts
    fng = hub.sync("alternative_me")
    assert fng["ok"] and fng["datasets"][0]["rows"] == 2
    with pytest.raises(ValueError):
        hub.sync("fred")  # 没有密钥


def test_csv_import_rejects_formulas(tmp_path):
    hub = DataHub(JsonStore(tmp_path))
    rec = hub.import_csv("ts,value\n2024-01-01,1.5\n2024-01-02,2.5\n", name="Macro_X", lag_ms=3_600_000)
    assert rec["field"] == "x_macro_x" and rec["rows"] == 2
    rows = hub.series(rec["key"])
    assert rows[0][1] - rows[0][0] == 3_600_000
    with pytest.raises(ValueError):
        hub.import_csv("ts,value\n2024-01-01,=CMD()\n", name="bad")


# ---------------------------------------------------------------------------
# 目标
# ---------------------------------------------------------------------------
def test_factor_goal_parse_and_check():
    parsed = FACTOR.parse_text("/goal RankIC>0.03 ICIR>0.5 库内相关<0.6 4h 持有期 3")
    spec = FACTOR.normalize_spec(parsed["spec"], FACTOR.normalize_spec({}))
    metrics = {i["metric"] for i in spec["items"]}
    assert {"rank_ic", "icir", "max_corr"} <= metrics
    assert spec["timeframe"] == "4h" and spec["horizon"] == 3
    ev = FACTOR.evaluate({"rank_ic": 0.05, "icir": 0.8, "max_corr": 0.2}, spec["items"])
    assert ev["passed"]
    assert FACTOR.check(spec)["verdict"] in ("ok", "warn")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


EVAL = {"universe": "major10", "timeframe": "4h", "bars_limit": 600, "source": "synthetic"}


def test_factor_api_validate_evaluate_and_library(client):
    meta = client.get("/api/factors/meta").json()
    assert meta["catalog"]["operators"] and meta["seeds"] and meta["universes"]
    assert client.post("/api/factors/validate", json={"expression": "ts_mean(close,"}).json()["ok"] is False
    v = client.post("/api/factors/validate", json={"expression": "-ts_corr(close,volume,20)"}).json()
    assert v["ok"] and v["fields"] == ["close", "volume"]

    r = client.post("/api/factors/evaluate", json={"expression": "-returns(5)", **EVAL})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["summary"]["rank_ic"] is not None and rep["panel"]["synthetic"]
    assert len(rep["checks"]) >= 4
    assert client.get("/api/factors/trials").json()["total"] == 1
    assert client.post("/api/factors/evaluate", json={"expression": "foo(close)", **EVAL}).status_code == 400

    r = client.post(
        "/api/factors",
        json={
            "name": "反转",
            "expression": "-returns(5)",
            "evaluate": False,
            "hypothesis": {"category": "microstructure", "rationale": "流动性补偿"},
        },
    )
    fid = r.json()["factor"]["id"]
    assert client.get("/api/factors").json()["factors"][0]["id"] == fid
    assert client.post(f"/api/factors/{fid}/status", json={"status": "active"}).status_code == 409
    r = client.post(f"/api/factors/{fid}/refresh", json={"source": "synthetic", "bars_limit": 600})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/factors/{fid}/report").json()["report"]["summary"]
    assert client.patch(f"/api/factors/{fid}", json={"note": "备注"}).json()["factor"]["note"] == "备注"
    assert client.delete(f"/api/factors/{fid}").json()["ok"]
    assert client.get(f"/api/factors/{fid}").status_code == 404


def test_datahub_api(client):
    conns = client.get("/api/datahub/connectors").json()
    ids = {c["id"] for c in conns["connectors"]}
    assert {"binance_futures", "defillama", "alternative_me", "fred", "csv"} <= ids
    r = client.post(
        "/api/datahub/import", json={"name": "onchain", "text": "ts,value\n1700000000000,1\n1700003600000,2\n"}
    )
    assert r.status_code == 200, r.text
    key = r.json()["dataset"]["key"]
    assert client.get(f"/api/datahub/datasets/{key}/series").json()["total"] == 2
    assert "x_onchain" in client.get("/api/factors/meta").json()["custom_fields"]
    assert client.post("/api/datahub/sync", json={"connector": "fred"}).status_code == 400
    assert client.post("/api/datahub/keys", json={"connector": "fred", "key": "abc"}).json()["has_key"]
    conns = client.get("/api/datahub/connectors").json()["connectors"]
    assert next(c for c in conns if c["id"] == "fred")["has_key"] is True
    assert "abc" not in json.dumps(conns)
    assert client.delete(f"/api/datahub/datasets/{key}").json()["ok"]


# ---------------------------------------------------------------------------
# AI 研究室里的因子项目
# ---------------------------------------------------------------------------
def _factor_project(client, **kw):
    body = {
        "kind": "factor",
        "idea": "资金费率太高的币后面会不会跑输",
        "data_source": "synthetic",
        "bars_limit": 600,
        "max_rounds": 2,
        "timeframe": "4h",
        **kw,
    }
    r = client.post("/api/lab/projects", json=body)
    assert r.status_code == 200, r.text
    return r.json()["project"]


def test_factor_project_fallback_autopilot_and_library(client):
    p = _factor_project(client)
    assert p["kind"] == "factor" and len(p["securities"]) == 10
    meta = client.get("/api/lab/meta").json()
    assert "factor" in meta["domains"] and {"strategy", "factor"} <= {k["id"] for k in meta["kinds"]}

    r = client.post(f"/api/lab/projects/{p['id']}/autopilot", json={"wait": True})
    assert r.status_code == 200, r.text
    view = r.json()
    kinds = [m["kind"] for m in view["messages"]]
    assert "factor_report" in kinds and "ask" in kinds
    proj = view["project"]
    assert proj["versions"] and proj["versions"][0]["metrics"]["rank_ic"] is not None
    assert view["context"]

    r = client.post(f"/api/lab/projects/{p['id']}/decision", json={"choice": "入库", "wait": True})
    assert r.status_code == 200, r.text
    fid = r.json()["project"]["factor_id"]
    assert fid and client.get(f"/api/factors/{fid}").json()["factor"]["project_id"] == p["id"]


def test_factor_project_chat_expression_and_goal(client):
    p = _factor_project(client)
    pid = p["id"]
    r = client.post(f"/api/lab/projects/{pid}/messages", json={"text": "-ts_mean(funding, 9)", "wait": True})
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    rep = [m for m in msgs if m["kind"] == "factor_report"]
    assert rep and rep[-1]["data"]["expression"] == FX.normalize("-ts_mean(funding, 9)")
    assert any("合成" in w for w in rep[-1]["data"]["panel"]["warnings"])

    parsed = client.post("/api/lab/goal/parse", json={"text": "RankIC>0.04 持有期 2", "project_id": pid}).json()
    assert parsed["domain"] == "factor" and parsed["spec"]["horizon"] == 2
    r = client.post(f"/api/lab/projects/{pid}/goal", json={"spec": parsed["spec"]})
    assert r.status_code == 200, r.text
    assert r.json()["project"]["horizon"] == 2

    assert client.post(f"/api/lab/projects/{pid}/code", json={"code": "ts_mean(close,"}).status_code == 400
    r = client.post(f"/api/lab/projects/{pid}/code", json={"code": "-returns(5)", "wait": True})
    assert r.status_code == 200 and r.json()["project"]["code"] == FX.normalize("-returns(5)")
    r = client.post(f"/api/lab/projects/{pid}/save")
    assert r.status_code == 200 and r.json()["factor"]["expression"] == FX.normalize("-returns(5)")
    # 策略项目不受影响
    strat = client.post("/api/lab/projects", json={"idea": "BTC 均线", "data_source": "synthetic"}).json()["project"]
    assert strat.get("kind", "strategy") == "strategy"


class ScriptedRouter:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        if not self.replies:
            raise LLMError("no more replies")
        r = self.replies.pop(0)
        return r if isinstance(r, ChatResult) else ChatResult(content=r, model="fake-1", provider="fake")

    def available_models(self):
        return [{"provider": "fake", "model": "fake-1", "target": "fake/fake-1", "type": "openai_compatible"}]

    def _has_credentials(self, _):
        return True


def test_factor_project_llm_tools(client):
    p = _factor_project(client)
    write = ToolCall(
        id="c1",
        name="write_factor",
        arguments={
            "expression": "-returns(5)",
            "name": "短期反转",
            "hypothesis": {
                "category": "microstructure",
                "rationale": "流动性补偿",
                "direction": "涨多跌",
                "falsify": "大币不显著",
            },
        },
    )
    router = ScriptedRouter(
        [
            ChatResult(content="先试短期反转", model="fake-1", provider="fake", tool_calls=[write]),
            "评估完成：RankIC 为正，建议继续。",
        ]
    )
    get_services().router = router
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "试试短期反转", "wait": True})
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["project"]["code"] == FX.normalize("-returns(5)")
    assert view["project"]["hypothesis"]["rationale"] == "流动性补偿"
    tool_names = {t.name for t in router.calls[0]["tools"]}
    assert {"write_factor", "evaluate_factor", "search_factor_library", "set_factor_goal"} <= tool_names
    sys_prompt = router.calls[0]["messages"][0].content
    assert "因子表达式语法" in sys_prompt
