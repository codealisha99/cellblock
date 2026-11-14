"""Keyless end-to-end demo: start cellblock on :4300, run the agent example through the SDK,
then measure cold vs warm sandbox create latency. Cleans up its server, containers, and state."""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
import json

PORT = int(os.environ.get("CELLBLOCK_PORT", "4300"))
BASE = f"http://127.0.0.1:{PORT}"
INSTANCE = "demo"
SAMPLES = 3


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


def main() -> None:
    home = tempfile.mkdtemp(prefix="cellblock-demo-")
    env = {**os.environ, "CELLBLOCK_HOME": home, "CELLBLOCK_INSTANCE": INSTANCE,
           "CELLBLOCK_POOL_SIZE": "4", "CELLBLOCK_PORT": str(PORT)}
    server = None
    try:
        key = subprocess.run([sys.executable, "-m", "cellblock.cli", "key", "create", "demo"],
                             env=env, capture_output=True, text=True, check=True).stdout.strip().splitlines()[-1]
        server = subprocess.Popen([sys.executable, "-m", "cellblock.cli", "serve", "--port", str(PORT)],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: healthz()["status"] == "ok", 30, "server health")
        wait_for(lambda: healthz()["pool_ready"] >= 1, 120, "warm pool")
        print(f"server up on :{PORT}; warm pool ready = {healthz()['pool_ready']}\n")

        agent_env = {**os.environ, "CELLBLOCK_URL": BASE, "CELLBLOCK_API_KEY": key}
        print("== agent example (scripted model, real sandboxes) ==")
        subprocess.run([sys.executable, "-m", "examples.agent.run"], env=agent_env, check=True)

        print("\n== sandbox create latency (client-measured, 3 samples each) ==")
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from cellblock_sdk import Cellblock, Sandbox

        client = Cellblock(base_url=BASE, api_key=key)
        cold = measure(client, Sandbox, cold=True)
        warm = measure(client, Sandbox, cold=False)
        report(cold, warm)
        client.close()
    finally:
        if server is not None:
            server.terminate()  # graceful: lifespan shutdown removes unclaimed pool containers
            server.wait(timeout=30)
        leftover = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=cellblock.instance={INSTANCE}"],
                                  capture_output=True, text=True).stdout.split()
        for container_id in leftover:  # backstop: the server should already have removed these
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        shutil.rmtree(home, ignore_errors=True)
        print(f"\ncleanup: server stopped, state removed, leftover containers removed = {len(leftover)}")


def measure(client, Sandbox, cold: bool) -> list[float]:
    times = []
    for _ in range(SAMPLES):
        if not cold:
            wait_for(lambda: healthz()["pool_ready"] >= 1, 60, "pool refill")
        kwargs = {"cpu": 0.5} if cold else {}  # cpu=0.5 is off the default profile, so it cannot use the pool
        started = time.perf_counter()
        sb = Sandbox(client=client, **kwargs)
        times.append((time.perf_counter() - started) * 1000)
        sb.delete()
    return times


def report(cold: list[float], warm: list[float]) -> None:
    print(f"{'path':<6} {'median ms':>10} {'min ms':>8} {'max ms':>8}   samples")
    for label, xs in (("cold", cold), ("warm", warm)):
        print(f"{label:<6} {statistics.median(xs):>10.1f} {min(xs):>8.1f} {max(xs):>8.1f}   "
              + ", ".join(f"{x:.1f}" for x in xs))
    print(f"warm pool is {statistics.median(cold) / max(statistics.median(warm), 0.1):.0f}x faster at create")


if __name__ == "__main__":
    main()
