"""SQLite state. Source of truth for what the control plane believes exists;
reconciled against Docker on startup."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sandboxes (
    id TEXT PRIMARY KEY, key_id TEXT, status TEXT NOT NULL, image TEXT NOT NULL,
    cpu REAL NOT NULL, memory_mb INTEGER NOT NULL, pids INTEGER NOT NULL,
    network INTEGER NOT NULL, workspace_mb INTEGER NOT NULL,
    snapshot_id TEXT, created_at REAL NOT NULL, expires_at REAL
);
CREATE TABLE IF NOT EXISTS execs (
    id TEXT PRIMARY KEY, sandbox_id TEXT NOT NULL, argv TEXT NOT NULL, status TEXT NOT NULL,
    exit_code INTEGER, timed_out INTEGER NOT NULL DEFAULT 0,
    stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY, key_id TEXT NOT NULL, name TEXT NOT NULL, image TEXT NOT NULL,
    source_sandbox_id TEXT NOT NULL, path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL
);
"""

# Table and column names are interpolated, so they must come from code, never users.
_TABLES = {"api_keys", "sandboxes", "execs", "templates"}


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            self._db.execute(sql, params)

    def insert(self, table: str, row: dict[str, Any]) -> None:
        assert table in _TABLES
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    def update(self, table: str, row_id: str, changes: dict[str, Any]) -> None:
        assert table in _TABLES
        sets = ", ".join(f"{col} = ?" for col in changes)
        self._run(f"UPDATE {table} SET {sets} WHERE id = ?", (*changes.values(), row_id))

    def delete(self, table: str, row_id: str) -> None:
        assert table in _TABLES
        self._run(f"DELETE FROM {table} WHERE id = ?", (row_id,))

    def get(self, table: str, row_id: str) -> dict[str, Any] | None:
        assert table in _TABLES
        rows = self._query(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        return rows[0] if rows else None

    def find(self, table: str, **where: Any) -> list[dict[str, Any]]:
        assert table in _TABLES
        if not where:
            return self._query(f"SELECT * FROM {table} ORDER BY rowid")
        clause = " AND ".join(f"{col} = ?" for col in where)
        return self._query(f"SELECT * FROM {table} WHERE {clause} ORDER BY rowid", tuple(where.values()))

    def sandboxes_in(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in statuses)
        return self._query(f"SELECT * FROM sandboxes WHERE status IN ({marks}) ORDER BY rowid", statuses)

    def expired(self, now: float) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM sandboxes WHERE status IN ('running','paused') AND expires_at < ?", (now,)
        )

    def count_active(self, key_id: str) -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM sandboxes WHERE key_id = ? AND status IN ('creating','running','paused')",
            (key_id,),
        )
        return int(rows[0]["n"])
