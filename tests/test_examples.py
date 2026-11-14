from cellblock_sdk.client import ExecResult
from examples.agent.model import ScriptedModel, text, tool_use
from examples.agent.run import run_agent


class FakeSandbox:
    def run_code(self, code, **_kwargs):
        return ExecResult(None, "", "", 0, False)


def test_agent_loop_survives_a_tool_call_with_empty_code():
    model = ScriptedModel([[tool_use("t1", "run_python", code="")], [text("done")]])
    assert run_agent(model, FakeSandbox(), "q", log=lambda *_: None) == "done"
