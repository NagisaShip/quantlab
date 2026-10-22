import pytest

pytest.importorskip("pandas")

from quant_agent.workflows.contracts import FORMAT, WorkflowError, compile_workflow
from quant_agent.workflows.registry import Registry
from quant_agent.workflows.runtime import Runner
from quantlab_research import finance, network, tables


def test_fitted_transform_does_not_learn_from_test_and_join_checks_keys():
    transformer = tables.fit_transformer([{"x": 1}, {"x": 3}], ["x"])
    assert tables.apply_transformer([{"x": 101}], transformer)[0]["x"] == 99
    assert transformer["fields"]["x"]["mean"] == 2
    with pytest.raises(ValueError):
        tables.join([{"id": 1}], [{"id": 1}, {"id": 1}], ["id"])
    with pytest.raises(ValueError):
        tables.join([{"id": None}], [{"id": None}], ["id"])


def test_component_graph_detects_cycle_and_residual_mismatch():
    import copy

    value = network.inspect(network.DEFAULT)
    assert value["output_shape"] == [1] and value["parameter_count"] == 321
    wrong = copy.deepcopy(network.DEFAULT)
    wrong["nodes"].append({"id": "sum", "type": "Add", "inputs": ["input", "hidden"]})
    wrong["output"] = "sum"
    with pytest.raises(ValueError, match="形状"):
        network.inspect(wrong)
    wrong = {
        "nodes": [{"id": "a", "type": "ReLU", "inputs": ["b"]}, {"id": "b", "type": "ReLU", "inputs": ["a"]}],
        "output": "a",
    }
    with pytest.raises(ValueError, match="循环"):
        network.inspect(wrong)


def test_component_execution_and_parameter_count_match():
    torch = pytest.importorskip("torch")
    graph = {
        "nodes": [
            {"id": "x", "type": "Input", "shape": [4, 3]},
            {"id": "gru", "type": "GRU", "inputs": ["x"], "hidden_size": 5},
            {"id": "out", "type": "Linear", "inputs": ["gru"], "out_features": 1},
        ],
        "output": "out",
    }
    model = network.build(graph)
    assert tuple(model(torch.randn(2, 4, 3)).shape) == (2, 1)
    assert sum(p.numel() for p in model.parameters()) == network.inspect(graph)["parameter_count"]


def test_purged_labels_and_execution_delay():
    rows = [{"symbol": "A", "date": i, "close": 100 + i} for i in range(10)]
    labels = finance.forward_label(rows, "close", "symbol", "date", 2)
    parts = finance.purged_split(labels, "date", "label_end", 4, 7)
    assert all(r["label_end"] <= 4 for r in parts["train"])
    assert all(r["label_end"] <= 7 for r in parts["validation"])
    result = finance.backtest(
        [
            {"symbol": "A", "date": 1, "return": 0.1, "signal": 1},
            {"symbol": "A", "date": 2, "return": 0.2, "signal": 0},
        ],
        "symbol",
        "date",
        fee_bps=0,
    )
    assert result["series"][0]["net_return"] == 0
    assert result["series"][1]["net_return"] == 0.2


def test_complete_model_module_keeps_roles_and_public_parameters(tmp_path):
    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "model",
                "kind": "module.model",
                "version": "1",
                "params": {"features": ["x"], "target": "y"},
                "position": {"x": 0, "y": 0},
            }
        ],
        "edges": [],
    }
    runner = Runner(tmp_path, registry)
    train = runner.artifact([{"x": i, "y": i % 2} for i in range(20)], {"type": "table", "role": "train"})
    val = runner.artifact([{"x": i, "y": i % 2} for i in range(20, 28)], {"type": "table", "role": "validation"})
    plan = compile_workflow(flow, registry, external={"model": {"train": train, "validation": val}})
    assert plan["order"] == ["model"]
    report = runner.run(flow, external={"model": {"train": train, "validation": val}})
    assert report["status"] == "completed", report
    model = runner.read(report["nodes"]["model"]["outputs"]["model"])
    assert model["features"] == ["x"]
    from quant_agent.workflows.contracts import subflow_instance

    expanded = subflow_instance(registry.get("module.model", "1"), flow["nodes"][0]["params"])
    assert any(n["params"].get("features") == ["x"] for n in expanded["nodes"])


