"""SQLite backend tests for persistence behaviour.

These tests are specific to the SQLite backend - they verify that
state survives across process-equivalent boundaries (closing and
reopening the backend) and that two SqliteBackend instances pointing
at the same file see each other's changes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from yokai.queue import (
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)
from yokai.queue.backends.sqlite import SqliteBackend


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "queue.db"


class TestPersistence:
    def test_jobs_survive_backend_recreation(self, db_path):
        b1 = SqliteBackend(db_path)
        job = b1.enqueue(Job.new("S-1", "r", {"x": 1}))

        # Simulate a process restart: discard b1, create fresh backend
        del b1
        b2 = SqliteBackend(db_path)

        retrieved = b2.get(job.job_id)
        assert retrieved.story_key == "S-1"
        assert retrieved.payload == {"x": 1}
        assert retrieved.status == JobStatus.QUEUED

    def test_results_survive_backend_recreation(self, db_path):
        b1 = SqliteBackend(db_path)
        b1.put(JobResult(job_id="abc", success=True, agent_output="hello"))
        del b1

        b2 = SqliteBackend(db_path)
        r = b2.get_result("abc")
        assert r is not None
        assert r.agent_output == "hello"

    def test_workers_survive_backend_recreation(self, db_path):
        from datetime import datetime, timezone

        b1 = SqliteBackend(db_path)
        now = datetime.now(timezone.utc)
        w = WorkerInfo(
            worker_id="w1",
            hostname="host",
            pid=42,
            started_at=now,
            last_heartbeat_at=now,
        )
        b1.register(w)
        del b1

        b2 = SqliteBackend(db_path)
        alive = b2.list_alive(timedelta(seconds=60))
        assert any(w.worker_id == "w1" for w in alive)

    def test_coordinator_lock_survives_backend_recreation(self, db_path):
        b1 = SqliteBackend(db_path)
        assert b1.acquire("c1", timedelta(seconds=60))
        del b1

        b2 = SqliteBackend(db_path)
        assert b2.current_owner() == "c1"
        # A different coordinator instance cannot steal it
        assert not b2.acquire("c2", timedelta(seconds=60))


class TestCrossProcessSafety:
    """Two SqliteBackend instances sharing a DB file simulate two
    separate yokai processes (coordinator + worker, or two workers)."""

    def test_worker_sees_jobs_enqueued_by_coordinator(self, db_path):
        coordinator = SqliteBackend(db_path)
        worker = SqliteBackend(db_path)

        coordinator.enqueue(Job.new("S-1", "r", {}))

        job = worker.dequeue("worker-1", timedelta(seconds=60))
        assert job is not None
        assert job.story_key == "S-1"
        assert job.worker_id == "worker-1"

    def test_coordinator_sees_status_change_from_worker(self, db_path):
        coordinator = SqliteBackend(db_path)
        worker = SqliteBackend(db_path)

        j = coordinator.enqueue(Job.new("S-1", "r", {}))
        job = worker.dequeue("w", timedelta(seconds=60))
        worker.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")

        # Coordinator immediately sees the new status
        retrieved = coordinator.get(j.job_id)
        assert retrieved.status == JobStatus.AGENT_RUNNING

    def test_two_workers_cannot_pick_same_job(self, db_path):
        coordinator = SqliteBackend(db_path)
        w1 = SqliteBackend(db_path)
        w2 = SqliteBackend(db_path)

        coordinator.enqueue(Job.new("S-1", "r", {}))

        j1 = w1.dequeue("worker-1", timedelta(seconds=60))
        j2 = w2.dequeue("worker-2", timedelta(seconds=60))

        # Exactly one of the two gets the job
        assert (j1 is not None) != (j2 is not None)


class TestSchema:
    def test_schema_creates_jobs_table(self, db_path):
        SqliteBackend(db_path)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            names = {t[0] for t in tables}
            assert "jobs" in names
            assert "results" in names
            assert "workers" in names
            assert "coordinator_lock" in names
        finally:
            conn.close()

    def test_schema_creates_indices(self, db_path):
        SqliteBackend(db_path)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            indices = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            names = {i[0] for i in indices}
            assert "idx_jobs_status_created" in names
            assert "idx_jobs_story_key_status" in names
        finally:
            conn.close()

    def test_init_is_idempotent(self, db_path):
        # Creating two SqliteBackend on the same file must not error
        SqliteBackend(db_path)
        SqliteBackend(db_path)  # should not raise

    def test_wal_mode_is_enabled(self, db_path):
        SqliteBackend(db_path)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            conn.close()
