"""Docker backend: hardened containers. See README for what this does and does not isolate."""

from __future__ import annotations

import re
import socket
from collections.abc import Iterable, Iterator

import docker
from docker.errors import ImageNotFound, NotFound
from docker.types import Ulimit

from cellblock.backends.base import SandboxSpec
from cellblock.paths import WORKSPACE

SANDBOX_UID = 1000
LABEL_MANAGED = "cellblock.managed"
LABEL_SANDBOX = "cellblock.sandbox_id"
LABEL_INSTANCE = "cellblock.instance"
TMP_OPTIONS = "rw,noexec,nosuid,nodev,size=64m,mode=1777"
# Resolves a path inside the container and reports whether it is a file, dir, or missing.
_RESOLVE_SCRIPT = (
    'p=$(realpath -m -- "$1") || exit 2; printf "%s\\n" "$p"; '
    'if [ -d "$1" ]; then echo dir; elif [ -e "$1" ]; then echo file; else echo none; fi'
)
# Each exec runs as its own session, so its process group id is the pid of the `setsid` child.
# The group id is written to a tmpfs file so a later kill needs only shell builtins. Builtins
# matter: once a fork bomb has eaten the pids cgroup, forking `grep` or `cat` would fail.
_WRAPPER = (
    'f=/tmp/cb-exec-$CELLBLOCK_EXEC.pgid; '
    'setsid timeout -k 2 "$0" "$@" & pg=$!; echo "$pg" > "$f"; '
    'wait "$pg"; rc=$?; kill -s KILL -- "-$pg" 2>/dev/null; rm -f "$f"; exit $rc'
)
_KILL = 'read pg < "/tmp/cb-exec-$1.pgid" && kill -s KILL -- "-$pg"'
# Names the host by loopback inside the container. Without this, a network=true sandbox can reach the
# control plane at host.docker.internal. Pinned to the container's own loopback, the name resolves to
# nothing listening. The raw host gateway IP is still reachable; only a firewall rule closes that.
HOST_ALIASES = {"host.docker.internal": "127.0.0.1", "gateway.docker.internal": "127.0.0.1"}
EXEC_ID_RE = re.compile(r"^ex_[0-9a-f]{16}$")


def container_name(sandbox_id: str) -> str:
    return f"cellblock-{sandbox_id}"