def test_role_inheritance_cannot_launder_test_into_training():
    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "split",
                "kind": "research.temporal_split",
                "version": "1",
                "params": {"time": "date", "train_end": "2020", "validation_end": "2021"},
            },
            {"id": "clean", "kind": "research.deduplicate", "version": "1", "params": {}},
            {"id": "fit", "kind": "research.fit_transformer", "version": "1", "params": {"fields": ["x"]}},
        ],
        "edges": [
            {"source": "split", "source_port": "test", "target": "clean", "target_port": "data"},
            {"source": "clean", "source_port": "data", "target": "fit", "target_port": "data"},
        ],
    }
    with pytest.raises(WorkflowError, match="用途"):
        compile_workflow(flow, registry, external={"split": {"data": {"type": "table"}}})


def test_composite_cleanup_preserves_train_role_and_direct_external_test_is_rejected(tmp_path):
    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "clean", "kind": "module.clean", "version": "1", "params": {}}],
        "edges": [],
    }
    plan = compile_workflow(flow, registry, external={"clean": {"data": {"type": "table", "role": "train"}}})
    assert plan["definitions"]["clean"]["outputs"]["data"]["role"] == "train"
    flow["nodes"] = [{"id": "fit", "kind": "research.fit_transformer", "version": "1", "params": {"fields": ["x"]}}]
    with pytest.raises(WorkflowError, match="用途"):
        compile_workflow(flow, registry, external={"fit": {"data": {"type": "table", "role": "test"}}})


def test_time_split_numeric_strings_and_iso_dates():
    assert (
        len(tables.temporal_split([{"year": 2020}, {"year": 2021}, {"year": 2022}], "year", "2020", "2021")["train"])
        == 1
    )
    assert (
        len(
            tables.temporal_split([{"date": "2020-01-01"}, {"date": "2021-01-01"}], "date", "2020-12-31", "2021-12-31")[
                "validation"
            ]
        )
        == 1
    )


def test_native_factor_is_shared_and_keeps_date_representation():
    import numpy as np

    from quant_agent.research import factor_expr as original
    from quantlab_research.engines import factor_expr
    from quantlab_research.native import factor

    assert original is factor_expr
    rows = [{"symbol": "A", "date": f"2024-01-{i:02d}", "close": 100 + i} for i in range(1, 8)]
    result = factor(rows, "returns(2)")
    expected = factor_expr.evaluate("returns(2)", {"close": np.array([[r["close"]] for r in rows])}).reshape(-1)
    assert result[0]["date"] == rows[0]["date"]
    np.testing.assert_allclose(
        [r["factor"] if r["factor"] is not None else np.nan for r in result], expected, equal_nan=True
    )


def test_grid_and_rolling_plans_are_bounded_and_do_not_mutate_saved_flow():
    import copy

    from quant_agent.workflows.experiments import variants

    flow = {
        "format": FORMAT,
        "nodes": [{"id": "source", "kind": "source.csv", "version": "1", "params": {"file": "input.csv"}}],
        "edges": [],
    }
    before = copy.deepcopy(flow)
    rows = variants(
        flow, Registry(), {"mode": "grid", "axes": [{"node": "source", "parameter": "max_rows", "values": [10, 20]}]}
    )
    assert len(rows) == 2 and flow == before
    with pytest.raises(WorkflowError, match="8"):
        variants(
            flow,
            Registry(),
            {"mode": "grid", "axes": [{"node": "source", "parameter": "max_rows", "values": list(range(1, 10))}]},
        )


def test_reencapsulation_exposes_parameters_and_expansion_keeps_edits(tmp_path):
    from quant_agent.workflows.subflows import encapsulate, expand

    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "source",
                "kind": "source.csv",
                "version": "1",
                "params": {"file": "input.csv"},
                "position": {"x": 0, "y": 0},
            }
        ],
        "edges": [],
    }
    code, grouped = encapsulate(
        flow,
        ["source"],
        "user.test",
        "研究模板",
        registry,
        exposed=[{"node": "source", "parameter": "encoding", "name": "encoding"}],
    )
    registry.add(code)
    grouped["nodes"][0]["params"]["encoding"] = "gb18030"
    expanded = expand(grouped, grouped["nodes"][0]["id"], registry)
    assert expanded["nodes"][0]["params"]["encoding"] == "gb18030"


def test_frozen_registry_does_not_replace_historical_definition_with_current_builtin():
    original = Registry().get("source.csv", "1")["code"]
    historical = original.replace("CSV", "Historical CSV")
    frozen = Registry([{"code": historical}], frozen=True)
    assert len(frozen.catalog()) == 1 and frozen.get("source.csv", "1")["code"] == historical


