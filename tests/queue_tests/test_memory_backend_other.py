"""Unit tests for InMemoryBackend - ResultStore, WorkerRegistry,
CoordinatorLock interfaces."""

from datetime import datetime, timedelta, timezone

import pytest

from yokai.queue import (
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)
from yokai.queue.backends.memory import InMemoryBackend


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def lease_5s():
    return timedelta(seconds=5)


def make_worker(worker_id: str = "worker-A") -> WorkerInfo:
    now = datetime.now(timezone.utc)
    return WorkerInfo(
        worker_id=worker_id,
        hostname="testhost",
        pid=12345,
        started_at=now,
        last_heartbeat_at=now,
    )

class TestResultStore:
    def test_put_then_get_roundtrip(self, backend):
        r = JobResult(job_id="abc", success=True, agent_output="ok")
        backend.put(r)
        retrieved = backend.get_result("abc")
        assert retrieved is not None
        assert retrieved.job_id == "abc"
        assert retrieved.agent_output == "ok"

    def test_get_unknown_returns_none(self, backend):
        assert backend.get_result("missing") is None

    def test_put_overwrites_previous(self, backend):
        backend.put(JobResult(job_id="abc", success=False, error="first"))
        backend.put(JobResult(job_id="abc", success=True, agent_output="retry ok"))
        r = backend.get_result("abc")
        assert r.success is True
        assert r.agent_output == "retry ok"

    def test_get_returns_independent_copy(self, backend):
        backend.put(JobResult(job_id="abc", success=True, agent_output="x"))
        r1 = backend.get_result("abc")
        r1.agent_output = "MUTATED"
        r2 = backend.get_result("abc")
        assert r2.agent_output == "x"

    def test_pending_for_postprocessing_returns_only_completed_successful(
        self, backend, lease_5s
    ):
        # Job 1: AGENT_COMPLETED + success result -> should be returned
        j1 = backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", lease_5s)
        backend.update_status(j1.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(j1.job_id, JobStatus.AGENT_COMPLETED, "w")
        backend.put(JobResult(job_id=j1.job_id, success=True))

        # Job 2: still QUEUED + no result -> not returned
        j2 = backend.enqueue(Job.new("S-2", "r", {}))

        # Job 3: AGENT_COMPLETED but failed result -> not returned
        j3 = backend.enqueue(Job.new("S-3", "r", {}))
        # Drain j2 first (oldest), then j3
        backend.dequeue("w", lease_5s)  # picks j2
        backend.dequeue("w2", lease_5s)  # picks j3
        backend.update_status(j3.job_id, JobStatus.AGENT_RUNNING, "w2")
        backend.update_status(j3.job_id, JobStatus.AGENT_COMPLETED, "w2")
        backend.put(JobResult(job_id=j3.job_id, success=False, error="x"))

        pending = backend.pending_for_postprocessing()
        ids = [p.job_id for p in pending]
        assert j1.job_id in ids
        assert j2.job_id not in ids
        assert j3.job_id not in ids

    def test_pending_for_postprocessing_respects_limit(self, backend, lease_5s):
        for i in range(5):
            j = backend.enqueue(Job.new(f"S-{i}", "r", {}))
            backend.dequeue("w", lease_5s)
            backend.update_status(j.job_id, JobStatus.AGENT_RUNNING, "w")
            backend.update_status(j.job_id, JobStatus.AGENT_COMPLETED, "w")
            backend.put(JobResult(job_id=j.job_id, success=True))
        result = backend.pending_for_postprocessing(limit=2)
        assert len(result) == 2

class TestWorkerRegistry:
    def test_register_then_alive(self, backend):
        backend.register(make_worker("w1"))
        alive = backend.list_alive(timedelta(seconds=60))
        assert len(alive) == 1
        assert alive[0].worker_id == "w1"

    def test_heartbeat_updates_timestamp(self, backend):
        w = make_worker("w1")
        backend.register(w)
        old = w.last_heartbeat_at
        backend.heartbeat("w1", current_job_id="job-42")
        alive = backend.list_alive(timedelta(seconds=60))
        assert alive[0].last_heartbeat_at >= old
        assert alive[0].current_job_id == "job-42"

    def test_heartbeat_for_unknown_worker_is_noop(self, backend):
        # Should not raise
        backend.heartbeat("ghost", current_job_id=None)
        assert backend.list_alive(timedelta(seconds=60)) == []

    def test_deregister_removes_worker(self, backend):
        backend.register(make_worker("w1"))
        backend.deregister("w1")
        assert backend.list_alive(timedelta(seconds=60)) == []

    def test_dead_workers_have_old_heartbeat(self, backend):
        old_worker = make_worker("old")
        old_worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            minutes=10
        )
        backend.register(old_worker)
        backend.register(make_worker("fresh"))

        alive = backend.list_alive(timedelta(seconds=60))
        dead = backend.list_dead(timedelta(seconds=60))
        alive_ids = [w.worker_id for w in alive]
        dead_ids = [w.worker_id for w in dead]
        assert "fresh" in alive_ids
        assert "old" in dead_ids
        assert "fresh" not in dead_ids
        assert "old" not in alive_ids

class TestCoordinatorLock:
    def test_acquire_succeeds_when_unowned(self, backend):
        assert backend.acquire("coord-1", timedelta(seconds=60))

    def test_acquire_fails_when_held_by_other(self, backend):
        backend.acquire("coord-1", timedelta(seconds=60))
        assert not backend.acquire("coord-2", timedelta(seconds=60))

    def test_reacquire_by_same_owner_succeeds(self, backend):
        backend.acquire("coord-1", timedelta(seconds=60))
        assert backend.acquire("coord-1", timedelta(seconds=60))

    def test_acquire_succeeds_after_lease_expiry(self, backend):
        backend.acquire("coord-1", timedelta(seconds=-1))
        assert backend.acquire("coord-2", timedelta(seconds=60))

    def test_renew_extends_lease(self, backend):
        backend.acquire("coord-1", timedelta(seconds=1))
        assert backend.renew("coord-1", timedelta(seconds=60))

    def test_renew_fails_for_non_owner(self, backend):
        backend.acquire("coord-1", timedelta(seconds=60))
        assert not backend.renew("coord-2", timedelta(seconds=60))

    def test_release_frees_lock(self, backend):
        backend.acquire("coord-1", timedelta(seconds=60))
        backend.release("coord-1")
        assert backend.acquire("coord-2", timedelta(seconds=60))

    def test_release_by_non_owner_is_noop(self, backend):
        backend.acquire("coord-1", timedelta(seconds=60))
        backend.release("coord-2")
        # coord-1 should still own it
        assert backend.current_owner() == "coord-1"

    def test_current_owner_returns_holder(self, backend):
        backend.acquire("coord-1", timedelta(seconds=60))
        assert backend.current_owner() == "coord-1"

    def test_current_owner_returns_none_when_expired(self, backend):
        backend.acquire("coord-1", timedelta(seconds=-1))
        assert backend.current_owner() is None
