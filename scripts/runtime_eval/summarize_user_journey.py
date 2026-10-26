"""Summarize saved user-journey evidence without making network/model calls."""

import ast
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

p = Path(sys.argv[1])


def read(name):
    return json.loads((p / f"{name}.json").read_text(encoding="utf-8"))["body"]


def dump(name, obj):
    (p / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


summary = {"factors": [], "models": [], "strategies": [], "portfolios": [], "lab": []}
for i in range(3):
    r = read(f"factor_{i}_evaluate")
    summary["factors"].append(
        {
            "expression": read(f"factor_{i}_validate").get("expression"),
            "summary": r["summary"],
            "admitted": r["admitted"],
            "panel": r["panel"],
        }
    )
for kind in ["ridge", "gbdt"]:
    r = read(f"model_{kind}_report")["report"]
    summary["models"].append(
        {
            "kind": kind,
            "summary": r["summary"],
            "baseline": r["baseline"],
            "checks": r["checks"],
            "admitted": r["admitted"],
        }
    )
for i in range(2):
    for scale in [1, 3]:
        r = read(f"strategy_{i}_backtest_cost{scale}")
        m = r["result"]["metrics"]
        summary["strategies"].append(
            {
                "strategy": i,
                "cost_scale": scale,
                "metrics": {
                    k: m.get(k)
                    for k in [
                        "total_return",
                        "annualized_return",
                        "sharpe",
                        "max_drawdown",
                        "trades",
                        "fills",
                        "total_fees",
                        "simple_benchmark_return",
                        "beats_simple",
                    ]
                },
                "review_verdict": r["row"].get("review_verdict"),
            }
        )
for opt in ["equal", "risk_parity", "hrp"]:
    for cost in [10, 30]:
        r = read(f"corrected_portfolio_{opt}_{cost}bps")["report"]
        summary["portfolios"].append(
            {
                "optimizer": opt,
                "cost_bps": cost,
                "summary": r["summary"],
                "constraints": r["config"]["constraints"],
                "admitted": r["admitted"],
            }
        )
for kind in ["factor", "model", "portfolio", "strategy"]:
    r = read(f"lab_{kind}_snapshot")
    project = r["project"]
    messages = r["messages"]
    summary["lab"].append(
        {
            "kind": kind,
            "id": project["id"],
            "status": project["status"],
            "versions": project.get("current_v"),
            "tool_events": sum(m["role"] == "tool" for m in messages),
            "seconds": json.loads((p / f"lab_{kind}_chat.json").read_text(encoding="utf-8"))["seconds"],
            "saves": [m.get("data") for m in messages if m.get("kind") == "saved"],
            "errors": [m["content"] for m in messages if m.get("kind") == "error"],
        }
    )
summary["usage"] = read("workspace_usage_snapshot")
dump("summary.json", summary)
artifacts = [f for f in p.glob("*.json") if f.name != "manifest.json"] + [p / "index.jsonl", *p.glob("*.md")]
for f in artifacts:
    text = f.read_text(encoding="utf-8")
    assert not re.search(r"oc_sk_[A-Za-z0-9_]{15,}", text), f"Credential found in {f.name}"
    if f.suffix == ".json":
        json.loads(text)
for document in p.glob("*.md"):
    for target in re.findall(r"\]\(([^)]+)\)", document.read_text(encoding="utf-8")):
        if not target.startswith("http"):
            assert (p / target).exists(), target
for name in ["user_journey.py", "summarize_user_journey.py"]:
    ast.parse((Path(__file__).parent / name).read_text(encoding="utf-8"))
assert read("factor_future_guard")["ok"] is False
assert read("factor_refresh_before_promotion")["report"]["admitted"] is False
assert read("factor_active_gate")["factor"]["status"] == "active"
assert read("factor_restore_candidate")["factor"]["status"] == "candidate"
assert all(x["constraints"]["max_weight"] == 0.3 for x in summary["portfolios"])
dump(
    "manifest.json",
    {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "python": sys.version,
        "model": "deepseek-v4.1-flash",
        "provider_base": "https://opencode.ai/zen/go/v1",
        "mode": "in-process FastAPI TestClient, real provider, real exchange, isolated workspace, no UI automation",
        "runtime_note": "AppServices default path; QAGENT_AGENT_RUNTIME not enabled by this harness; no durable-runtime claims",
        "metric_note": "factor/model ls_return is ANNUALIZED AFTER COST; strategy total_return is cumulative",
        "evidence_notes": [
            "Original lab_portfolio_final.json was overwritten by inventory due to a harness filename collision; use lab_portfolio_snapshot.json and lab_portfolio_chat.json.",
            "Initial portfolio payload put constraints at the wrong nesting level. Use corrected_portfolio_* for the 30% constraint evaluation; original saved schemes retain defaults.",
            "Factor library baseline saves used evaluate=false; the separate factor_*_evaluate evidence is not attached to these initial candidates. Refresh probe attaches a real report.",
            "Direct-API end is frozen; lab end is null/current and may differ by one candle. Do not compare as identical datasets.",
            "One sandbox snapshot process was canceled before any snapshot output; the successful snapshot ran with network-approved execution.",
        ],
        "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(artifacts)},
    },
)
print(json.dumps({"evidence_files": len(artifacts), "lab": summary["lab"]}, ensure_ascii=False, indent=2))
