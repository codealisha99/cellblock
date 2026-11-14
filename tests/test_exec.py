import threading
import time

import pytest

from cellblock.backends.base import SandboxSpec
from cellblock.service import collect

pytestmark = pytest.mark.docker


def _spec(**over):
    base = dict(image="python:3.12-slim", cpu=1.0, memory_mb=512, pids=256, network=False, workspace_mb=64)
    return SandboxSpec(**{**base, **over})


def _sandbox(service, key, **over):
    return service.create_sandbox(key, _spec(**over), ttl_s=600)["id"]


def test_stream_preserves_order_and_arrives_before_exit(service, key):
    sid = _sandbox(service, key)
    script = "for i in 1 2 3; do echo out$i; echo err$i >&2; sleep 0.3; done"
    seen, first_at = [], None
    started = time.monotonic()
    for ev in service.exec_stream(key, sid, ["sh", "-c", script], 20):
        if ev["type"] in ("stdout", "stderr"):
            seen.append((ev["type"], ev["data"].strip()))
            first_at = first_at or time.monotonic() - started
        if ev["type"] == "exit":
            assert ev["exit_code"] == 0
            total = time.monotonic() - started
    assert [t for t in seen if t[0] == "stdout"] == [("stdout", f"out{i}") for i in (1, 2, 3)]
    assert [t for t in seen if t[0] == "stderr"] == [("stderr", f"err{i}") for i in (1, 2, 3)]
    assert first_at < total - 0.5, "first chunk should arrive while the command is still running"


def test_exit_code_and_stderr_are_reported(service, key):
    sid = _sandbox(service, key)
    result = collect(service.exec_stream(key, sid, ["sh", "-c", "echo boom >&2; exit 7"], 20))
    assert (result["exit_code"], result["stderr"], result["timed_out"]) == (7, "boom\n", False)


def test_timeout_kills_command_and_its_children(service, key):
    sid = _sandbox(service, key)
    started = time.monotonic()
    result = collect(service.exec_stream(
        key, sid, ["sh", "-c", "sleep 300 & echo $! > /workspace/child.pid; wait"], 2))
    assert time.monotonic() - started < 15
    assert result["timed_out"] is True and result["exit_code"] == 124
    child = b"".join(service.read_file(key, sid, "child.pid")).decode().strip()
    alive = collect(service.exec_stream(key, sid, ["sh", "-c", f"kill -0 {child} 2>/dev/null && echo alive || echo dead"], 10))
    assert alive["stdout"].strip() == "dead"


def test_kill_from_another_request(service, key):
    sid = _sandbox(service, key)
    events = service.exec_stream(key, sid, ["sleep", "300"], 60)
    exec_id = next(events)["exec_id"]
    done = {}
    reader = threading.Thread(target=lambda: done.update(result=collect(events)))
    reader.start()
    time.sleep(0.5)
    service.kill_exec(key, sid, exec_id)
    reader.join(timeout=15)
    assert not reader.is_alive()
    assert done["result"]["exit_code"] != 0 and done["result"]["timed_out"] is False
    assert service.list_execs(key, sid)[0]["status"] == "finished"


def test_run_code_python_and_history(service, key):
    sid = _sandbox(service, key)
    result = collect(service.run_code(key, sid, "python", "print(sum(range(10)))", 20))
    assert result["stdout"] == "45\n" and result["exit_code"] == 0
    assert service.list_execs(key, sid)[0]["stdout"] == "45\n"


def test_program_exiting_124_on_its_own_is_not_a_timeout(service, key):
    sid = _sandbox(service, key)
    result = collect(service.exec_stream(key, sid, ["sh", "-c", "exit 124"], 20))
    assert (result["exit_code"], result["timed_out"]) == (124, False)


def test_run_code_node_on_node_image(service, key):
    sid = _sandbox(service, key, image="node:22-alpine")
    result = collect(service.run_code(key, sid, "node", "console.log(6 * 7)", 20))
    assert result["stdout"] == "42\n" and result["exit_code"] == 0
