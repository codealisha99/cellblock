"""Record a real session against a live Docker-backed cellblock server, for the landing page.

Starts the control plane on :4300 with an isolated home, runs the SDK example, the containment
probes (fork bomb, OOM, network, path traversal, write outside the workspace), snapshot/restore,
and cold-vs-warm create latency. Writes what actually happened to site/data/. Nothing is scripted:
every line in the output is what the sandbox returned.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
PORT = int(os.environ.get("CELLBLOCK_PORT", "4300"))
BASE = f"http://127.0.0.1:{PORT}"
INSTANCE = "record"
LATENCY_SAMPLES = 20
POOL_SIZE = 4

sys.path.insert(0, str(ROOT / "src"))
from cellblock_sdk import ApiError, Cellblock, Sandbox  # noqa: E402


class Transcript:
    """Ordered terminal lines. `at_ms` is measured from the start of the recording."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self.t0 = time.perf_counter()

    def _at(self) -> int:
        return round((time.perf_counter() - self.t0) * 1000)

    def cmd(self, text: str) -> None:
        self.lines.append({"kind": "cmd", "text": text, "at_ms": self._at()})

    def out(self, text: str, stream: str = "stdout") -> None:
        for line in text.splitlines(keepends=True):
            self.lines.append({"kind": stream, "text": line, "at_ms": self._at()})

    def note(self, text: str) -> None:
        self.lines.append({"kind": "note", "text": text, "at_ms": self._at()})

    def exit(self, code: int | None, timed_out: bool) -> None:
        self.lines.append({"kind": "exit", "code": code, "timed_out": timed_out, "at_ms": self._at()})


def healthz() -> dict:
    with urllib.request.urlopen(f"{BASE}/healthz", timeout=2) as r:
        return json.loads(r.read())


def wait_for(predicate, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise SystemExit(f"timed out waiting for {what}")


def run_probe(tr: Transcript, sb: Sandbox, label: str, command: str, timeout: int = 30) -> None:
    if label:
        tr.note(label)
    tr.cmd(command)
    res = sb.exec(command, timeout=timeout)
    tr.out(res.stdout)
    tr.out(res.stderr, stream="stderr")
    tr.exit(res.exit_code, res.timed_out)


def scenario_sdk_streaming(tr: Transcript, client: Cellblock) -> None:
    tr.note("SDK: create a sandbox, run code, then stream a command as it runs")
    tr.cmd("with Sandbox(memory_mb=256, ttl_seconds=600) as sb:")
    with Sandbox(client=client, memory_mb=256, ttl_seconds=600) as sb:
        tr.cmd('    print(sb.run_code("print(sum(range(10)))").stdout)')
        res = sb.run_code("print(sum(range(10)))")
        tr.out(res.stdout)
        tr.cmd('    sb.exec("for i in 1 2 3; do echo step $i; sleep 0.5; done", on_stdout=print)')
        streamed = sb.exec("for i in 1 2 3; do echo step $i; sleep 0.5; done",
                           on_stdout=lambda chunk: tr.out(chunk))
        tr.exit(streamed.exit_code, streamed.timed_out)
        tr.note("containment probes, each in a fresh call to the same sandbox")
        probes(tr, sb)
        tr.note("snapshot /workspace, then restore it into a new sandbox")
        tr.cmd('    template = sb.snapshot("with-app")')
        tr.cmd('    sb.files.write("app/main.py", "print(\'hi from the snapshot\')")')
        sb.files.write("app/main.py", "print('hi from the snapshot')")
        template = sb.snapshot("with-app")
        tr.out(f"template {template['id']}  size_bytes={template['size_bytes']}\n")
        tr.cmd('with Sandbox(snapshot_id=template["id"]) as sb2:')
        with Sandbox(client=client, snapshot_id=template["id"]) as sb2:
            tr.cmd('    print(sb2.files.read_text("app/main.py"))')
            tr.out(sb2.files.read_text("app/main.py") + "\n")
            tr.cmd('    sb2.files.list("/workspace")')
            tr.out(json.dumps(sb2.files.list("/workspace")) + "\n")


def probes(tr: Transcript, sb: Sandbox) -> None:
    run_probe(tr, sb, "fork bomb: pids cap stops it, then the sandbox still works",
              'while :; do sh -c "sleep 30" & done', timeout=5)
    run_probe(tr, sb, "", "echo still-alive")
    run_probe(tr, sb, "memory: allocate 400 MiB under a 256 MiB limit",
              "python3 -c \"x = bytearray(400 * 1024 * 1024); print('allocated')\"")
    run_probe(tr, sb, "network is disabled by default",
              "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)\" 2>&1; echo rc=$?")
    run_probe(tr, sb, "write outside the workspace is refused",
              "echo x > /usr/local/evil 2>&1; echo rc=$?; touch /etc/evil 2>&1; echo rc=$?")
    tr.note("path traversal through the file API is rejected before it reaches the container")
    tr.cmd('sb.files.read_text("../../etc/passwd")')
    try:
        sb.files.read_text("../../etc/passwd")
        tr.out("UNEXPECTED: read succeeded\n")
    except ApiError as exc:
        tr.out(f"{exc}\n", stream="stderr")


def scenario_network_probe(tr: Transcript, client: Cellblock) -> None:
    tr.note("a network=true sandbox tries the control plane by its host name")
    code = ("import urllib.request as u\n"
            "try:\n"
            "    print(u.urlopen('http://host.docker.internal:4300/healthz', timeout=3).status)\n"
            "except Exception as e:\n"
            "    print('blocked:', type(e).__name__, e)\n")
    for line in code.splitlines():
        tr.cmd(line)
    with Sandbox(client=client, network=True, ttl_seconds=300) as sb:
        res = sb.run_code(code)
        tr.out(res.stdout)
        tr.out(res.stderr, stream="stderr")
        tr.exit(res.exit_code, res.timed_out)


def measure(client: Cellblock, cold: bool) -> list[float]:
    times = []
    for _ in range(LATENCY_SAMPLES):
        if not cold:
            wait_for(lambda: healthz()["pool_ready"] >= 1, 120, "pool refill")
        kwargs = {"cpu": 0.5} if cold else {}  # cpu=0.5 is off the default profile, so it cannot claim a warm container
        started = time.perf_counter()
        sb = Sandbox(client=client, **kwargs)
        times.append((time.perf_counter() - started) * 1000)
        sb.delete()
    return times


def docker_version() -> str:
    out = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True)
    return out.stdout.strip() or "unknown"


