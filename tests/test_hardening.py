import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cellblock.backends.base import SandboxSpec
from cellblock.service import collect

pytestmark = pytest.mark.docker


IFACES = "tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' '"  # per-netns view; /sys is host-wide


def _run(service, key, sid, script, timeout=20):
    return collect(service.exec_stream(key, sid, ["sh", "-c", script], timeout))


@pytest.fixture
def sb(service, key):
    spec = SandboxSpec(image="python:3.12-slim", cpu=1.0, memory_mb=128, pids=64, network=False, workspace_mb=16)
    return service.create_sandbox(key, spec, ttl_s=600)["id"]


def test_runs_as_unprivileged_user_with_no_capabilities(service, key, sb):
    out = _run(service, key, sb, "id -u; grep -E '^Cap(Eff|Prm|Bnd)' /proc/self/status; grep NoNewPrivs /proc/self/status")
    lines = out["stdout"].split("\n")
    assert lines[0] == "1000"
    assert all(line.endswith("0000000000000000") for line in lines[1:4])
    assert lines[4].split()[-1] == "1"


def test_root_filesystem_is_read_only_and_docker_socket_absent(service, key, sb):
    out = _run(service, key, sb, "touch /etc/pwned 2>&1; echo rc=$?; ls /var/run/docker.sock 2>&1; echo rc2=$?")
    assert "Read-only file system" in out["stdout"] and "rc=1" in out["stdout"]
    assert "rc2=2" in out["stdout"]


def test_network_is_unreachable_when_disabled(service, key, sb):
    script = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)\" 2>&1; echo rc=$?"
    out = _run(service, key, sb, script)
    assert "rc=1" in out["stdout"]
    assert "Network is unreachable" in out["stdout"] or "Errno" in out["stdout"]
    assert not [i for i in _run(service, key, sb, IFACES)["stdout"].split() if i.startswith("eth")]


def test_network_interface_exists_when_enabled(service, key):
    spec = SandboxSpec(image="python:3.12-slim", cpu=1.0, memory_mb=128, pids=64, network=True, workspace_mb=16)
    sid = service.create_sandbox(key, spec, ttl_s=600)["id"]
    assert "eth0" in _run(service, key, sid, IFACES)["stdout"].split()


def test_memory_limit_kills_allocation_without_swap(service, key, sb):
    out = _run(service, key, sb, "python3 -c \"x = bytearray(400 * 1024 * 1024); print('allocated')\"", timeout=30)
    assert out["exit_code"] == 137 and "allocated" not in out["stdout"]
    assert _run(service, key, sb, "echo still-alive")["stdout"] == "still-alive\n"


def test_fork_bomb_is_contained_by_pids_limit(service, key, sb):
    # Fork fails at the pids cap, so the bomb dies on its own (or is timed out). Either way it returns.
    bomb = 'while :; do sh -c "sleep 30" & done'
    out = _run(service, key, sb, bomb, timeout=5)
    assert out["exit_code"] is not None and "Cannot fork" in out["stderr"] + out["stdout"] or out["timed_out"]
    assert _run(service, key, sb, "echo still-alive")["stdout"] == "still-alive\n"


def test_workspace_is_size_limited(service, key, sb):
    out = _run(service, key, sb, "dd if=/dev/zero of=/workspace/big bs=1M count=32 2>&1; echo rc=$?")
    assert "No space left on device" in out["stdout"] or "rc=1" in out["stdout"]


def test_cannot_write_outside_workspace(service, key, sb):
    out = _run(service, key, sb, "echo x > /usr/local/evil 2>&1; echo rc=$?; echo y > /home/evil 2>&1; echo rc=$?")
    assert out["stdout"].count("rc=0") == 0


class _HostService(BaseHTTPRequestHandler):
    def do_GET(self):  # stands in for the control plane on the host
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"host-service")

    def log_message(self, *args):
        pass


def test_networked_sandbox_cannot_reach_host_service_by_name(service, key):
    server = ThreadingHTTPServer(("0.0.0.0", 0), _HostService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        spec = SandboxSpec(image="python:3.12-slim", cpu=1.0, memory_mb=128, pids=64, network=True, workspace_mb=16)
        sid = service.create_sandbox(key, spec, ttl_s=600)["id"]
        port = server.server_address[1]
        probe = (f"python3 -c \"import urllib.request as u; "
                 f"print(u.urlopen('http://host.docker.internal:{port}/', timeout=3).read().decode())\" 2>&1; echo rc=$?")
        out = _run(service, key, sid, probe)
        assert "host-service" not in out["stdout"] and "rc=0" not in out["stdout"]
    finally:
        server.shutdown()
