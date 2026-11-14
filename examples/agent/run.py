"""Tool-use loop where the sandbox is the model's code interpreter.

Run keyless (scripted model):   uv run python -m examples.agent.run
Run with Anthropic:             ANTHROPIC_API_KEY=... uv run --extra examples python -m examples.agent.run
"""

from __future__ import annotations

import json
from typing import Any

from cellblock_sdk import Cellblock, Sandbox
from examples.agent.model import Model, make_model

RUN_PYTHON = {
    "name": "run_python",
    "description": "Run Python code in an isolated sandbox (no network, 512 MB, 1 CPU). Returns stdout, stderr, exit_code.",
    "input_schema": {
        "type": "object",
        "properties": {"code": {"type": "string", "description": "Complete Python 3 program."}},
        "required": ["code"],
        "additionalProperties": False,
    },
}
MAX_TURNS = 8
QUESTION = "What is the sum of the squares of 1..10? Then check if you can reach the internet."


def execute_tool(sandbox: Sandbox, name: str, tool_input: dict[str, Any]) -> str:
    if name != "run_python":
        return json.dumps({"error": f"unknown tool {name}"})
    result = sandbox.run_code(str(tool_input.get("code", "")), language="python", timeout=20)
    return json.dumps({"stdout": result.stdout, "stderr": result.stderr,
                       "exit_code": result.exit_code, "timed_out": result.timed_out})


def run_agent(model: Model, sandbox: Sandbox, question: str, log=print) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    for _ in range(MAX_TURNS):
        turn = model.respond(messages, [RUN_PYTHON])
        messages.append({"role": "assistant", "content": turn["content"]})
        calls = [b for b in turn["content"] if b["type"] == "tool_use"]
        if turn["stop_reason"] != "tool_use" or not calls:
            return "".join(b.get("text", "") for b in turn["content"] if b["type"] == "text")
        results = []
        for call in calls:  # all results go back in ONE user message
            first_line = (call["input"].get("code") or "").splitlines()[:1] or [""]
            log(f"-> tool {call['name']}: {first_line[0][:60]}")
            output = execute_tool(sandbox, call["name"], call["input"])
            log(f"<- {output}")
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": output})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("agent did not finish within MAX_TURNS")


def main() -> None:
    model = make_model()
    print(f"model: {type(model).__name__}")
    with Sandbox(client=Cellblock()) as sandbox:
        answer = run_agent(model, sandbox, QUESTION)
    print(f"answer: {answer}")


if __name__ == "__main__":
    main()
