"""Redis-specific tests using fakeredis.

The shared backend contract tests already verify functional behaviour.
These tests cover Redis-specific concerns: shared client (multi-instance
on same fake server), TTL behaviour, key layout, and graceful import.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

fakeredis = pytest.importorskip("fakeredis")
from yokai.queue import (
    DuplicateJobError,
    Job,
    JobResult,
    JobStatus,
)
from yokai.queue.backends.redis import RedisBackend


@pytest.fixture
def shared_server():
    """Single FakeServer instance: two RedisBackend clients on it
    behave as if they were two processes hitting the same Redis."""
    server = fakeredis.FakeServer()
    return server


def _make_backend(server):
    fake = fakeredis.FakeRedis(server=server, decode_responses=True)
    return RedisBackend(client=fake)


class TestSharedServer:
    """Two RedisBackend instances on the same fake server simulate
    cross-process coordination."""

    def test_worker_sees_jobs_enqueued_by_coordinator(self, shared_server):
        coord = _make_backend(shared_server)
        worker = _make_backend(shared_server)

        coord.enqueue(Job.new("S-1", "r", {}))

        job = worker.dequeue("worker-1", timedelta(seconds=60))
        assert job is not None
        assert job.story_key == "S-1"
        assert job.worker_id == "worker-1"

    def test_coordinator_sees_status_change_from_worker(self, shared_server):
        coord = _make_backend(shared_server)
        worker = _make_backend(shared_server)

        j = coord.enqueue(Job.new("S-1", "r", {}))
        job = worker.dequeue("w", timedelta(seconds=60))
        worker.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")

        retrieved = coord.get(j.job_id)
        assert retrieved.status == JobStatus.AGENT_RUNNING

    def test_two_workers_cannot_pick_same_job(self, shared_server):
        coord = _make_backend(shared_server)
        w1 = _make_backend(shared_server)
        w2 = _make_backend(shared_server)

        coord.enqueue(Job.new("S-1", "r", {}))

        j1 = w1.dequeue("worker-1", timedelta(seconds=60))
        j2 = w2.dequeue("worker-2", timedelta(seconds=60))

        assert (j1 is not None) != (j2 is not None)

    def test_dedupe_across_clients(self, shared_server):
        a = _make_backend(shared_server)
        b = _make_backend(shared_server)

        a.enqueue(Job.new("S-DUP", "r", {}))
        with pytest.raises(DuplicateJobError):
            b.enqueue(Job.new("S-DUP", "r", {}))


class TestKeyLayout:
    """Verify Redis keys follow the documented `yokai:` prefix layout
    so coexistence with other apps in the same Redis is safe."""

    def test_all_keys_use_yokai_prefix(self, shared_server):
        b = _make_backend(shared_server)

        # Trigger creation of every key family
        job = Job.new("S-1", "r", {"x": 1})
        b.enqueue(job)
        picked = b.dequeue("w", timedelta(seconds=60))
        b.update_status(picked.job_id, JobStatus.AGENT_RUNNING, "w")
        b.put(JobResult(job_id=picked.job_id, success=True, branch_name="b"))

        from yokai.queue.models import WorkerInfo
        from datetime import datetime, timezone
        b.register(
            WorkerInfo(
                worker_id="w-x",
                hostname="h",
                pid=1,
                started_at=datetime.now(timezone.utc),
                last_heartbeat_at=datetime.now(timezone.utc),
            )
        )
        b.acquire("c1", timedelta(seconds=60))

        client = b._r
        keys = list(client.scan_iter("*"))
        assert keys, "no keys created at all"
        non_yokai = [k for k in keys if not k.startswith("yokai:")]
        assert non_yokai == [], f"non-yokai keys present: {non_yokai}"


class TestTTLs:
    """Verify TTL-based behaviours (lease keys, worker registration)."""

    def test_lease_key_has_ttl_when_dequeued(self, shared_server):
        b = _make_backend(shared_server)
        b.enqueue(Job.new("S-1", "r", {}))
        job = b.dequeue("w", timedelta(seconds=120))

        ttl = b._r.ttl(f"yokai:lease:{job.job_id}")
        assert 0 < ttl <= 120

    def test_worker_key_has_ttl(self, shared_server):
        from yokai.queue.models import WorkerInfo
        from datetime import datetime, timezone

        b = RedisBackend(
            client=fakeredis.FakeRedis(
                server=shared_server, decode_responses=True
            ),
            worker_ttl_seconds=45,
        )
        b.register(
            WorkerInfo(
                worker_id="w1",
                hostname="h",
                pid=1,
                started_at=datetime.now(timezone.utc),
                last_heartbeat_at=datetime.now(timezone.utc),
            )
        )
        ttl = b._r.ttl("yokai:worker:w1")
        assert 0 < ttl <= 45

    def test_coord_lock_has_ttl(self, shared_server):
        b = _make_backend(shared_server)
        b.acquire("c1", timedelta(seconds=90))
        ttl = b._r.ttl("yokai:coord:lock")
        assert 0 < ttl <= 90

    def test_release_drops_lock_key(self, shared_server):
        b = _make_backend(shared_server)
        b.acquire("c1", timedelta(seconds=90))
        b.release("c1")
        assert b._r.get("yokai:coord:lock") is None


class TestFlushAllYokaiKeys:
    def test_flush_removes_all_yokai_keys(self, shared_server):
        b = _make_backend(shared_server)
        b.enqueue(Job.new("S-1", "r", {}))
        b.acquire("c1", timedelta(seconds=60))

        # Add a non-yokai key as poison: must NOT be deleted
        b._r.set("other-app:foo", "bar")

        n = b.flush_all_yokai_keys()
        assert n > 0
        assert b._r.get("other-app:foo") == "bar"
        assert b._r.get("yokai:coord:lock") is None
        with pytest.raises(Exception):
            b.get("does-not-exist")  # any get raises JobNotFound


class TestErrorPaths:
    def test_init_without_client_or_url_raises(self):
        from yokai.queue.exceptions import QueueBackendError

        with pytest.raises(QueueBackendError, match="client or a url"):
            RedisBackend()

    def test_init_rejects_decode_false(self, shared_server):
        from yokai.queue.exceptions import QueueBackendError

        bad_client = fakeredis.FakeRedis(
            server=shared_server, decode_responses=False
        )
        with pytest.raises(QueueBackendError, match="decode_responses"):
            RedisBackend(client=bad_client)


class TestStaleResultsCleanup:
    def test_pending_for_postprocessing_skips_orphan_result_ids(
        self, shared_server
    ):
        b = _make_backend(shared_server)
        # Manually inject a stale entry in the pending set with no
        # corresponding job
        b._r.sadd("yokai:results:pending", "orphan-job-id")
        # Should not crash and should drop the orphan
        results = b.pending_for_postprocessing()
        assert results == []
        assert "orphan-job-id" not in b._r.smembers("yokai:results:pending")