def main() -> None:
    home = tempfile.mkdtemp(prefix="cellblock-record-")
    env = {**os.environ, "CELLBLOCK_HOME": home, "CELLBLOCK_INSTANCE": INSTANCE,
           "CELLBLOCK_POOL_SIZE": str(POOL_SIZE), "CELLBLOCK_PORT": str(PORT)}
    server = None
    try:
        key = subprocess.run([sys.executable, "-m", "cellblock.cli", "key", "create", "record"],
                             env=env, capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
        server = subprocess.Popen(
            [sys.executable, "-m", "cellblock.cli", "serve", "--host", "0.0.0.0", "--port", str(PORT)],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT)
        wait_for(lambda: healthz()["status"] == "ok", 30, "server health")
        wait_for(lambda: healthz()["pool_ready"] >= 1, 180, "warm pool")
        client = Cellblock(base_url=BASE, api_key=key)

        tr = Transcript()
        scenario_sdk_streaming(tr, client)
        scenario_network_probe(tr, client)
        tr_path = DATA / "session.json"
        DATA.mkdir(parents=True, exist_ok=True)
        tr_path.write_text(json.dumps({"docker": docker_version(), "lines": tr.lines}, indent=1))
        print(f"wrote {tr_path} ({len(tr.lines)} lines)")

        cold = measure(client, cold=True)
        warm = measure(client, cold=False)
        latency = {"docker": docker_version(), "samples": LATENCY_SAMPLES,
                   "cold_ms": [round(x, 1) for x in cold], "warm_ms": [round(x, 1) for x in warm],
                   "cold_median_ms": round(statistics.median(cold), 1),
                   "warm_median_ms": round(statistics.median(warm), 1)}
        (DATA / "latency.json").write_text(json.dumps(latency, indent=1))
        print(f"cold median {latency['cold_median_ms']} ms, warm median {latency['warm_median_ms']} ms "
              f"({LATENCY_SAMPLES} samples each)")
        client.close()
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=30)
        leftover = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=cellblock.instance={INSTANCE}"],
                                  capture_output=True, text=True).stdout.split()
        for container_id in leftover:
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        shutil.rmtree(home, ignore_errors=True)
        print(f"cleanup: leftover containers removed = {len(leftover)}")


if __name__ == "__main__":
    main()
