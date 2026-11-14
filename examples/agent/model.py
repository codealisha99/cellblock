"""One provider seam for the agent loop. Anthropic is the default; a scripted model replays a
fixed tool-use transcript so the example runs keyless and deterministically."""

from __future__ import annotations

import os
from typing import Any, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"  # overridable via CELLBLOCK_MODEL, per the project's build rules


class Model(Protocol):
    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Return {"stop_reason": str, "content": [block dicts]} for the next assistant turn."""
        ...


class AnthropicModel:
    def __init__(self, model: str | None = None) -> None:
        import anthropic  # optional dependency: `uv sync --extra examples`

        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("CELLBLOCK_MODEL", DEFAULT_MODEL)

    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        response = self._client.messages.create(
            model=self._model, max_tokens=2048, tools=tools, messages=messages,
        )
        return {
            "stop_reason": response.stop_reason,
            "content": [block.model_dump(exclude_none=True) for block in response.content],
        }


class ScriptedModel:
    """Replays canned assistant turns in order. The tool results it receives are ignored."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self._turns = list(turns)

    def respond(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if not self._turns:
            raise RuntimeError("scripted model ran out of turns")
        content = self._turns.pop(0)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return {"stop_reason": stop, "content": content}


def tool_use(block_id: str, name: str, **arguments: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": arguments}


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def make_model() -> Model:
    """Anthropic when a key is configured, otherwise the scripted demo transcript."""
    if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CELLBLOCK_MODEL_PROVIDER", "anthropic") == "anthropic":
        return AnthropicModel()
    from examples.agent.demo_script import SCRIPT

    return ScriptedModel(SCRIPT)
