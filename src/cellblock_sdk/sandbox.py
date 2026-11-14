"""The Sandbox object: a context manager that owns a sandbox for the duration of a `with` block."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from cellblock_sdk.client import Cellblock, ExecResult

Callback = Callable[[str], None]


class Files:
    def __init__(self, sandbox: "Sandbox") -> None:
        self._sb = sandbox

    def write(self, path: str, data: bytes | str) -> dict[str, Any]:
        payload = data.encode() if isinstance(data, str) else data
        return self._sb._api.json(
            "PUT", f"/v1/sandboxes/{self._sb.id}/files/content", params={"path": path},
            content=payload, headers={"Content-Type": "application/octet-stream"},
        )

    def read(self, path: str) -> bytes:
        return self._sb._api.request("GET", f"/v1/sandboxes/{self._sb.id}/files/content", params={"path": path}).content

    def read_text(self, path: str) -> str:
        return self.read(path).decode()

    def list(self, path: str = "/workspace") -> list[dict[str, Any]]:
        return self._sb._api.json("GET", f"/v1/sandboxes/{self._sb.id}/files", params={"path": path})


class Sandbox:
    """A running sandbox. `Sandbox()` creates one; `Sandbox.connect(id)` attaches to an existing one."""

    def __init__(self, *, image: str | None = None, snapshot_id: str | None = None, cpu: float = 1.0,
                 memory_mb: int = 512, pids: int = 256, network: bool = False, workspace_mb: int = 256,
                 ttl_seconds: int = 900, client: Cellblock | None = None) -> None:
        self._api = client or Cellblock()
        body = {"image": image, "snapshot_id": snapshot_id, "cpu": cpu, "memory_mb": memory_mb, "pids": pids,
                "network": network, "workspace_mb": workspace_mb, "ttl_seconds": ttl_seconds}
        self._info: dict[str, Any] = self._api.json("POST", "/v1/sandboxes", json=body)
        self.files = Files(self)

    @classmethod
    def connect(cls, sandbox_id: str, client: Cellblock | None = None) -> "Sandbox":
        obj = cls.__new__(cls)
        obj._api = client or Cellblock()
        obj._info = obj._api.json("GET", f"/v1/sandboxes/{sandbox_id}")
        obj.files = Files(obj)
        return obj

    @property
    def id(self) -> str:
        return self._info["id"]

    @property
    def status(self) -> str:
        return self._info["status"]

    def refresh(self) -> dict[str, Any]:
        self._info = self._api.json("GET", f"/v1/sandboxes/{self.id}")
        return self._info

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc: object) -> None:
        self.delete()

    def exec(self, command: str | list[str], *, timeout: int = 30,
             on_stdout: Callback | None = None, on_stderr: Callback | None = None) -> ExecResult:
        """Run a shell string (`"ls -la"`) or an argv list. Pass callbacks to stream output as it arrives."""
        body = {"argv": command, "timeout_s": timeout} if isinstance(command, list) else \
               {"command": command, "timeout_s": timeout}
        path = f"/v1/sandboxes/{self.id}/exec"
        if on_stdout is None and on_stderr is None:
            return ExecResult(**_result_fields(self._api.json("POST", path, json=body)))
        return self._stream(f"{path}/stream", body, on_stdout, on_stderr)

    def run_code(self, code: str, *, language: str = "python", timeout: int = 30,
                 on_stdout: Callback | None = None, on_stderr: Callback | None = None) -> ExecResult:
        body = {"language": language, "code": code, "timeout_s": timeout}
        path = f"/v1/sandboxes/{self.id}/code"
        if on_stdout is None and on_stderr is None:
            return ExecResult(**_result_fields(self._api.json("POST", path, json=body)))
        return self._stream(f"{path}/stream", body, on_stdout, on_stderr)

    def _stream(self, path: str, body: dict[str, Any], on_stdout: Callback | None,
                on_stderr: Callback | None) -> ExecResult:
        out, err = [], []
        exec_id, exit_code, timed_out = None, None, False
        for event, data in self._api.stream_events(path, body):
            if event == "started":
                exec_id = data["exec_id"]
            elif event in ("stdout", "stderr"):
                sink, cb = (out, on_stdout) if event == "stdout" else (err, on_stderr)
                sink.append(data["data"])
                if cb:
                    cb(data["data"])
            elif event == "exit":
                exit_code, timed_out = data["exit_code"], data["timed_out"]
        return ExecResult(exec_id, "".join(out), "".join(err), exit_code, timed_out)

    def kill_exec(self, exec_id: str) -> None:
        self._api.request("POST", f"/v1/sandboxes/{self.id}/execs/{exec_id}/kill")

    def execs(self) -> list[dict[str, Any]]:
        return self._api.json("GET", f"/v1/sandboxes/{self.id}/execs")

    def pause(self) -> None:
        self._info = self._api.json("POST", f"/v1/sandboxes/{self.id}/pause")

    def resume(self) -> None:
        self._info = self._api.json("POST", f"/v1/sandboxes/{self.id}/resume")

    def snapshot(self, name: str) -> dict[str, Any]:
        """Save /workspace as a reusable template. Pass its `id` as `snapshot_id=` to a new Sandbox."""
        return self._api.json("POST", f"/v1/sandboxes/{self.id}/snapshots", json={"name": name})

    def delete(self) -> None:
        self._api.request("DELETE", f"/v1/sandboxes/{self.id}")


def _result_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {"exec_id": payload.get("exec_id"), "stdout": payload["stdout"], "stderr": payload["stderr"],
            "exit_code": payload["exit_code"], "timed_out": payload["timed_out"]}