@pytest.mark.parametrize(
    "kind", ["line", "bar", "area", "scatter", "histogram", "box", "heatmap", "network", "candlestick"]
)
def test_chart_types_generate_real_png_and_svg(tmp_path, kind):
    from quantlab_research.charts import render

    rows = [{"x": i, "y": i * i, "open": i + 1, "close": i + 2, "high": i + 3, "low": i} for i in range(1, 5)]
    target = tmp_path / (kind + ".png")
    spec = render(rows, {"chart_type": kind, "x": "x", "y": "y"}, target)
    assert target.read_bytes().startswith(b"\x89PNG")
    assert "<svg" in target.with_suffix(".svg").read_text(encoding="utf-8")
    assert spec["statistics"]["rows"] == 4


def test_complete_strategy_chart_pipeline_from_csv_and_style_only_reuses_upstream(tmp_path):
    from quantlab_research.templates import templates

    runner = Runner(tmp_path, Registry())
    (runner.files / "input.csv").write_text(
        "symbol,date,close\n" + "".join(f"001,2024-01-{i:02d},{100 + i}\n" for i in range(1, 12)), encoding="utf-8"
    )
    flow = templates()[1]
    report = runner.run(flow)
    assert report["status"] == "completed", report
    chart = runner.read(report["nodes"]["chart"]["outputs"]["chart"])
    assert chart["statistics"]["rows"] == 10
    assert (runner.files / chart["files"]["png"]["file"]).read_bytes().startswith(b"\x89PNG")
    flow["nodes"][-1]["params"]["title"] = "Different style only"
    second = runner.run(flow)
    assert second["status"] == "completed", second
    assert second["nodes"]["research"]["status"] == "cached"
    assert second["nodes"]["chart"]["status"] == "completed"


def test_neural_module_training_and_frozen_prediction_follow_component_graph(tmp_path):
    import copy

    pytest.importorskip("torch")
    graph = copy.deepcopy(network.DEFAULT)
    graph["nodes"][0]["shape"] = [2]
    registry = Registry()
    runner = Runner(tmp_path, registry)
    train = runner.artifact(
        [{"x": float(i), "z": float(i % 3), "label": i % 2} for i in range(16)], {"type": "table", "role": "train"}
    )
    val = runner.artifact(
        [{"x": float(i), "z": float(i % 3), "label": i % 2} for i in range(16, 22)],
        {"type": "table", "role": "validation"},
    )
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "network",
                "kind": "module.network",
                "version": "1",
                "params": {"graph": graph, "features": ["x", "z"], "epochs": 2},
            }
        ],
        "edges": [],
    }
    report = runner.run(flow, external={"network": {"train": train, "validation": val}})
    assert report["status"] == "completed", report
    model = report["nodes"]["network"]["outputs"]["model"]
    assert model["semantics"] == {"backend": "torch"}
    value = runner.read(model)
    assert value["parameter_count"] == network.inspect(graph)["parameter_count"]
    prediction = {
        "format": FORMAT,
        "nodes": [{"id": "predict", "kind": "research.predict_network", "version": "1", "params": {}}],
        "edges": [],
    }
    test = runner.artifact([{"x": 10.0, "z": 1.0}, {"x": 11.0, "z": 2.0}], {"type": "table", "role": "test"})
    result = runner.run(prediction, external={"predict": {"data": test, "model": model}})
    assert result["status"] == "completed", result
    assert len(runner.read(result["nodes"]["predict"]["outputs"]["data"])) == 2


def test_incomplete_module_can_expand_and_reencapsulate_but_cannot_execute():
    from quant_agent.workflows.subflows import encapsulate, expand

    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [
            {"id": "model", "kind": "module.network", "version": "1", "params": {}, "position": {"x": 0, "y": 0}}
        ],
        "edges": [],
    }
    expanded = expand(flow, "model", registry)
    assert len(expanded["nodes"]) == 2
    with pytest.raises(WorkflowError):
        compile_workflow(expanded, registry)
    code, grouped = encapsulate(expanded, [n["id"] for n in expanded["nodes"]], "user.network", "My network", registry)
    definition = registry.add(code)
    assert len(definition["inputs"]) == 2
    assert len(grouped["nodes"]) == 1


def test_composite_public_parameter_binding_is_resolved_at_runtime_without_silent_expansion_loss(tmp_path):
    from quant_agent.workflows.subflows import expand

    registry = Registry()
    runner = Runner(tmp_path, registry)
    train = runner.artifact(
        [{"x": float(i), "label": i % 2, "fields": ["x"]} for i in range(12)], {"type": "table", "role": "train"}
    )
    val = runner.artifact(
        [{"x": float(i), "label": i % 2} for i in range(12, 18)], {"type": "table", "role": "validation"}
    )
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "fit",
                "kind": "module.model",
                "version": "1",
                "params": {},
                "bindings": {"features": {"input": "train", "path": ["0", "fields"]}},
            }
        ],
        "edges": [],
    }
    report = runner.run(flow, external={"fit": {"train": train, "validation": val}})
    assert report["status"] == "completed", report
    assert runner.read(report["nodes"]["fit"]["outputs"]["model"])["features"] == ["x"]
    with pytest.raises(WorkflowError, match="绑定"):
        expand(flow, "fit", registry)


