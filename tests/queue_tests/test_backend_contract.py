"""Shared test suite for all queue backends.

The same set of behavioural assertions is parametrized over each
backend implementation (memory, sqlite). Both backends must pass
identical semantics; if they diverge, one of them has a bug.

Usage:
    pytest tests/unit/test_backend_contract.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yokai.queue import (
    DuplicateJobError,
    InvalidStateTransition,
    Job,
    JobNotFound,
    JobResult,
    JobStatus,
    LeaseExpiredError,
    WorkerInfo,
)
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.backends.sqlite import SqliteBackend


def _make_redis_backend():
    """Build a RedisBackend backed by fakeredis for tests.

    Returns None if fakeredis is unavailable.
    """
    try:
        import fakeredis
        from yokai.queue.backends.redis import RedisBackend
    except ImportError:
        return None
    fake = fakeredis.FakeRedis(decode_responses=True)
    fake.flushall()
    return RedisBackend(client=fake)


_BACKEND_PARAMS = ["memory", "sqlite"]
if _make_redis_backend() is not None:
    _BACKEND_PARAMS.append("redis")

@pytest.fixture(params=_BACKEND_PARAMS)
def backend(request, tmp_path: Path):
    """Returns a fresh backend instance for each test."""
    if request.param == "memory":
        return InMemoryBackend()
    elif request.param == "sqlite":
        db = tmp_path / "queue.db"
        return SqliteBackend(db)
    elif request.param == "redis":
        b = _make_redis_backend()
        assert b is not None, "fakeredis required for redis tests"
        return b
    else:
        raise ValueError(f"Unknown backend: {request.param}")


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

class TestEnqueueContract:
    def test_enqueue_returns_job_in_queued_status(self, backend):
        job = Job.new("STORY-1", "repo", {"foo": "bar"})
        result = backend.enqueue(job)
        assert result.status == JobStatus.QUEUED
        assert result.job_id == job.job_id

    def test_enqueued_job_is_retrievable(self, backend):
        job = Job.new("STORY-1", "repo", {})
        backend.enqueue(job)
        retrieved = backend.get(job.job_id)
        assert retrieved.story_key == "STORY-1"

    def test_enqueue_rejects_duplicate_in_flight_story(self, backend):
        backend.enqueue(Job.new("STORY-1", "repo", {}))
        with pytest.raises(DuplicateJobError, match="STORY-1"):
            backend.enqueue(Job.new("STORY-1", "repo", {}))

    def test_enqueue_allows_same_story_after_terminal(self, backend, lease_5s):
        first = backend.enqueue(Job.new("STORY-1", "repo", {}))
        backend.dequeue("worker-A", lease_5s)
        backend.update_status(
            first.job_id, JobStatus.AGENT_RUNNING, "worker-A"
        )
        backend.update_status(
            first.job_id, JobStatus.AGENT_COMPLETED, "worker-A"
        )
        backend.update_status(first.job_id, JobStatus.POSTPROCESSING)
        backend.update_status(first.job_id, JobStatus.DONE)
        second = backend.enqueue(Job.new("STORY-1", "repo", {}))
        assert second.job_id != first.job_id

    def test_payload_is_preserved_through_roundtrip(self, backend):
        payload = {
            "summary": "fix bug",
            "components": ["EMU-BE", "EMU-FE"],
            "nested": {"key": "value", "n": 42},
        }
        job = Job.new("S-1", "r", payload)
        backend.enqueue(job)
        retrieved = backend.get(job.job_id)
        assert retrieved.payload == payload


class TestDequeueContract:
    def test_returns_none_when_empty(self, backend, lease_5s):
        assert backend.dequeue("w", lease_5s) is None

    def test_returns_oldest_first(self, backend, lease_5s):
        a = backend.enqueue(Job.new("S-1", "r", {}))
        # ensure b has a strictly later created_at on fast machines too
        import time
        time.sleep(0.001)
        b = backend.enqueue(Job.new("S-2", "r", {}))
        first = backend.dequeue("w", lease_5s)
        assert first.job_id == a.job_id

    def test_dequeued_job_is_picked_up(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        assert job.status == JobStatus.PICKED_UP
        assert job.worker_id == "worker-A"
        assert job.picked_up_at is not None
        assert job.attempts == 1

    def test_dequeue_does_not_return_picked_up_jobs(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", lease_5s)
        assert backend.dequeue("w", lease_5s) is None


class TestUpdateStatusContract:
    def test_valid_transition(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        updated = backend.update_status(
            job.job_id, JobStatus.AGENT_RUNNING, "w"
        )
        assert updated.status == JobStatus.AGENT_RUNNING

    def test_invalid_transition_raises(self, backend):
        j = backend.enqueue(Job.new("S-1", "r", {}))
        with pytest.raises(InvalidStateTransition):
            backend.update_status(j.job_id, JobStatus.DONE)

    def test_unknown_job_raises(self, backend):
        with pytest.raises(JobNotFound):
            backend.update_status("does-not-exist", JobStatus.QUEUED)

    def test_lease_check_rejects_other_worker(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        with pytest.raises(LeaseExpiredError):
            backend.update_status(
                job.job_id, JobStatus.AGENT_RUNNING, "worker-B"
            )

    def test_coordinator_can_force_transition_without_worker_id(
        self, backend, lease_5s
    ):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        # Coordinator reclaim does not need worker_id
        result = backend.update_status(job.job_id, JobStatus.QUEUED)
        assert result.status == JobStatus.QUEUED
        assert result.worker_id is None

    def test_error_message_persists(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        backend.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(
            job.job_id,
            JobStatus.AGENT_FAILED,
            "w",
            error="something went wrong",
        )
        retrieved = backend.get(job.job_id)
        assert retrieved.last_error == "something went wrong"

    def test_terminal_state_sets_completed_at(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        backend.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(job.job_id, JobStatus.AGENT_COMPLETED, "w")
        backend.update_status(job.job_id, JobStatus.POSTPROCESSING)
        backend.update_status(job.job_id, JobStatus.DONE)
        retrieved = backend.get(job.job_id)
        assert retrieved.completed_at is not None


class TestListByStatusContract:
    def test_filters_by_status(self, backend, lease_5s):
        a = backend.enqueue(Job.new("S-1", "r", {}))
        b = backend.enqueue(Job.new("S-2", "r", {}))
        backend.dequeue("w", lease_5s)
        queued = backend.list_by_status(JobStatus.QUEUED)
        picked = backend.list_by_status(JobStatus.PICKED_UP)
        assert {j.job_id for j in queued} == {b.job_id}
        assert {j.job_id for j in picked} == {a.job_id}

    def test_respects_limit(self, backend):
        for i in range(7):
            backend.enqueue(Job.new(f"S-{i}", "r", {}))
        result = backend.list_by_status(JobStatus.QUEUED, limit=3)
        assert len(result) == 3


class TestReclaimContract:
    def test_does_not_reclaim_active_lease(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", timedelta(seconds=60))
        assert backend.reclaim_expired_leases() == []

    def test_reclaims_expired_lease_back_to_queued(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}, max_attempts=3))
        backend.dequeue("w", timedelta(seconds=-1))
        reclaimed = backend.reclaim_expired_leases()
        assert len(reclaimed) == 1
        assert reclaimed[0].status == JobStatus.QUEUED

    def test_reclaim_dead_letters_when_attempts_exhausted(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}, max_attempts=1))
        backend.dequeue("w", timedelta(seconds=-1))
        reclaimed = backend.reclaim_expired_leases()
        assert len(reclaimed) == 1
        assert reclaimed[0].status == JobStatus.DEAD_LETTERED


class TestStatsContract:
    def test_empty_returns_all_zero(self, backend):
        stats = backend.stats()
        for status in JobStatus:
            assert stats[status] == 0

    def test_counts_by_status(self, backend, lease_5s):
        for i in range(3):
            backend.enqueue(Job.new(f"S-{i}", "r", {}))
        backend.dequeue("w", lease_5s)
        stats = backend.stats()
        assert stats[JobStatus.QUEUED] == 2
        assert stats[JobStatus.PICKED_UP] == 1

class TestResultStoreContract:
    def test_put_then_get_roundtrip(self, backend):
        r = JobResult(
            job_id="abc",
            success=True,
            agent_output="ok",
            duration_seconds=12.5,
            branch_name="feature/x",
            commit_sha="deadbeef",
        )
        backend.put(r)
        retrieved = backend.get_result("abc")
        assert retrieved is not None
        assert retrieved.success is True
        assert retrieved.agent_output == "ok"
        assert retrieved.duration_seconds == 12.5
        assert retrieved.branch_name == "feature/x"
        assert retrieved.commit_sha == "deadbeef"

    def test_get_unknown_returns_none(self, backend):
        assert backend.get_result("missing") is None

    def test_put_overwrites(self, backend):
        backend.put(JobResult(job_id="abc", success=False, error="first"))
        backend.put(JobResult(job_id="abc", success=True, agent_output="ok"))
        r = backend.get_result("abc")
        assert r.success is True
        assert r.error is None

    def test_pending_for_postprocessing(self, backend, lease_5s):
        # Job in AGENT_COMPLETED with success result -> returned
        j1 = backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", lease_5s)
        backend.update_status(j1.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(j1.job_id, JobStatus.AGENT_COMPLETED, "w")
        backend.put(JobResult(job_id=j1.job_id, success=True))

        # Job in QUEUED -> not returned
        j2 = backend.enqueue(Job.new("S-2", "r", {}))

        pending = backend.pending_for_postprocessing()
        ids = [p.job_id for p in pending]
        assert j1.job_id in ids
        assert j2.job_id not in ids

    def test_pending_excludes_failed_results(self, backend, lease_5s):
        j = backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", lease_5s)
        backend.update_status(j.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(j.job_id, JobStatus.AGENT_COMPLETED, "w")
        backend.put(JobResult(job_id=j.job_id, success=False, error="x"))
        pending = backend.pending_for_postprocessing()
        assert pending == []

class TestWorkerRegistryContract:
    def test_register_then_alive(self, backend):
        backend.register(make_worker("w1"))
        alive = backend.list_alive(timedelta(seconds=60))
        assert any(w.worker_id == "w1" for w in alive)

    def test_heartbeat_updates_timestamp(self, backend):
        w = make_worker("w1")
        backend.register(w)
        old = w.last_heartbeat_at
        import time
        time.sleep(0.01)
        backend.heartbeat("w1", current_job_id="job-42")
        alive = backend.list_alive(timedelta(seconds=60))
        match = next(x for x in alive if x.worker_id == "w1")
        assert match.last_heartbeat_at > old
        assert match.current_job_id == "job-42"

    def test_heartbeat_for_unknown_worker_is_noop(self, backend):
        backend.heartbeat("ghost", current_job_id=None)
        assert not any(
            w.worker_id == "ghost"
            for w in backend.list_alive(timedelta(seconds=60))
        )

    def test_deregister_removes_worker(self, backend):
        backend.register(make_worker("w1"))
        backend.deregister("w1")
        assert not any(
            w.worker_id == "w1"
            for w in backend.list_alive(timedelta(seconds=60))
        )

    def test_dead_workers_classified_correctly(self, backend):
        old = make_worker("old")
        old.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(
            minutes=10
        )
        backend.register(old)
        backend.register(make_worker("fresh"))
        alive = {w.worker_id for w in backend.list_alive(timedelta(seconds=60))}
        dead = {w.worker_id for w in backend.list_dead(timedelta(seconds=60))}
        assert "fresh" in alive
        assert "old" in dead
        assert alive.isdisjoint(dead)

class TestCoordinatorLockContract:
    def test_acquire_unowned(self, backend):
        assert backend.acquire("c1", timedelta(seconds=60))

    def test_acquire_fails_when_owned_by_other(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        assert not backend.acquire("c2", timedelta(seconds=60))

    def test_reacquire_by_same_owner(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        assert backend.acquire("c1", timedelta(seconds=60))

    def test_acquire_after_expiry(self, backend):
        backend.acquire("c1", timedelta(seconds=-1))
        assert backend.acquire("c2", timedelta(seconds=60))

    def test_renew_extends(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        assert backend.renew("c1", timedelta(seconds=60))

    def test_renew_fails_for_other(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        assert not backend.renew("c2", timedelta(seconds=60))

    def test_release_frees(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        backend.release("c1")
        assert backend.acquire("c2", timedelta(seconds=60))

    def test_release_by_non_owner_is_noop(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        backend.release("c2")
        assert backend.current_owner() == "c1"

    def test_current_owner(self, backend):
        backend.acquire("c1", timedelta(seconds=60))
        assert backend.current_owner() == "c1"

    def test_current_owner_none_when_no_one_holds(self, backend):
        assert backend.current_owner() is None

    def test_current_owner_none_when_expired(self, backend):
        backend.acquire("c1", timedelta(seconds=-1))
        assert backend.current_owner() is None