class DockerBackend:
    def __init__(self, client: docker.DockerClient | None = None, instance: str = "default") -> None:
        self.client = client or docker.from_env()
        self.instance = instance

    def _container(self, sandbox_id: str):
        return self.client.containers.get(container_name(sandbox_id))

    def _ensure_image(self, image: str) -> None:
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

    def create(self, sandbox_id: str, spec: SandboxSpec) -> None:
        self._ensure_image(spec.image)
        mem = f"{spec.memory_mb}m"
        container = self.client.containers.create(
            spec.image,
            command=["sleep", "infinity"],
            name=container_name(sandbox_id),
            labels={LABEL_MANAGED: "true", LABEL_INSTANCE: self.instance, LABEL_SANDBOX: sandbox_id},
            user=f"{SANDBOX_UID}:{SANDBOX_UID}",
            working_dir=WORKSPACE,
            environment={"HOME": WORKSPACE},
            init=True,  # tini reaps orphaned grandchildren, which would otherwise eat the pids budget
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            network_mode="bridge" if spec.network else "none",
            mem_limit=mem,
            memswap_limit=mem,  # no swap, so the memory limit is a hard ceiling
            nano_cpus=int(spec.cpu * 1e9),
            pids_limit=spec.pids,
            tmpfs={
                WORKSPACE: f"rw,nosuid,nodev,size={spec.workspace_mb}m,uid={SANDBOX_UID},gid={SANDBOX_UID},mode=0755",
                "/tmp": TMP_OPTIONS,
            },
            ulimits=[Ulimit(name="nofile", soft=1024, hard=1024)],
            extra_hosts=HOST_ALIASES,
        )
        container.start()

    def destroy(self, sandbox_id: str) -> None:
        try:
            self._container(sandbox_id).remove(force=True)
        except NotFound:
            pass

    def pause(self, sandbox_id: str) -> None:
        self._container(sandbox_id).pause()

    def resume(self, sandbox_id: str) -> None:
        self._container(sandbox_id).unpause()

    def list_managed(self) -> dict[str, str]:
        labels = [f"{LABEL_MANAGED}=true", f"{LABEL_INSTANCE}={self.instance}"]
        containers = self.client.containers.list(all=True, filters={"label": labels})
        return {c.labels[LABEL_SANDBOX]: c.status for c in containers}

    def exec_stream(
        self, sandbox_id: str, exec_id: str, argv: list[str], timeout_s: int
    ) -> Iterator[tuple[str, bytes]]:
        cmd = ["sh", "-c", _WRAPPER, str(timeout_s), *argv]
        api = self.client.api
        created = api.exec_create(
            container_name(sandbox_id), cmd, stdout=True, stderr=True,
            environment={"CELLBLOCK_EXEC": exec_id}, workdir=WORKSPACE,
        )
        for stdout, stderr in api.exec_start(created["Id"], stream=True, demux=True):
            if stdout:
                yield "stdout", stdout
            if stderr:
                yield "stderr", stderr
        yield "exit", int(api.exec_inspect(created["Id"])["ExitCode"] or 0)

    def kill_exec(self, sandbox_id: str, exec_id: str) -> None:
        if not EXEC_ID_RE.match(exec_id):
            raise ValueError("malformed exec id")
        self._container(sandbox_id).exec_run(["sh", "-c", _KILL, "cb-kill", exec_id])

    def _run(self, sandbox_id: str, argv: list[str]) -> bytes:
        code, output = self._container(sandbox_id).exec_run(argv, demux=False)
        if code != 0:
            raise RuntimeError(f"in-container command failed ({code}): {argv[0]}")
        return output or b""

    def resolve(self, sandbox_id: str, path: str) -> tuple[str, str]:
        lines = self._run(sandbox_id, ["sh", "-c", _RESOLVE_SCRIPT, "cb-resolve", path]).decode().splitlines()
        return lines[0], lines[1]

    def list_dir(self, sandbox_id: str, path: str) -> list[tuple[str, int, str]]:
        out = self._run(sandbox_id, ["find", path, "-mindepth", "1", "-maxdepth", "1", "-printf", "%y\t%s\t%f\n"])
        entries = (line.split("\t", 2) for line in out.decode().splitlines() if line)
        return [(kind, int(size), name) for kind, size, name in entries]

    def mkdir(self, sandbox_id: str, path: str) -> None:
        self._run(sandbox_id, ["mkdir", "-p", "--", path])

    def read_file(self, sandbox_id: str, path: str) -> Iterator[bytes]:
        # put/get_archive cannot see tmpfs mounts (where /workspace lives), so go through exec.
        # Streamed so a large file never sits whole in control-plane memory. Exit status is not
        # checked here; the service has already confirmed the file exists.
        _, output = self._container(sandbox_id).exec_run(["cat", "--", path], demux=False, stream=True)
        return output

    def write_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self._exec_with_stdin(sandbox_id, ["sh", "-c", 'cat > "$1"', "cb-write", path], data)

    def snapshot_workspace(self, sandbox_id: str) -> Iterator[bytes]:
        argv = ["tar", "-C", WORKSPACE, "-cf", "-", "."]
        api = self.client.api
        created = api.exec_create(container_name(sandbox_id), argv, stdout=True, stderr=True)
        for stdout, _stderr in api.exec_start(created["Id"], stream=True, demux=True):
            if stdout:
                yield stdout
        code = api.exec_inspect(created["Id"])["ExitCode"]
        if code != 0:
            raise RuntimeError(f"snapshot failed (exit {code})")

    def restore_workspace(self, sandbox_id: str, tar_chunks: Iterable[bytes]) -> None:
        self._exec_with_stdin(sandbox_id, ["tar", "-C", WORKSPACE, "-xf", "-"], tar_chunks)

    def _exec_with_stdin(self, sandbox_id: str, argv: list[str], data: bytes | Iterable[bytes]) -> None:
        """Pipe `data` (one buffer or a stream of chunks) to a command's stdin and fail if it exits non-zero."""
        api = self.client.api
        created = api.exec_create(container_name(sandbox_id), argv, stdin=True, stdout=True, stderr=True)
        sock = api.exec_start(created["Id"], socket=True)
        raw = getattr(sock, "_sock", sock)
        try:
            for chunk in ([data] if isinstance(data, bytes) else data):
                raw.sendall(chunk)
            raw.shutdown(socket.SHUT_WR)  # EOF tells the command its input is complete
            while raw.recv(65536):
                pass
        finally:
            raw.close()
        code = api.exec_inspect(created["Id"])["ExitCode"]
        if code != 0:
            raise RuntimeError(f"in-container write failed ({code}): {argv[0]}")
