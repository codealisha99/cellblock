"""Orchestration. Owns policy (keys, quotas, pool, TTL, file rules) and talks to
the Backend and Store. Nothing here knows about HTTP."""

from __future__ import annotations

import codecs
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from cellblock.backends.base import EXIT_TIMED_OUT, Backend, SandboxSpec
from cellblock.config import Settings
from cellblock.errors import BadRequest, Conflict, NotFound, QuotaExceeded, Unauthorized
from cellblock.ids import new_id
from cellblock.paths import assert_inside, normalize
from cellblock.store import Store

log = logging.getLogger("cellblock")
RECORD_LIMIT = 64 * 1024  # bytes of stdout/stderr kept per exec for `cellblock logs`
RUNTIME_LANGS = {"python": ["python3", "-u"], "node": ["node"]}
FILE_KINDS = {"f": "file", "d": "dir", "l": "symlink"}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"], "status": row["status"], "image": row["image"], "cpu": row["cpu"],
        "memory_mb": row["memory_mb"], "pids": row["pids"], "network": bool(row["network"]),
        "workspace_mb": row["workspace_mb"], "snapshot_id": row["snapshot_id"],
        "created_at": row["created_at"], "expires_at": row["expires_at"],
    }


def collect(events: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """Fold a stream of exec events into one JSON-able result."""
    result: dict[str, Any] = {"stdout": "", "stderr": "", "exit_code": None, "timed_out": False}
    for event in events:
        if event["type"] in ("stdout", "stderr"):
            result[event["type"]] += event["data"]
        elif event["type"] == "started":
            result["exec_id"] = event["exec_id"]
        elif event["type"] == "exit":
            result.update(exit_code=event["exit_code"], timed_out=event["timed_out"])
    return result


def _exec_public(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "argv": json.loads(row["argv"]), "timed_out": bool(row["timed_out"])}


class Service:
    def __init__(self, settings: Settings, store: Store, backend: Backend) -> None:
        self.settings = settings
        self.store = store
        self.backend = backend
        self.clock = time.time
        self._lock = threading.RLock()  # guards quota check + reservation
        self._pool_lock = threading.Lock()  # one pool refill at a time
        self._closing = False

    # ---- auth -------------------------------------------------------------
    def create_key(self, name: str) -> str:
        raw = "cb_" + secrets.token_urlsafe(24)
        self.store.insert("api_keys", {
            "id": new_id("key"), "name": name, "key_hash": _hash(raw), "created_at": self.clock(),
        })
        return raw

    def authenticate(self, raw: str | None) -> str:
        if not raw:
            raise Unauthorized("missing API key")
        rows = self.store.find("api_keys", key_hash=_hash(raw))
        if not rows:
            raise Unauthorized("invalid API key")
        return rows[0]["id"]

    # ---- sandboxes --------------------------------------------------------
    @property
    def default_spec(self) -> SandboxSpec:
        s = self.settings
        return SandboxSpec(image=s.default_image, cpu=s.default_cpu, memory_mb=s.default_memory_mb,
                           pids=s.default_pids, network=False, workspace_mb=s.default_workspace_mb)

    def create_sandbox(self, key_id: str, spec: SandboxSpec, ttl_s: int,
                       snapshot_id: str | None = None) -> dict[str, Any]:
        template = self._owned_template(key_id, snapshot_id) if snapshot_id else None
        if template:
            spec = replace(spec, image=template["image"])
        with self._lock:
            self._check_quota(key_id)
            if template is None and spec == self.default_spec:
                claimed = self._claim_pooled(key_id, ttl_s)
                if claimed:
                    return _public(claimed)
            sandbox_id = new_id("sbx")
            self.store.insert("sandboxes", self._row(sandbox_id, key_id, "creating", spec, snapshot_id))
        try:
            self.backend.create(sandbox_id, spec)
            if template:
                self.backend.restore_workspace(sandbox_id, Path(template["path"]).read_bytes())
        except Exception:
            self._retire(sandbox_id)
            raise
        self.store.update("sandboxes", sandbox_id,
                          {"status": "running", "expires_at": self.clock() + ttl_s})
        return _public(self.store.get("sandboxes", sandbox_id))

    def _row(self, sandbox_id: str, key_id: str | None, status: str, spec: SandboxSpec,
             snapshot_id: str | None) -> dict[str, Any]:
        return {
            "id": sandbox_id, "key_id": key_id, "status": status, "image": spec.image, "cpu": spec.cpu,
            "memory_mb": spec.memory_mb, "pids": spec.pids, "network": int(spec.network),
            "workspace_mb": spec.workspace_mb, "snapshot_id": snapshot_id, "created_at": self.clock(),
            "expires_at": None,
        }

    def _check_quota(self, key_id: str) -> None:
        if self.store.count_active(key_id) >= self.settings.max_sandboxes_per_key:
            raise QuotaExceeded(f"quota reached: at most {self.settings.max_sandboxes_per_key} concurrent sandboxes per key")

    def _claim_pooled(self, key_id: str, ttl_s: int) -> dict[str, Any] | None:
        pooled = self.store.find("sandboxes", status="pooled")
        if not pooled:
            return None
        self.store.update("sandboxes", pooled[0]["id"], {
            "status": "running", "key_id": key_id, "expires_at": self.clock() + ttl_s,
            "created_at": self.clock(),
        })
        return self.store.get("sandboxes", pooled[0]["id"])

    def _retire(self, sandbox_id: str) -> None:
        self.backend.destroy(sandbox_id)
        self.store.update("sandboxes", sandbox_id, {"status": "deleted"})

    def get(self, key_id: str, sandbox_id: str) -> dict[str, Any]:
        return _public(self._owned(key_id, sandbox_id))

    def list(self, key_id: str) -> list[dict[str, Any]]:
        rows = self.store.find("sandboxes", key_id=key_id)
        return [_public(r) for r in rows if r["status"] in ("running", "paused")]

    def destroy(self, key_id: str, sandbox_id: str) -> None:
        self._owned(key_id, sandbox_id)
        self._retire(sandbox_id)

    def pause(self, key_id: str, sandbox_id: str) -> dict[str, Any]:
        self._in_state(self._owned(key_id, sandbox_id), "running")
        self.backend.pause(sandbox_id)
        self.store.update("sandboxes", sandbox_id, {"status": "paused"})
        return self.get(key_id, sandbox_id)

    def resume(self, key_id: str, sandbox_id: str) -> dict[str, Any]:
        self._in_state(self._owned(key_id, sandbox_id), "paused")
        self.backend.resume(sandbox_id)
        self.store.update("sandboxes", sandbox_id, {"status": "running"})
        return self.get(key_id, sandbox_id)

    def _owned(self, key_id: str, sandbox_id: str) -> dict[str, Any]:
        row = self.store.get("sandboxes", sandbox_id)
        if row is None or row["key_id"] != key_id or row["status"] == "deleted":
            raise NotFound("sandbox not found")
        return row

    @staticmethod
    def _in_state(row: dict[str, Any], *states: str) -> dict[str, Any]:
        if row["status"] not in states:
            raise Conflict(f"sandbox is {row['status']}; expected {' or '.join(states)}")
        return row

    def _running(self, key_id: str, sandbox_id: str) -> None:
        self._in_state(self._owned(key_id, sandbox_id), "running")

    # ---- pool -------------------------------------------------------------
    def refill_pool(self) -> int:
        """Top the warm pool up to `pool_size` default-profile containers. Returns how many were added."""
        added = 0
        with self._pool_lock:
            if self._closing:
                return 0
            missing = self.settings.pool_size - len(self.store.find("sandboxes", status="pooled"))
            for _ in range(max(missing, 0)):
                sandbox_id, spec = new_id("sbx"), self.default_spec
                self.backend.create(sandbox_id, spec)
                # Row is written only after the container is running, so a claim never sees a half-built one.
                self.store.insert("sandboxes", self._row(sandbox_id, None, "pooled", spec, None))
                added += 1
        return added

    def pool_ready(self) -> int:
        return len(self.store.find("sandboxes", status="pooled"))

    # ---- exec -------------------------------------------------------------
    def exec_stream(self, key_id: str, sandbox_id: str, argv: list[str], timeout_s: int) -> Iterator[dict[str, Any]]:
        """Validates eagerly (so callers can return 404/409 before streaming), then yields events."""
        self._running(key_id, sandbox_id)
        exec_id = new_id("ex")
        self.store.insert("execs", {
            "id": exec_id, "sandbox_id": sandbox_id, "argv": json.dumps(argv), "status": "running",
            "started_at": self.clock(),
        })
        return self._run_exec(sandbox_id, exec_id, argv, timeout_s)

    def _run_exec(self, sandbox_id: str, exec_id: str, argv: list[str], timeout_s: int) -> Iterator[dict[str, Any]]:
        decoders = {k: codecs.getincrementaldecoder("utf-8")("replace") for k in ("stdout", "stderr")}
        recorded = {"stdout": "", "stderr": ""}
        exit_code: int | None = None
        yield {"type": "started", "exec_id": exec_id}
        try:
            for kind, data in self.backend.exec_stream(sandbox_id, exec_id, argv, timeout_s):
                if kind == "exit":
                    exit_code = data
                    break
                text = decoders[kind].decode(data)
                recorded[kind] = (recorded[kind] + text)[:RECORD_LIMIT]
                yield {"type": kind, "data": text}
        finally:
            self.store.update("execs", exec_id, {
                "status": "finished" if exit_code is not None else "aborted",
                "exit_code": exit_code, "timed_out": int(exit_code == EXIT_TIMED_OUT),
                "stdout": recorded["stdout"], "stderr": recorded["stderr"], "finished_at": self.clock(),
            })
        yield {"type": "exit", "exit_code": exit_code, "timed_out": exit_code == EXIT_TIMED_OUT}

    def exec_collect(self, key_id: str, sandbox_id: str, argv: list[str], timeout_s: int) -> dict[str, Any]:
        return collect(self.exec_stream(key_id, sandbox_id, argv, timeout_s))

    def run_code(self, key_id: str, sandbox_id: str, language: str, code: str, timeout_s: int) -> Iterator[dict[str, Any]]:
        if language not in RUNTIME_LANGS:
            raise BadRequest(f"unsupported language {language!r}; use one of {sorted(RUNTIME_LANGS)}")
        self._running(key_id, sandbox_id)
        suffix = {"python": "py", "node": "js"}[language]
        path = f"/tmp/snippet-{secrets.token_hex(6)}.{suffix}"
        self.backend.write_file(sandbox_id, path, code.encode())
        return self.exec_stream(key_id, sandbox_id, [*RUNTIME_LANGS[language], path], timeout_s)

    def kill_exec(self, key_id: str, sandbox_id: str, exec_id: str) -> None:
        self._owned(key_id, sandbox_id)
        row = self.store.get("execs", exec_id)
        if row is None or row["sandbox_id"] != sandbox_id:
            raise NotFound("exec not found")
        if row["status"] != "running":
            raise Conflict("exec already finished")
        self.backend.kill_exec(sandbox_id, exec_id)

    def list_execs(self, key_id: str, sandbox_id: str) -> list[dict[str, Any]]:
        self._owned(key_id, sandbox_id)
        return [_exec_public(r) for r in self.store.find("execs", sandbox_id=sandbox_id)]

    # ---- files ------------------------------------------------------------
    def _checked_path(self, sandbox_id: str, raw: str) -> tuple[str, str]:
        """Normalize, then resolve symlinks inside the container and re-check the real location."""
        path = normalize(raw)
        resolved, kind = self.backend.resolve(sandbox_id, path)
        return assert_inside(resolved), kind

    def list_files(self, key_id: str, sandbox_id: str, raw: str) -> list[dict[str, Any]]:
        self._running(key_id, sandbox_id)
        resolved, kind = self._checked_path(sandbox_id, raw)
        if kind == "none":
            raise NotFound("no such directory")
        if kind == "file":
            raise BadRequest("path is a file, not a directory")
        return [{"name": name, "type": FILE_KINDS.get(t, "other"), "size": size}
                for t, size, name in self.backend.list_dir(sandbox_id, resolved)]

    def read_file(self, key_id: str, sandbox_id: str, raw: str) -> bytes:
        self._running(key_id, sandbox_id)
        resolved, kind = self._checked_path(sandbox_id, raw)
        if kind == "none":
            raise NotFound("no such file")
        if kind == "dir":
            raise BadRequest("path is a directory")
        return self.backend.read_file(sandbox_id, resolved)

    def write_file(self, key_id: str, sandbox_id: str, raw: str, data: bytes) -> str:
        self._running(key_id, sandbox_id)
        path = normalize(raw)
        parent = path.rsplit("/", 1)[0]
        parent_real, _ = self.backend.resolve(sandbox_id, parent)
        assert_inside(parent_real)  # check before mkdir -p can follow a symlink out of the workspace
        self.backend.mkdir(sandbox_id, parent)
        resolved, kind = self._checked_path(sandbox_id, path)
        if kind == "dir":
            raise BadRequest("path is a directory")
        self.backend.write_file(sandbox_id, resolved, data)
        return resolved

    # ---- snapshots --------------------------------------------------------
    def snapshot(self, key_id: str, sandbox_id: str, name: str) -> dict[str, Any]:
        row = self._in_state(self._owned(key_id, sandbox_id), "running")
        data = self.backend.snapshot_workspace(sandbox_id)
        template_id = new_id("tpl")
        self.settings.template_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.template_dir / f"{template_id}.tar"
        path.write_bytes(data)
        self.store.insert("templates", {
            "id": template_id, "key_id": key_id, "name": name, "image": row["image"],
            "source_sandbox_id": sandbox_id, "path": str(path), "size_bytes": len(data),
            "created_at": self.clock(),
        })
        return self._template_public(self.store.get("templates", template_id))

    def list_templates(self, key_id: str) -> list[dict[str, Any]]:
        return [self._template_public(r) for r in self.store.find("templates", key_id=key_id)]

    def get_template(self, key_id: str, template_id: str) -> dict[str, Any]:
        return self._template_public(self._owned_template(key_id, template_id))

    def delete_template(self, key_id: str, template_id: str) -> None:
        row = self._owned_template(key_id, template_id)
        Path(row["path"]).unlink(missing_ok=True)
        self.store.delete("templates", template_id)

    def _owned_template(self, key_id: str, template_id: str | None) -> dict[str, Any]:
        row = self.store.get("templates", template_id or "")
        if row is None or row["key_id"] != key_id:
            raise NotFound("template not found")
        return row

    @staticmethod
    def _template_public(row: dict[str, Any]) -> dict[str, Any]:
        return {k: row[k] for k in ("id", "name", "image", "source_sandbox_id", "size_bytes", "created_at")}

    # ---- lifecycle --------------------------------------------------------
    def reap_expired(self) -> list[str]:
        reaped = []
        for row in self.store.expired(self.clock()):
            self._retire(row["id"])
            reaped.append(row["id"])
        if reaped:
            log.info("reaped %d expired sandbox(es): %s", len(reaped), ", ".join(reaped))
        return reaped

    def reconcile(self) -> dict[str, list[str]]:
        """Make SQLite and Docker agree. Containers without a live row are orphans and get removed;
        rows whose container vanished are marked lost."""
        live = self.backend.list_managed()
        rows = {r["id"]: r for r in self.store.sandboxes_in(("running", "paused", "pooled"))}
        lost, synced = [], []
        for sandbox_id, row in rows.items():
            runtime = live.get(sandbox_id)
            if runtime not in ("running", "paused"):
                lost.append(sandbox_id)
                self.store.update("sandboxes", sandbox_id, {"status": "deleted" if row["status"] == "pooled" else "lost"})
                self.backend.destroy(sandbox_id)
            elif runtime != row["status"]:
                synced.append(sandbox_id)
                self.store.update("sandboxes", sandbox_id, {"status": runtime})
        orphans = [sid for sid in live if sid not in rows]
        for sandbox_id in orphans:
            self.backend.destroy(sandbox_id)
        for row in self.store.sandboxes_in(("creating",)):
            self._retire(row["id"])
        if lost or orphans:
            log.info("reconcile: lost=%s orphans_removed=%s", lost, orphans)
        return {"lost": lost, "orphans_removed": orphans, "synced": synced}

    def shutdown(self) -> None:
        """Pooled containers have no owner, so they must not outlive the control plane.
        Taking the pool lock waits out any in-flight refill, and `_closing` stops new ones."""
        with self._pool_lock:
            self._closing = True
            for row in self.store.find("sandboxes", status="pooled"):
                self._retire(row["id"])
