"""SQLite-backed implementation of all four queue interfaces.

Persistence model: a single SQLite file holds all queue state.
WAL mode is enabled for better read/write concurrency. All multi-step
operations (enqueue + dedupe check, dequeue + lease assignment) are
wrapped in transactions with `BEGIN IMMEDIATE` to take a write lock
upfront and avoid SQLITE_BUSY in the middle of a transaction.

Concurrent access: SQLite supports multiple readers and one writer.
The Python `sqlite3` module's default isolation behaviour (autocommit
when not in a transaction, single write lock when in one) is fine for
our use case as long as we do not hold transactions open across
network calls. Each public method opens, executes, commits, closes.

Process safety: yes - multiple coordinator/worker processes can share
the same SQLite file. Tested implicitly by the shared test suite.

Threading safety: yes - we open a new connection per call. SQLite
connections are not thread-safe in Python by default, but a fresh
connection per call sidesteps the issue.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yokai.queue.exceptions import (
    DuplicateJobError,
    JobNotFound,
    LeaseExpiredError,
    QueueBackendError,
)
from yokai.queue.interfaces import (
    CoordinatorLock,
    JobQueue,
    ResultStore,
    WorkerRegistry,
)
from yokai.queue.models import (
    TERMINAL_STATES,
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)
from yokai.queue.state_machine import needs_recovery, transition


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    story_key TEXT NOT NULL,
    repo_slug TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    picked_up_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    worker_id TEXT,
    lease_expires_at TEXT,
    not_before TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created
    ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status_not_before
    ON jobs(status, not_before, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_story_key_status
    ON jobs(story_key, status);
CREATE INDEX IF NOT EXISTS idx_jobs_lease_expires
    ON jobs(lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS results (
    job_id TEXT PRIMARY KEY,
    success INTEGER NOT NULL,
    agent_output TEXT NOT NULL DEFAULT '',
    error TEXT,
    traceback TEXT,
    duration_seconds REAL NOT NULL DEFAULT 0,
    branch_name TEXT,
    commit_sha TEXT,
    completed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    hostname TEXT NOT NULL,
    pid INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    last_heartbeat_at TEXT NOT NULL,
    current_job_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_workers_heartbeat
    ON workers(last_heartbeat_at);

CREATE TABLE IF NOT EXISTS coordinator_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class SqliteBackend(JobQueue, ResultStore, WorkerRegistry, CoordinatorLock):
    """Persistent backend storing all queue state in a single SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        # Per-process init lock: avoid two threads racing to create schema
        self._init_lock = threading.Lock()
        self._initialized = False
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,  # we manage transactions explicitly
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        # WAL mode is a database-level setting set once in _init_schema.
        # synchronous and foreign_keys are connection-level: set on each.
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = sqlite3.connect(
                self._db_path,
                isolation_level=None,
                timeout=30.0,
            )
            try:
                # PRAGMA journal_mode = WAL takes an exclusive lock and
                # is expensive under contention. Only execute it if the
                # DB is not already in WAL mode (the setting persists in
                # the file).
                current = conn.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
                if current.lower() != "wal":
                    conn.execute("PRAGMA journal_mode = WAL;")
                conn.executescript(SCHEMA)
            finally:
                conn.close()
            self._initialized = True

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            story_key=row["story_key"],
            repo_slug=row["repo_slug"],
            payload=json.loads(row["payload"]),
            status=JobStatus(row["status"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            picked_up_at=_parse(row["picked_up_at"]),
            completed_at=_parse(row["completed_at"]),
            last_error=row["last_error"],
            worker_id=row["worker_id"],
            not_before=_parse(row["not_before"]),
        )

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> JobResult:
        return JobResult(
            job_id=row["job_id"],
            success=bool(row["success"]),
            agent_output=row["agent_output"],
            error=row["error"],
            traceback=row["traceback"],
            duration_seconds=row["duration_seconds"],
            branch_name=row["branch_name"],
            commit_sha=row["commit_sha"],
            completed_at=_parse(row["completed_at"]),
        )

    @staticmethod
    def _row_to_worker(row: sqlite3.Row) -> WorkerInfo:
        return WorkerInfo(
            worker_id=row["worker_id"],
            hostname=row["hostname"],
            pid=row["pid"],
            started_at=_parse(row["started_at"]),
            last_heartbeat_at=_parse(row["last_heartbeat_at"]),
            current_job_id=row["current_job_id"],
        )

    def enqueue(self, job: Job) -> Job:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Dedupe check: any non-terminal job for same story_key?
                placeholders = ",".join("?" * len(TERMINAL_STATES))
                terminal_values = [s.value for s in TERMINAL_STATES]
                row = conn.execute(
                    f"""
                    SELECT job_id, status FROM jobs
                    WHERE story_key = ?
                      AND status NOT IN ({placeholders})
                    LIMIT 1
                    """,
                    [job.story_key, *terminal_values],
                ).fetchone()
                if row is not None:
                    conn.execute("ROLLBACK;")
                    raise DuplicateJobError(
                        f"Story {job.story_key} already has an in-flight "
                        f"job: {row['job_id']} (status={row['status']})"
                    )

                new_status = transition(job.status, JobStatus.QUEUED)
                now = _utcnow()
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, story_key, repo_slug, payload, status,
                        attempts, max_attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        job.job_id,
                        job.story_key,
                        job.repo_slug,
                        json.dumps(job.payload),
                        new_status.value,
                        job.attempts,
                        job.max_attempts,
                        _iso(job.created_at),
                        _iso(now),
                    ],
                )
                conn.execute("COMMIT;")
            except DuplicateJobError:
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            return self._fetch_job(conn, job.job_id)
        finally:
            conn.close()

    def dequeue(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> Job | None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                now = _utcnow()
                row = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE status = ?
                      AND (not_before IS NULL OR not_before <= ?)
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    [JobStatus.QUEUED.value, _iso(now)],
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT;")
                    return None
                job = self._row_to_job(row)
                new_status = transition(job.status, JobStatus.PICKED_UP)
                lease_expiry = now + lease_duration
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        worker_id = ?,
                        picked_up_at = ?,
                        updated_at = ?,
                        attempts = attempts + 1,
                        lease_expires_at = ?
                    WHERE job_id = ?
                    """,
                    [
                        new_status.value,
                        worker_id,
                        _iso(now),
                        _iso(now),
                        _iso(lease_expiry),
                        job.job_id,
                    ],
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            return self._fetch_job(conn, job.job_id)
        finally:
            conn.close()

    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        worker_id: str | None = None,
        error: str | None = None,
        not_before: datetime | None = None,
    ) -> Job:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", [job_id]
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK;")
                    raise JobNotFound(f"Job {job_id} not found")
                if (
                    worker_id is not None
                    and row["worker_id"] != worker_id
                ):
                    conn.execute("ROLLBACK;")
                    raise LeaseExpiredError(
                        f"Worker {worker_id} does not hold lease on job "
                        f"{job_id} (current owner: {row['worker_id']})"
                    )
                current_status = JobStatus(row["status"])
                final_status = transition(current_status, new_status)
                now = _utcnow()

                truncated_error = (
                    error[:2000] if error is not None else row["last_error"]
                )
                completed_at = (
                    _iso(now)
                    if final_status in TERMINAL_STATES
                    else row["completed_at"]
                )
                # Reclaim/retry: clear worker assignment and lease
                if final_status == JobStatus.QUEUED and current_status != JobStatus.PENDING:
                    new_worker_id: str | None = None
                    new_lease_expiry: str | None = None
                    new_not_before = _iso(not_before)
                elif final_status in TERMINAL_STATES:
                    new_worker_id = row["worker_id"]
                    new_lease_expiry = None
                    new_not_before = None
                else:
                    new_worker_id = row["worker_id"]
                    new_lease_expiry = row["lease_expires_at"]
                    new_not_before = row["not_before"]

                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?,
                        worker_id = ?,
                        last_error = ?,
                        updated_at = ?,
                        completed_at = ?,
                        lease_expires_at = ?,
                        not_before = ?
                    WHERE job_id = ?
                    """,
                    [
                        final_status.value,
                        new_worker_id,
                        truncated_error,
                        _iso(now),
                        completed_at,
                        new_lease_expiry,
                        new_not_before,
                        job_id,
                    ],
                )
                conn.execute("COMMIT;")
            except (JobNotFound, LeaseExpiredError):
                raise
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            return self._fetch_job(conn, job_id)
        finally:
            conn.close()

    def get(self, job_id: str) -> Job:
        conn = self._connect()
        try:
            return self._fetch_job(conn, job_id)
        finally:
            conn.close()

    def _fetch_job(self, conn: sqlite3.Connection, job_id: str) -> Job:
        row = conn.execute(
            "SELECT * FROM jobs WHERE job_id = ?", [job_id]
        ).fetchone()
        if row is None:
            raise JobNotFound(f"Job {job_id} not found")
        return self._row_to_job(row)

    def list_by_status(
        self,
        status: JobStatus,
        limit: int = 100,
    ) -> list[Job]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                [status.value, limit],
            ).fetchall()
            return [self._row_to_job(r) for r in rows]
        finally:
            conn.close()

    def reclaim_expired_leases(self) -> list[Job]:
        conn = self._connect()
        reclaimed: list[Job] = []
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                now = _utcnow()
                rows = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                      AND status IN (?, ?)
                    """,
                    [
                        _iso(now),
                        JobStatus.PICKED_UP.value,
                        JobStatus.AGENT_RUNNING.value,
                    ],
                ).fetchall()

                for row in rows:
                    job = self._row_to_job(row)
                    if not needs_recovery(job.status):
                        continue
                    if job.attempts >= job.max_attempts:
                        # AGENT_FAILED -> DEAD_LETTERED
                        s = transition(job.status, JobStatus.AGENT_FAILED)
                        s = transition(s, JobStatus.DEAD_LETTERED)
                        conn.execute(
                            """
                            UPDATE jobs
                            SET status = ?,
                                completed_at = ?,
                                updated_at = ?,
                                lease_expires_at = NULL,
                                last_error = ?
                            WHERE job_id = ?
                            """,
                            [
                                s.value,
                                _iso(now),
                                _iso(now),
                                f"Worker {job.worker_id} lease expired and "
                                f"retry budget exhausted",
                                job.job_id,
                            ],
                        )
                    else:
                        s = transition(job.status, JobStatus.QUEUED)
                        conn.execute(
                            """
                            UPDATE jobs
                            SET status = ?,
                                worker_id = NULL,
                                lease_expires_at = NULL,
                                updated_at = ?,
                                last_error = ?
                            WHERE job_id = ?
                            """,
                            [
                                s.value,
                                _iso(now),
                                f"Worker lease expired, returning to queue "
                                f"(attempt {job.attempts}/{job.max_attempts})",
                                job.job_id,
                            ],
                        )
                    # Re-fetch the freshly-updated row
                    fresh_row = conn.execute(
                        "SELECT * FROM jobs WHERE job_id = ?", [job.job_id]
                    ).fetchone()
                    reclaimed.append(self._row_to_job(fresh_row))
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
            return reclaimed
        finally:
            conn.close()

    def stats(self) -> dict[JobStatus, int]:
        conn = self._connect()
        try:
            counts: dict[JobStatus, int] = {s: 0 for s in JobStatus}
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            for r in rows:
                try:
                    counts[JobStatus(r["status"])] = r["n"]
                except ValueError:
                    pass  # unknown status in DB - skip
            return counts
        finally:
            conn.close()

    def put(self, result: JobResult) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO results (
                    job_id, success, agent_output, error, traceback,
                    duration_seconds, branch_name, commit_sha, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    success = excluded.success,
                    agent_output = excluded.agent_output,
                    error = excluded.error,
                    traceback = excluded.traceback,
                    duration_seconds = excluded.duration_seconds,
                    branch_name = excluded.branch_name,
                    commit_sha = excluded.commit_sha,
                    completed_at = excluded.completed_at
                """,
                [
                    result.job_id,
                    1 if result.success else 0,
                    result.agent_output,
                    result.error,
                    result.traceback,
                    result.duration_seconds,
                    result.branch_name,
                    result.commit_sha,
                    _iso(result.completed_at),
                ],
            )
        finally:
            conn.close()

    def get_result(self, job_id: str) -> JobResult | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM results WHERE job_id = ?", [job_id]
            ).fetchone()
            if row is None:
                return None
            return self._row_to_result(row)
        finally:
            conn.close()

    def pending_for_postprocessing(
        self, limit: int = 50
    ) -> list[JobResult]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT r.*
                FROM results r
                JOIN jobs j ON j.job_id = r.job_id
                WHERE r.success = 1
                  AND j.status = ?
                ORDER BY r.completed_at ASC
                LIMIT ?
                """,
                [JobStatus.AGENT_COMPLETED.value, limit],
            ).fetchall()
            return [self._row_to_result(r) for r in rows]
        finally:
            conn.close()

    def register(self, worker: WorkerInfo) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO workers (
                    worker_id, hostname, pid, started_at,
                    last_heartbeat_at, current_job_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    hostname = excluded.hostname,
                    pid = excluded.pid,
                    started_at = excluded.started_at,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    current_job_id = excluded.current_job_id
                """,
                [
                    worker.worker_id,
                    worker.hostname,
                    worker.pid,
                    _iso(worker.started_at),
                    _iso(worker.last_heartbeat_at),
                    worker.current_job_id,
                ],
            )
        finally:
            conn.close()

    def heartbeat(
        self, worker_id: str, current_job_id: str | None
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                UPDATE workers
                SET last_heartbeat_at = ?, current_job_id = ?
                WHERE worker_id = ?
                """,
                [_iso(_utcnow()), current_job_id, worker_id],
            )
        finally:
            conn.close()

    def deregister(self, worker_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM workers WHERE worker_id = ?", [worker_id]
            )
        finally:
            conn.close()

    def list_alive(self, max_age: timedelta) -> list[WorkerInfo]:
        conn = self._connect()
        try:
            cutoff = _utcnow() - max_age
            rows = conn.execute(
                """
                SELECT * FROM workers
                WHERE last_heartbeat_at >= ?
                ORDER BY worker_id
                """,
                [_iso(cutoff)],
            ).fetchall()
            return [self._row_to_worker(r) for r in rows]
        finally:
            conn.close()

    def list_dead(self, max_age: timedelta) -> list[WorkerInfo]:
        conn = self._connect()
        try:
            cutoff = _utcnow() - max_age
            rows = conn.execute(
                """
                SELECT * FROM workers
                WHERE last_heartbeat_at < ?
                ORDER BY worker_id
                """,
                [_iso(cutoff)],
            ).fetchall()
            return [self._row_to_worker(r) for r in rows]
        finally:
            conn.close()

    def acquire(
        self,
        owner_id: str,
        lease_duration: timedelta,
    ) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                now = _utcnow()
                row = conn.execute(
                    "SELECT owner_id, expires_at FROM coordinator_lock WHERE id = 1"
                ).fetchone()

                if row is None:
                    conn.execute(
                        """
                        INSERT INTO coordinator_lock (id, owner_id, expires_at)
                        VALUES (1, ?, ?)
                        """,
                        [owner_id, _iso(now + lease_duration)],
                    )
                    conn.execute("COMMIT;")
                    return True

                current_owner = row["owner_id"]
                expires = _parse(row["expires_at"])
                if (
                    current_owner == owner_id
                    or expires is None
                    or expires <= now
                ):
                    conn.execute(
                        """
                        UPDATE coordinator_lock
                        SET owner_id = ?, expires_at = ?
                        WHERE id = 1
                        """,
                        [owner_id, _iso(now + lease_duration)],
                    )
                    conn.execute("COMMIT;")
                    return True

                conn.execute("COMMIT;")
                return False
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

    def renew(
        self, owner_id: str, lease_duration: timedelta
    ) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT owner_id FROM coordinator_lock WHERE id = 1"
                ).fetchone()
                if row is None or row["owner_id"] != owner_id:
                    conn.execute("ROLLBACK;")
                    return False
                conn.execute(
                    """
                    UPDATE coordinator_lock
                    SET expires_at = ?
                    WHERE id = 1
                    """,
                    [_iso(_utcnow() + lease_duration)],
                )
                conn.execute("COMMIT;")
                return True
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        finally:
            conn.close()

    def release(self, owner_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                DELETE FROM coordinator_lock
                WHERE id = 1 AND owner_id = ?
                """,
                [owner_id],
            )
        finally:
            conn.close()

    def current_owner(self) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT owner_id, expires_at FROM coordinator_lock WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            expires = _parse(row["expires_at"])
            if expires is None or expires <= _utcnow():
                return None
            return row["owner_id"]
        finally:
            conn.close()
