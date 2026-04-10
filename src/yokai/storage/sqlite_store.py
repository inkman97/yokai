"""SQLite-backed execution store.

Persists story execution state across process restarts. Uses a single
table with story_key as primary key, so repeated runs of the same story
are idempotent.

Thread safety: a lock around every write plus check_same_thread=False
on the connection. This is enough for a single-process orchestrator
with a ThreadPoolExecutor. It is not designed for multi-process sharing;
use a real database if you need that.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from yokai.core.exceptions import StorageError
from yokai.core.interfaces import ExecutionStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS story_executions (
    story_key    TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    started_at   TEXT,
    completed_at TEXT,
    pr_url       TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_started_at ON story_executions(started_at);
"""


class SqliteExecutionStore(ExecutionStore):
    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as e:
            raise StorageError(f"Failed to initialize store at {db_path}: {e}") from e

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def is_in_flight(self, story_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM story_executions WHERE story_key = ?",
                (story_key,),
            ).fetchone()
            return row is not None and row["status"] == "in_flight"

    def mark_in_flight(self, story_key: str) -> bool:
        now = _now_iso()
        with self._lock:
            existing = self._conn.execute(
                "SELECT status FROM story_executions WHERE story_key = ?",
                (story_key,),
            ).fetchone()
            if existing and existing["status"] == "in_flight":
                return False
            self._conn.execute(
                """
                INSERT INTO story_executions
                    (story_key, status, started_at, completed_at, pr_url, error)
                VALUES (?, 'in_flight', ?, NULL, NULL, NULL)
                ON CONFLICT(story_key) DO UPDATE SET
                    status='in_flight',
                    started_at=excluded.started_at,
                    completed_at=NULL,
                    pr_url=NULL,
                    error=NULL
                """,
                (story_key, now),
            )
            self._conn.commit()
            return True

    def mark_completed(self, story_key: str, pr_url: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO story_executions
                    (story_key, status, started_at, completed_at, pr_url, error)
                VALUES (?, 'completed', ?, ?, ?, NULL)
                ON CONFLICT(story_key) DO UPDATE SET
                    status='completed',
                    completed_at=excluded.completed_at,
                    pr_url=excluded.pr_url,
                    error=NULL
                """,
                (story_key, now, now, pr_url),
            )
            self._conn.commit()

    def mark_failed(self, story_key: str, error: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO story_executions
                    (story_key, status, started_at, completed_at, pr_url, error)
                VALUES (?, 'failed', ?, ?, NULL, ?)
                ON CONFLICT(story_key) DO UPDATE SET
                    status='failed',
                    completed_at=excluded.completed_at,
                    error=excluded.error
                """,
                (story_key, now, now, error),
            )
            self._conn.commit()

    def list_recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT story_key, status, started_at, completed_at, pr_url, error
                FROM story_executions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
