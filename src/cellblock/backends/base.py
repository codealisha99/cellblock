"""The isolation seam. Everything above this file is backend-agnostic.

A Firecracker backend would implement this same Protocol: create a microVM
from a rootfs, run commands over vsock/agent, and map the same resource and
network knobs onto the VM. The service layer would not change.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

EXIT_TIMED_OUT = 124  # what `timeout(1)` reports when it had to stop the command


@dataclass(frozen=True)
class SandboxSpec:
    image: str
    cpu: float
    memory_mb: int
    pids: int
    network: bool
    workspace_mb: int


class Backend(Protocol):
    def create(self, sandbox_id: str, spec: SandboxSpec) -> None: ...
    def destroy(self, sandbox_id: str) -> None: ...
    def pause(self, sandbox_id: str) -> None: ...
    def resume(self, sandbox_id: str) -> None: ...
    def list_managed(self) -> dict[str, str]: ...  # sandbox_id -> runtime status
    def exec_stream(
        self, sandbox_id: str, exec_id: str, argv: list[str], timeout_s: int
    ) -> Iterator[tuple[str, bytes]]: ...  # yields ("stdout"|"stderr", chunk), then ("exit", code)
    def kill_exec(self, sandbox_id: str, exec_id: str) -> None: ...
    def resolve(self, sandbox_id: str, path: str) -> tuple[str, str]: ...  # (realpath, "file"|"dir"|"none")
    def list_dir(self, sandbox_id: str, path: str) -> list[tuple[str, int, str]]: ...  # (type, size, name)
    def mkdir(self, sandbox_id: str, path: str) -> None: ...
    def read_file(self, sandbox_id: str, path: str) -> Iterator[bytes]: ...  # streamed; never fully buffered
    def write_file(self, sandbox_id: str, path: str, data: bytes) -> None: ...
    def snapshot_workspace(self, sandbox_id: str) -> Iterator[bytes]: ...  # streamed tar of /workspace
    def restore_workspace(self, sandbox_id: str, tar_chunks: Iterable[bytes]) -> None: ...
