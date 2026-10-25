"""Produce inspectable summaries without repeating any provider calls."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import statistics
from pathlib import Path

from cases import grade


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = [
        json.loads(line) for line in (args.input / "live-final/results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    corrections = []
    for row in rows:
        row["validated_grade"] = grade(row["task"], row["output"], row["tool_calls"])
        if row["grade"]["passed"] != row["validated_grade"]["passed"]:
            corrections.append(row["id"])
    mock = json.loads((args.input / "mock-final-v2/results.json").read_text())["rows"]
    summaries = []
    for framework in ("legacy", "quantlab", "langgraph", "langgraph_sqlite", "pydantic"):
        subset = [r for r in rows if r["framework"] == framework]
        overhead = [(r["seconds"] - r["provider_seconds"]) * 1000 for r in subset]
        mock_rows = [r for r in mock if r["framework"] == framework and not r["warmup"]]
        n = len(subset)
        passes = sum(r["validated_grade"]["passed"] for r in subset)
        z = 1.96
        p = passes / n
        center = (p + z * z / (2 * n)) / (1 + z * z / n)
        radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        requests = [
            req
            for r in subset
            for req in json.loads((args.input / "live-final" / r["id"] / "trace.json").read_text(encoding="utf-8"))
        ]
        summaries.append(
            {
                "framework": framework,
                "n": n,
                "passed": passes,
                "raw_passed": sum(r["grade"]["passed"] for r in subset),
                "wilson_95": [round(center - radius, 4), round(center + radius, 4)],
                "per_task": {
                    task: {
                        "passed": sum(r["validated_grade"]["passed"] for r in subset if r["task"] == task),
                        "n": sum(r["task"] == task for r in subset),
                    }
                    for task in sorted({r["task"] for r in subset})
                },
                "wall_median_s": statistics.median(r["seconds"] for r in subset),
                "wall_max_s": max(r["seconds"] for r in subset),
                "host_overhead_median_ms": statistics.median(overhead),
                "requests": sum(r["requests"] for r in subset),
                "prompt_tokens": sum(r["prompt_tokens"] for r in subset),
                "completion_tokens": sum(r["completion_tokens"] for r in subset),
                "billed_usd": sum(r["billed_cost"] for r in subset),
                "list_cost_usd": sum(r["list_cost"] for r in subset),
                "allowance_usd": sum(
                    r["response"].get("usage", {}).get("opencode", {}).get("allowance", 0) for r in requests
                ),
                "mock_median_ms": statistics.median(r["ms"] for r in mock_rows),
                "mock_p95_ms": percentile([r["ms"] for r in mock_rows], 0.95),
                "mock_passed": sum(r["passed"] for r in mock_rows),
                "mock_n": len(mock_rows),
            }
        )
    details = {
        "summaries": summaries,
        "grading_corrections": corrections,
        "grading_sha256": hashlib.sha256((Path(__file__).parent / "cases.py").read_bytes()).hexdigest(),
        "counts": {
            "live_trials": len(rows),
            "live_provider_requests": sum(r["requests"] for r in rows),
            "mock_trials_including_warmups": len(mock),
        },
        "classification": dict(collections.Counter(r["error"].split(":")[0] for r in rows if r["error"])),
    }
    (args.output / "comparison-summary.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "validated-results.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "trials": len(rows),
                "corrections": corrections,
                "scores": [(s["framework"], s["passed"]) for s in summaries],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
