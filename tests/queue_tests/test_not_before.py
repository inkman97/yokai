"""Tests for not_before semantics in both backends."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yokai.queue import Job, JobStatus
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.backends.sqlite import SqliteBackend


@pytest.fixture(params=["memory", "sqlite", "redis"])
def backend(request, tmp_path: Path):
    if request.param == "memory":
        return InMemoryBackend()
    if request.param == "sqlite":
        return SqliteBackend(tmp_path / "queue.db")
    if request.param == "redis":
        try:
            import fakeredis
            from yokai.queue.backends.redis import RedisBackend
        except ImportError:
            pytest.skip("fakeredis not installed")
        fake = fakeredis.FakeRedis(decode_responses=True)
        fake.flushall()
        return RedisBackend(client=fake)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TestNotBefore:
    def test_dequeue_skips_job_with_future_not_before(self, backend):
        j = backend.enqueue(Job.new("S-1", "r", {}))
        # Pick it up, then requeue with future not_before
        backend.dequeue("w", timedelta(seconds=60))
        future = _utcnow() + timedelta(seconds=60)
        backend.update_status(
            j.job_id, JobStatus.QUEUED, not_before=future
        )
        # Should not be returned
        assert backend.dequeue("w2", timedelta(seconds=60)) is None

    def test_dequeue_returns_job_after_not_before_elapsed(self, backend):
        j = backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", timedelta(seconds=60))
        past = _utcnow() - timedelta(seconds=10)
        backend.update_status(j.job_id, JobStatus.QUEUED, not_before=past)
        # Should be picked up
        next_job = backend.dequeue("w2", timedelta(seconds=60))
        assert next_job is not None
        assert next_job.job_id == j.job_id

    def test_not_before_cleared_on_terminal_state(self, backend):
        j = backend.enqueue(Job.new("S-1", "r", {}, max_attempts=1))
        backend.dequeue("w", timedelta(seconds=60))
        backend.update_status(j.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(j.job_id, JobStatus.AGENT_COMPLETED, "w")
        backend.update_status(j.job_id, JobStatus.POSTPROCESSING)
        backend.update_status(j.job_id, JobStatus.DONE)
        retrieved = backend.get(j.job_id)
        assert retrieved.not_before is None

    def test_not_before_persisted_correctly(self, backend):
        j = backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", timedelta(seconds=60))
        future = _utcnow() + timedelta(minutes=5)
        backend.update_status(
            j.job_id, JobStatus.QUEUED, not_before=future
        )
        retrieved = backend.get(j.job_id)
        assert retrieved.not_before is not None
        # Allow 1s drift for SQLite timestamp roundtrip
        delta = abs((retrieved.not_before - future).total_seconds())
        assert delta < 1

    def test_dequeue_picks_oldest_eligible_job(self, backend):
        # j1 has future not_before, j2 has past not_before
        # Should pick j2 even though j1 was enqueued first
        import time

        j1 = backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", timedelta(seconds=60))
        future = _utcnow() + timedelta(seconds=60)
        backend.update_status(j1.job_id, JobStatus.QUEUED, not_before=future)

        time.sleep(0.01)
        j2 = backend.enqueue(Job.new("S-2", "r", {}))

        picked = backend.dequeue("w2", timedelta(seconds=60))
        assert picked is not None
        assert picked.job_id == j2.job_id
