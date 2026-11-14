"""End-to-end over real HTTP: uvicorn on a free port, the Python SDK, real Docker."""

import socket
import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from cellblock.api import create_app
from cellblock.backends.docker import DockerBackend
from cellblock.config import Settings
from cellblock_sdk import ApiError, Cellblock, Sandbox

pytestmark = pytest.mark.docker


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    settings = Settings(home=tmp_path_factory.mktemp("home"), pool_size=1, reaper_interval_s=0.2,
                        instance=f"live{uuid.uuid4().hex[:6]}")
    backend = DockerBackend(instance=settings.instance)
    app = create_app(settings, backend)
    key = app.state.service.create_key("live")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/healthz").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    yield {"base": base, "key": key, "app": app}
    server.should_exit = True
    thread.join(15)
    for sandbox_id in list(backend.list_managed()):
        backend.destroy(sandbox_id)


@pytest.fixture
def client(live):
    c = Cellblock(base_url=live["base"], api_key=live["key"])
    yield c
    c.close()


def test_context_manager_runs_code_then_deletes(client):
    with Sandbox(client=client) as sb:
        result = sb.run_code("print(6 * 7)")
        assert result.ok and result.stdout == "42\n"
        sandbox_id = sb.id
    with pytest.raises(ApiError) as gone:
        Sandbox.connect(sandbox_id, client=client)
    assert gone.value.status == 404


def test_streaming_callbacks_deliver_chunks_before_exit(client):
    with Sandbox(client=client) as sb:
        seen = []
        start = time.monotonic()
        stamps = []

        def on_stdout(chunk: str) -> None:
            seen.append(chunk)
            stamps.append(time.monotonic() - start)

        result = sb.exec("for i in 1 2 3; do echo line$i; sleep 0.3; done", on_stdout=on_stdout)
        total = time.monotonic() - start
    assert "".join(seen) == "line1\nline2\nline3\n" == result.stdout
    assert stamps[0] < total - 0.5, "first chunk must arrive while the process still runs"


def test_timeout_and_kill_through_sdk(client):
    with Sandbox(client=client) as sb:
        assert sb.exec("sleep 60", timeout=2).timed_out is True
        outcome = {}
        runner = threading.Thread(target=lambda: outcome.update(r=sb.exec("sleep 60", timeout=60)))
        runner.start()
        running = []
        for _ in range(50):
            running = [e for e in sb.execs() if e["status"] == "running"]
            if running:
                break
            time.sleep(0.1)
        sb.kill_exec(running[0]["id"])
        runner.join(20)
        assert outcome["r"].exit_code != 0 and outcome["r"].timed_out is False


def test_files_roundtrip_and_binary_upload(client):
    with Sandbox(client=client) as sb:
        sb.files.write("notes/a.txt", "hello")
        blob = bytes(range(256))
        sb.files.write("bin/blob.dat", blob)
        assert sb.files.read_text("notes/a.txt") == "hello"
        assert sb.files.read("bin/blob.dat") == blob
        assert {e["name"] for e in sb.files.list("notes")} == {"a.txt"}
        with pytest.raises(ApiError) as escape:
            sb.files.read("../../etc/passwd")
        assert escape.value.status == 400


def test_snapshot_restore_through_sdk(client):
    with Sandbox(client=client) as src:
        src.files.write("project/app.py", "print('restored')")
        template = src.snapshot("baseline")
    with Sandbox(client=client, snapshot_id=template["id"]) as dst:
        assert dst.run_code("import runpy; runpy.run_path('/workspace/project/app.py')").stdout == "restored\n"


def test_bad_key_is_rejected(live):
    with pytest.raises(ApiError) as denied:
        Sandbox(client=Cellblock(base_url=live["base"], api_key="cb_nope"))
    assert denied.value.status == 401


def test_validation_rejects_out_of_range_limits(live):
    headers = {"Authorization": f"Bearer {live['key']}"}
    bad = httpx.post(f"{live['base']}/v1/sandboxes", json={"cpu": 99}, headers=headers)
    assert bad.status_code == 422
    bad_image = httpx.post(f"{live['base']}/v1/sandboxes", json={"image": "evil;rm -rf /"}, headers=headers)
    assert bad_image.status_code == 422


def test_ttl_reaper_runs_in_background(client):
    sb = Sandbox(client=client, ttl_seconds=1)
    sandbox_id = sb.id
    for _ in range(100):
        try:
            Sandbox.connect(sandbox_id, client=client)
        except ApiError:
            return
        time.sleep(0.1)
    pytest.fail("background reaper did not remove the expired sandbox")


def test_warm_pool_is_filled_and_reported(live):
    for _ in range(50):
        if httpx.get(f"{live['base']}/healthz").json()["pool_ready"] >= 1:
            return
        time.sleep(0.2)
    pytest.fail("warm pool never became ready")
