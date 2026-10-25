"""Keep benchmark scoring mistakes from masquerading as runtime quality defects."""

import json

import pytest

from scripts.runtime_eval.cases import calculate, grade


def test_arithmetic_is_bounded_and_cannot_execute_tools_or_code():
    assert calculate("(1.1*0.9*1.05-1)*100")["value"] == pytest.approx(3.95)
    assert calculate("1250-12.5-340-4.5+90-3") == {"value": 980.0}
    for invalid in ("2**(9**9)", "2**1000000", "1/0", "__import__('os').getcwd()", "[0]*100000000", "9" * 513):
        assert "error" in calculate(invalid)


def test_valid_builtin_code_and_semantic_missing_fields_are_not_false_failures():
    code = """def unique_sorted(values):
    result = set()
    for value in values:
        if type(value) is int:
            result.add(value)
        elif isinstance(value, str):
            text = value.strip()
            digits = text[1:] if text and text[0] in '+-' else text
            if digits and all(48 <= ord(c) <= 57 for c in digits):
                result.add(int(text))
    return sorted(result)
"""
    assert grade("coding", json.dumps({"code": code}), [])["passed"]
    result = {
        "ready_for_live": False,
        "fee_roundtrip_bps": 10,
        "reasons": ["small sample", "in sample only"],
        "missing": ["trade timestamps", "slippage and market-impact assumptions", "benchmark returns for comparison"],
        "summary_zh": "证据不足",
    }
    calls = [{"name": "read_fixture", "arguments": {"name": "evidence"}}]
    assert grade("research_judgment", json.dumps(result), calls)["passed"]
    assert not grade(
        "coding", json.dumps({"code": "import os\ndef unique_sorted(values): return os.listdir('.')"}), []
    )["passed"]


def test_correct_numbers_in_narrative_do_not_satisfy_a_json_delivery_contract():
    assert not grade("multihop_evidence", "The compound return is 3.95%.", [])["passed"]
    valid = {
        "dataset": "returns_v2",
        "version": "2",
        "cumulative_return_pct": 3.95,
        "costs_included": False,
        "guaranteed": False,
    }
    calls = [
        {"name": "read_fixture", "arguments": {"name": "catalog"}},
        {"name": "read_fixture", "arguments": {"name": "returns_actual"}},
    ]
    assert grade("multihop_evidence", json.dumps(valid), calls)["passed"]
