"""An independent host that resumes a durable fork/join across CLI processes."""

import argparse
import json

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult


class Model:
    def generate(self, messages, *, request_id):
        return ModelResult(text="completed: " + messages[0]["content"], usage_micro=1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--parent")
    parser.add_argument("--pause-after-first", action="store_true")
    args = parser.parse_args()
    runtime = AgentRuntime(Journal(args.database), Model(), None)
    parent = args.parent
    if parent is None:
        parent = runtime.start(
            "summarize children", AgentSpec("parent", "1", budget_limit_micro=3, model_reservation_micro=1)
        )
        child_spec = AgentSpec("child", "1", model_reservation_micro=1)
        runtime.fork_children(
            parent, [("calculate A", child_spec), ("calculate B", child_spec)], idempotency_key="research"
        )
    for index, child in enumerate(runtime.children(parent)):
        runtime.resume(child)
        if args.pause_after_first and index == 0:
            print(json.dumps({"parent": parent, "state": runtime.resume(parent)["state"]}))
            return
    result = runtime.resume(parent)
    print(json.dumps({"parent": parent, "state": result["state"], "output": result.get("output")}))


if __name__ == "__main__":
    main()
