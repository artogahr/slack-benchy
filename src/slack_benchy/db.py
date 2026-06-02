"""SQLite persistence layer.

All access is funnelled through a single connection and an asyncio lock so that
the poll loop and interaction handlers never collide on writes. WAL is enabled
on open so reads do not block writers.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sanitize import MAX_INVENTORY, normalized_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS filaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    norm_key TEXT NOT NULL UNIQUE,
    is_loaded INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trackers (
    user_id TEXT NOT NULL,
    job_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, job_key)
);

CREATE TABLE IF NOT EXISTS job_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    file_name TEXT,
    started_at REAL,
    ended_at REAL,
    ended_state TEXT
);
"""


class InventoryFull(RuntimeError):
    pass


class DuplicateFilament(RuntimeError):
    pass


@dataclass
class Filament:
    id: int
    name: str
    is_loaded: bool


class Database:
    """Async-friendly wrapper around a single SQLite connection."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        def _open() -> sqlite3.Connection:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self._path,
                isolation_level=None,
                check_same_thread=False,
                timeout=10.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(SCHEMA)
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            await asyncio.to_thread(conn.close)

    @contextmanager
    def _cursor(self) -> Any:
        assert self._conn is not None, "Database not opened"
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    async def _run(self, fn, *args):  # type: ignore[no-untyped-def]
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    # kv

    async def kv_get(self, key: str) -> str | None:
        def _go() -> str | None:
            with self._cursor() as cur:
                row = cur.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

        return await self._run(_go)

    async def kv_set(self, key: str, value: str) -> None:
        def _go() -> None:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO kv(key, value, updated_at) VALUES(?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, value, time.time()),
                )

        await self._run(_go)

    async def kv_get_json(self, key: str) -> Any:
        raw = await self.kv_get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def kv_set_json(self, key: str, value: Any) -> None:
        await self.kv_set(key, json.dumps(value, separators=(",", ":")))

    # filaments

    async def list_filaments(self) -> list[Filament]:
        def _go() -> list[Filament]:
            with self._cursor() as cur:
                rows = cur.execute(
                    "SELECT id, name, is_loaded FROM filaments ORDER BY name COLLATE NOCASE"
                ).fetchall()
            return [Filament(id=r["id"], name=r["name"], is_loaded=bool(r["is_loaded"])) for r in rows]

        return await self._run(_go)

    async def get_loaded_filament(self) -> Filament | None:
        def _go() -> Filament | None:
            with self._cursor() as cur:
                row = cur.execute(
                    "SELECT id, name, is_loaded FROM filaments WHERE is_loaded=1 LIMIT 1"
                ).fetchone()
            return Filament(id=row["id"], name=row["name"], is_loaded=True) if row else None

        return await self._run(_go)

    async def add_filament(self, name: str) -> Filament:
        norm = normalized_key(name)

        def _go() -> Filament:
            with self._cursor() as cur:
                count = cur.execute("SELECT COUNT(*) AS c FROM filaments").fetchone()["c"]
                if count >= MAX_INVENTORY:
                    raise InventoryFull(
                        f"Inventory is full ({MAX_INVENTORY}). Remove a spool before adding a new one."
                    )
                existing = cur.execute(
                    "SELECT id, name, is_loaded FROM filaments WHERE norm_key=?",
                    (norm,),
                ).fetchone()
                if existing:
                    raise DuplicateFilament(existing["name"])
                cur.execute(
                    "INSERT INTO filaments(name, norm_key, is_loaded, created_at) VALUES(?, ?, 0, ?)",
                    (name, norm, time.time()),
                )
                fid = cur.lastrowid
            return Filament(id=fid, name=name, is_loaded=False)

        return await self._run(_go)

    async def remove_filament(self, filament_id: int) -> None:
        def _go() -> None:
            with self._cursor() as cur:
                cur.execute("DELETE FROM filaments WHERE id=?", (filament_id,))

        await self._run(_go)

    async def set_loaded_filament(self, filament_id: int | None) -> None:
        def _go() -> None:
            with self._cursor() as cur:
                cur.execute("BEGIN")
                cur.execute("UPDATE filaments SET is_loaded=0")
                if filament_id is not None:
                    cur.execute(
                        "UPDATE filaments SET is_loaded=1 WHERE id=?",
                        (filament_id,),
                    )
                cur.execute("COMMIT")

        await self._run(_go)

    # trackers

    async def add_tracker(self, user_id: str, job_key: str) -> bool:
        """Return True if newly added, False if it was already a tracker."""

        def _go() -> bool:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO trackers(user_id, job_key, created_at) VALUES(?, ?, ?)",
                    (user_id, job_key, time.time()),
                )
                return cur.rowcount > 0

        return await self._run(_go)

    async def remove_tracker(self, user_id: str, job_key: str) -> None:
        def _go() -> None:
            with self._cursor() as cur:
                cur.execute(
                    "DELETE FROM trackers WHERE user_id=? AND job_key=?",
                    (user_id, job_key),
                )

        await self._run(_go)

    async def trackers_for(self, job_key: str) -> list[str]:
        def _go() -> list[str]:
            with self._cursor() as cur:
                rows = cur.execute(
                    "SELECT user_id FROM trackers WHERE job_key=? ORDER BY created_at",
                    (job_key,),
                ).fetchall()
            return [r["user_id"] for r in rows]

        return await self._run(_go)

    async def clear_trackers(self, job_key: str) -> None:
        def _go() -> None:
            with self._cursor() as cur:
                cur.execute("DELETE FROM trackers WHERE job_key=?", (job_key,))

        await self._run(_go)

    # job history

    async def record_job_event(
        self,
        job_key: str,
        file_name: str | None,
        started_at: float | None,
        ended_at: float | None,
        ended_state: str | None,
    ) -> None:
        def _go() -> None:
            with self._cursor() as cur:
                cur.execute(
                    "INSERT INTO job_history(job_key, file_name, started_at, ended_at, ended_state) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (job_key, file_name, started_at, ended_at, ended_state),
                )

        await self._run(_go)