def test_unknown_label_ends_remain_unknown_in_numeric_and_date_splits():
    for rows, a, b in [
        ([{"t": 1, "end": None}], 1, 2),
        ([{"t": "2024-01-01", "end": None}], "2024-02-01", "2024-03-01"),
    ]:
        result = finance.purged_split(rows, "t", "end", a, b)
        assert not result["train"] and result["audit"]["excluded_boundary_or_unknown"] == 1


def test_join_does_not_erase_test_role_from_either_input():
    flow = {
        "format": FORMAT,
        "nodes": [
            {"id": "join", "kind": "research.join", "version": "1", "params": {"keys": ["id"]}},
            {"id": "fit", "kind": "research.fit_transformer", "version": "1", "params": {"fields": ["x"]}},
        ],
        "edges": [{"source": "join", "source_port": "data", "target": "fit", "target_port": "data"}],
    }
    with pytest.raises(WorkflowError, match="用途"):
        compile_workflow(
            flow,
            Registry(),
            external={"join": {"left": {"type": "table", "role": "train"}, "right": {"type": "table", "role": "test"}}},
        )


@pytest.mark.parametrize("domain", ["factor", "portfolio", "execution"])
def test_expandable_native_research_modules_execute_the_shared_engines(tmp_path, domain):
    import math

    rows = []
    count = 400 if domain == "execution" else 150
    symbols = ["A"] if domain == "execution" else ["A", "B", "C"]
    step = 60000 if domain == "execution" else 86400000
    for t in range(count):
        for j, symbol in enumerate(symbols):
            value = 100 * (1 + 0.0001 * t + 0.01 * math.sin(t / 7 + j))
            rows.append(
                {
                    "symbol": symbol,
                    "date": 1704067200000 + t * step,
                    "open": value,
                    "close": value * 1.0001,
                    "high": value * 1.002,
                    "low": value * 0.998,
                    "volume": 1000.0,
                }
            )
    registry = Registry()
    runner = Runner(tmp_path, registry)
    params = {}
    if domain == "portfolio":
        params = {"config": {"optimizer": "equal", "lookback": 60, "rebalance": 10}}
    if domain == "execution":
        params = {"config": {"algo": "twap", "horizon": 10, "episodes": 4}}
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "research", "kind": "module.native_" + domain, "version": "1", "params": params}],
        "edges": [],
    }
    data = runner.artifact(rows, {"type": "table"})
    result = runner.run(flow, external={"research": {"data": data}})
    assert result["status"] == "completed", result
    assert result["nodes"]["research"]["child_run_id"]
    table = runner.read(result["nodes"]["research"]["outputs"]["data"])
    assert len(table) > 0


def test_generated_ids_support_eight_nested_templates_without_layout_drift():
    from quant_agent.workflows.subflows import encapsulate, expand

    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [
            {
                "id": "s" + "x" * 79,
                "kind": "source.csv",
                "version": "1",
                "params": {"file": "input.csv"},
                "position": {"x": 400, "y": 240},
            }
        ],
        "edges": [],
    }
    for i in range(8):
        code, flow = encapsulate(flow, [flow["nodes"][0]["id"]], "user." + "layer" * 14 + str(i), "Nested", registry)
        registry.add(code)
    for _ in range(8):
        flow = expand(flow, flow["nodes"][0]["id"], registry)
    assert len(flow["nodes"][0]["id"]) <= 80
    assert flow["nodes"][0]["position"] == {"x": 400, "y": 240}
    assert flow["nodes"][0]["params"]["file"] == "input.csv"


def test_custom_composite_keeps_dynamic_roles_when_reused_for_different_samples():
    from quant_agent.workflows.subflows import encapsulate

    registry = Registry()
    flow = {
        "format": FORMAT,
        "nodes": [{"id": "clean", "kind": "research.deduplicate", "version": "1", "params": {}}],
        "edges": [],
    }
    code, grouped = encapsulate(flow, ["clean"], "user.cleanup", "Cleanup", registry)
    definition = registry.add(code)
    nid = grouped["nodes"][0]["id"]
    inp = next(iter(definition["inputs"]))
    out = next(iter(definition["outputs"]))
    for role in ("train", "validation", "test"):
        plan = compile_workflow(grouped, registry, external={nid: {inp: {"type": "table", "role": role}}})
        assert plan["definitions"][nid]["outputs"][out]["role"] == role
