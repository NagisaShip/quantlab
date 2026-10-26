"""Validate saved real-provider follow-up evidence without additional API calls."""

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1])


def body(name):
    return json.loads((out / f"{name}.json").read_text(encoding="utf-8"))["body"]


def turn(name):
    data = body(name)
    messages = data["messages"]
    user_seq = max(m["seq"] for m in messages if m["role"] == "user")
    recent = [m for m in messages if m["seq"] > user_seq]
    return {
        "project_id": data["project"]["id"],
        "current_version": data["project"].get("current_v"),
        "version_count": len(data["project"].get("versions") or []),
        "budget": data["project"].get("tool_budget"),
        "execution": data.get("execution"),
        "saved": [m["data"] for m in recent if m["kind"] == "saved"],
        "errors": [m["content"] for m in recent if m["kind"] == "error"],
        "tool_events": [m.get("data", {}).get("name") for m in recent if m["kind"] == "tool"],
        "answer": "\n".join(m["content"] for m in recent if m["role"] == "assistant" and m["content"]),
    }


result = {
    "model": "deepseek-v4.1-flash",
    "factor": turn("final_factor_snapshot"),
    "models": turn("final_model_snapshot"),
    "portfolio": turn("postfix_portfolio_snapshot"),
    "strategy": turn("postfix_strategy_snapshot"),
    "usage": body("final_usage"),
}
result["activation_http"] = {}
for domain in ["factor", "model", "portfolio"]:
    response = json.loads((out / f"verified_{domain}_active_rejected.json").read_text(encoding="utf-8"))
    assert response["http"] == 409
    result["activation_http"][domain] = response["http"]
result["risk_off"] = body("verified_risk_off_diversification")["report"]["summary"]
assert 0 < result["risk_off"]["effective_n"] <= 6
assert result["risk_off"]["avg_exposure"] < 1
assert all(item["limit"] is None for item in result["usage"]["items"])
for domain in ["factor", "models", "portfolio", "strategy"]:
    assert not result[domain]["errors"], domain
for domain, key in [("models", "model_id"), ("portfolio", "scheme_id")]:
    rows = result[domain]["saved"]
    assert len(rows) == 2 and len({r[key] for r in rows}) == 2
    assert all(r["status"] == "candidate" for r in rows)
assert result["strategy"]["saved"][-1]["slug"] == "strategy-dc823d"
for domain in ["factor", "models"]:
    assert not {"evaluate_factor", "train_model"}.intersection(result[domain]["tool_events"])
    before = body(f"postfix_{'model' if domain == 'models' else domain}_snapshot")["project"]
    assert result[domain]["version_count"] == len(before["versions"])
assert "特征等权合成" in result["models"]["answer"]
assert "线性模型" in result["models"]["answer"]
assert "年化" in result["factor"]["answer"] and "累计" in result["factor"]["answer"]
for domain in ["factors", "models", "portfolio"]:
    rows = body(f"final_inventory_{domain}")[{"factors": "factors", "models": "models", "portfolio": "schemes"}[domain]]
    assert not any(r.get("status") == "active" for r in rows), domain

files = subprocess.check_output(["git", "diff", "--name-only"], cwd=root, text=True).splitlines()
files += ["src/quant_agent/research/contracts.py", "tests/test_journey_regressions.py"]
result["source_sha256"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
for path in out.glob("*.json"):
    assert not re.search(r"oc_sk_[A-Za-z0-9_]{15,}", path.read_text(encoding="utf-8")), path.name
(out / "remediation-summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(
    json.dumps(
        {
            "validated": True,
            "models": result["models"]["saved"],
            "portfolio": result["portfolio"]["saved"],
            "strategy": result["strategy"]["saved"],
        },
        ensure_ascii=False,
        indent=2,
    )
)
