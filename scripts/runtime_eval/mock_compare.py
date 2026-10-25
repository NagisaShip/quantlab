"""Zero-provider-latency loop overhead, including native HTTP serialization."""

from __future__ import annotations

import argparse
import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from live_compare import LEGACY, Trial, langgraph_run, legacy_class, pydantic_run, quantlab_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    trials = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            trial = trials[self.path.split("/")[1]]
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            completed = {m.get("tool_call_id") for m in body["messages"] if m["role"] == "tool"}
            names = {
                call["function"]["name"]
                for m in body["messages"]
                if m["role"] == "assistant"
                for call in m.get("tool_calls", [])
                if call["id"] in completed
            }
            if "read_fixture" not in names:
                message = {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "fixed",
                    "tool_calls": [
                        {
                            "id": "read1",
                            "type": "function",
                            "function": {"name": "read_fixture", "arguments": '{"name":"ledger"}'},
                        }
                    ],
                }
            elif "calculator" not in names:
                message = {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "fixed",
                    "tool_calls": [
                        {
                            "id": "calc1",
                            "type": "function",
                            "function": {"name": "calculator", "arguments": '{"expression":"1250-12.5-340-4.5+90-3"}'},
                        }
                    ],
                }
            else:
                message = {
                    "role": "assistant",
                    "content": '{"net_profit":980,"ending_nav":10980,"return_pct":9.8,"evidence":"ledger"}',
                }
            raw = json.dumps(
                {
                    "id": "fixed",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek-v4.1-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                }
            ).encode()
            trial.requests.append(1)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    legacy = legacy_class(args.output)
    frameworks = ["legacy", "quantlab", "langgraph", "langgraph_sqlite", "pydantic"]
    rows = []
    rng = random.Random(112)
    try:
        for repeat in range(-2, args.repeats):
            order = frameworks.copy()
            rng.shuffle(order)
            for framework in order:
                trial = Trial(f"mock-{repeat}-{framework}")
                trials[trial.id] = trial
                folder = args.output / trial.id
                folder.mkdir(exist_ok=True)
                url = f"http://127.0.0.1:{server.server_port}/{trial.id}/v1"
                started = time.perf_counter()
                error = None
                try:
                    task = {"prompt": "Read ledger and calculate net profit with calculator. Return JSON."}
                    if framework in {"legacy", "quantlab"}:
                        output, state = quantlab_run(
                            trial, task, url, folder, legacy if framework == "legacy" else None
                        )
                    elif framework.startswith("langgraph"):
                        output, state = langgraph_run(trial, task, url, folder, framework == "langgraph_sqlite")
                    else:
                        output, state = pydantic_run(trial, task, url)
                    passed = (
                        json.loads(output)["net_profit"] == 980
                        and len(trial.requests) == 3
                        and len(trial.tools) == 2
                        and state == "done"
                    )
                except Exception as exc:
                    passed = False
                    error = type(exc).__name__ + ": " + str(exc)[:200]
                rows.append(
                    {
                        "framework": framework,
                        "repeat": repeat,
                        "warmup": repeat < 0,
                        "ms": (time.perf_counter() - started) * 1000,
                        "passed": passed,
                        "requests": len(trial.requests),
                        "error": error,
                    }
                )
    finally:
        server.shutdown()
        server.server_close()
    (args.output / "results.json").write_text(
        json.dumps(
            {"legacy_commit": LEGACY, "provider": "local deterministic mock", "external_cost": 0, "rows": rows},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"trials": len(rows), "failures": sum(not r["passed"] for r in rows)}))


if __name__ == "__main__":
    main()
