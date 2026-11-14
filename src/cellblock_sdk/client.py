"""Low-level HTTP client and the value types shared by the SDK."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:4300"


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class ExecResult:
    exec_id: str | None
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class Cellblock:
    """Talks to the control plane. Reads CELLBLOCK_URL / CELLBLOCK_API_KEY when args are omitted."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 120.0) -> None:
        key = api_key or os.environ.get("CELLBLOCK_API_KEY")
        if not key:
            raise ValueError("no API key: pass api_key= or set CELLBLOCK_API_KEY")
        self._http = httpx.Client(
            base_url=base_url or os.environ.get("CELLBLOCK_URL", DEFAULT_URL),
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._http.request(method, path, **kwargs)
        _raise_for_status(response)
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        return self.request(method, path, **kwargs).json()

    def list_sandboxes(self) -> list[dict[str, Any]]:
        return self.json("GET", "/v1/sandboxes")

    def stream_events(self, path: str, body: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield (event, data) pairs from a server-sent-events endpoint."""
        with self._http.stream("POST", path, json=body) as response:
            _raise_for_status(response)
            yield from _parse_sse(response.iter_lines())

    def close(self) -> None:
        self._http.close()


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        message = response.json().get("error", response.text)
    except ValueError:
        message = response.text
    raise ApiError(response.status_code, message)


def _parse_sse(lines: Iterator[str]) -> Iterator[tuple[str, dict[str, Any]]]:
    event, data = "message", []
    for line in lines:
        if line == "":
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
