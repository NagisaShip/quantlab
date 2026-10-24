"""Standalone host: python minimal_cli.py DB start|resume [RUN_ID]."""

import argparse
import json

from agent_runtime import AgentRuntime, Journal
from agent_runtime.contracts import AgentSpec, ModelResult, ToolCall, ToolSpec


class DemoModel:
    def generate(self, messages, *, request_id):
        if any(message["role"] == "tool" for message in messages):
            return ModelResult(text="The answer is " + messages[-1]["content"], usage_micro=1)
        return ModelResult(tool_call=ToolCall("calculator", {"a": 2, "b": 3}), usage_micro=1)


class DemoTools:
    def execute(self, call, *, effect_id):
        return call.arguments["a"] + call.arguments["b"]

    def reconcile(self, call, *, effect_id):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("action", choices=("start", "resume"))
    parser.add_argument("run_id", nargs="?")
    args = parser.parse_args()
    runtime = AgentRuntime(Journal(args.database), DemoModel(), DemoTools())
    if args.action == "start":
        run_id = runtime.start(
            "Calculate 2 + 3", AgentSpec("cli-demo", "1", tools=(ToolSpec("calculator", requires_approval=True),))
        )
        result = runtime.resume(run_id)
    else:
        if not args.run_id:
            parser.error("resume requires RUN_ID")
        run_id = args.run_id
        waiting = next(step for step in runtime.get(run_id)["steps"] if step["state"] == "WAITING_APPROVAL")
        digest = next(
            event["data"]["arguments_sha256"]
            for event in runtime.events(run_id)
            if event["type"] == "approval.required" and event["data"]["step_id"] == waiting["id"]
        )
        runtime.signal(
            run_id,
            "approval",
            {"step_id": waiting["id"], "approved": True, "arguments_sha256": digest},
            signal_id="cli-approval-" + waiting["id"],
            actor="cli",
        )
        result = runtime.resume(run_id)
    print(json.dumps({"run_id": run_id, "state": result["state"], "output": result["output"]}))


if __name__ == "__main__":
    main()
