"""Unit tests for InMemoryBackend - JobQueue interface."""

from datetime import timedelta

import pytest

from yokai.queue import (
    DuplicateJobError,
    InvalidStateTransition,
    Job,
    JobNotFound,
    JobStatus,
    LeaseExpiredError,
)
from yokai.queue.backends.memory import InMemoryBackend


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def lease_5s():
    return timedelta(seconds=5)


class TestEnqueue:
    def test_enqueue_returns_job_in_queued_status(self, backend):
        job = Job.new("STORY-1", "repo-x", {"foo": "bar"})
        result = backend.enqueue(job)
        assert result.status == JobStatus.QUEUED
        assert result.job_id == job.job_id

    def test_enqueued_job_is_retrievable(self, backend):
        job = Job.new("STORY-1", "repo-x", {})
        backend.enqueue(job)
        retrieved = backend.get(job.job_id)
        assert retrieved.story_key == "STORY-1"

    def test_enqueue_rejects_duplicate_in_flight_story(self, backend):
        backend.enqueue(Job.new("STORY-1", "repo", {}))
        with pytest.raises(DuplicateJobError, match="STORY-1"):
            backend.enqueue(Job.new("STORY-1", "repo", {}))

    def test_enqueue_allows_same_story_after_terminal(self, backend, lease_5s):
        first = backend.enqueue(Job.new("STORY-1", "repo", {}))
        # Walk the first job to DONE
        backend.dequeue("worker-A", lease_5s)
        backend.update_status(first.job_id, JobStatus.AGENT_RUNNING, "worker-A")
        backend.update_status(first.job_id, JobStatus.AGENT_COMPLETED, "worker-A")
        backend.update_status(first.job_id, JobStatus.POSTPROCESSING)
        backend.update_status(first.job_id, JobStatus.DONE)
        # Now enqueueing a new job for the same story is allowed
        second = backend.enqueue(Job.new("STORY-1", "repo", {}))
        assert second.job_id != first.job_id

    def test_enqueue_returns_independent_copy(self, backend):
        job = Job.new("STORY-1", "repo", {})
        result = backend.enqueue(job)
        # Mutating the returned copy must not affect the stored job
        result.story_key = "MUTATED"
        retrieved = backend.get(result.job_id)
        assert retrieved.story_key == "STORY-1"


class TestDequeue:
    def test_returns_none_when_empty(self, backend, lease_5s):
        assert backend.dequeue("worker-A", lease_5s) is None

    def test_returns_oldest_queued_job_first(self, backend, lease_5s):
        a = backend.enqueue(Job.new("S-1", "r", {}))
        b = backend.enqueue(Job.new("S-2", "r", {}))
        first = backend.dequeue("worker", lease_5s)
        assert first.job_id == a.job_id
        second = backend.dequeue("worker", lease_5s)
        assert second.job_id == b.job_id

    def test_dequeued_job_has_picked_up_status(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        assert job.status == JobStatus.PICKED_UP
        assert job.worker_id == "worker-A"
        assert job.picked_up_at is not None

    def test_dequeue_increments_attempts(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        assert job.attempts == 1

    def test_dequeued_job_not_returned_again(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        first = backend.dequeue("w", lease_5s)
        second = backend.dequeue("w", lease_5s)
        assert first is not None
        assert second is None


class TestUpdateStatus:
    def test_valid_transition_succeeds(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        updated = backend.update_status(
            job.job_id, JobStatus.AGENT_RUNNING, "w"
        )
        assert updated.status == JobStatus.AGENT_RUNNING

    def test_invalid_transition_raises(self, backend):
        j = backend.enqueue(Job.new("S-1", "r", {}))
        with pytest.raises(InvalidStateTransition):
            # QUEUED -> DONE is not allowed
            backend.update_status(j.job_id, JobStatus.DONE)

    def test_unknown_job_raises(self, backend):
        with pytest.raises(JobNotFound):
            backend.update_status("does-not-exist", JobStatus.DONE)

    def test_lease_check_passes_for_owner(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        result = backend.update_status(
            job.job_id, JobStatus.AGENT_RUNNING, "worker-A"
        )
        assert result.status == JobStatus.AGENT_RUNNING

    def test_lease_check_rejects_other_worker(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        with pytest.raises(LeaseExpiredError, match="worker-B"):
            backend.update_status(
                job.job_id, JobStatus.AGENT_RUNNING, "worker-B"
            )

    def test_lease_check_skipped_when_no_worker_id_passed(
        self, backend, lease_5s
    ):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("worker-A", lease_5s)
        # Coordinator-initiated transition (no worker_id arg) bypasses lease check
        result = backend.update_status(job.job_id, JobStatus.QUEUED)
        assert result.status == JobStatus.QUEUED
        assert result.worker_id is None

    def test_terminal_transition_clears_lease(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        backend.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")
        backend.update_status(job.job_id, JobStatus.AGENT_FAILED, "w")
        backend.update_status(job.job_id, JobStatus.FAILED)
        # Reclaim should not pick up the now-terminal job
        reclaimed = backend.reclaim_expired_leases()
        assert reclaimed == []

    def test_error_message_truncated(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        job = backend.dequeue("w", lease_5s)
        backend.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")
        long_error = "x" * 5000
        backend.update_status(
            job.job_id, JobStatus.AGENT_FAILED, "w", error=long_error
        )
        retrieved = backend.get(job.job_id)
        assert len(retrieved.last_error) <= 2000


class TestListByStatus:
    def test_lists_only_matching_status(self, backend, lease_5s):
        a = backend.enqueue(Job.new("S-1", "r", {}))
        b = backend.enqueue(Job.new("S-2", "r", {}))
        backend.dequeue("w", lease_5s)
        queued = backend.list_by_status(JobStatus.QUEUED)
        picked = backend.list_by_status(JobStatus.PICKED_UP)
        assert len(queued) == 1
        assert queued[0].job_id == b.job_id
        assert len(picked) == 1
        assert picked[0].job_id == a.job_id

    def test_respects_limit(self, backend):
        for i in range(10):
            backend.enqueue(Job.new(f"S-{i}", "r", {}))
        result = backend.list_by_status(JobStatus.QUEUED, limit=3)
        assert len(result) == 3


class TestReclaimExpiredLeases:
    def test_does_not_reclaim_active_lease(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("w", timedelta(seconds=60))
        reclaimed = backend.reclaim_expired_leases()
        assert reclaimed == []

    def test_reclaims_expired_lease_back_to_queued(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}, max_attempts=3))
        job = backend.dequeue("w", timedelta(seconds=-1))  # already expired
        reclaimed = backend.reclaim_expired_leases()
        assert len(reclaimed) == 1
        assert reclaimed[0].status == JobStatus.QUEUED
        assert reclaimed[0].worker_id is None

    def test_reclaims_to_dead_letter_when_max_attempts_reached(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}, max_attempts=1))
        backend.dequeue("w", timedelta(seconds=-1))
        # attempts is now 1 == max, reclaim should dead-letter
        reclaimed = backend.reclaim_expired_leases()
        assert len(reclaimed) == 1
        assert reclaimed[0].status == JobStatus.DEAD_LETTERED

    def test_reclaim_works_for_agent_running_state(self, backend):
        backend.enqueue(Job.new("S-1", "r", {}, max_attempts=3))
        job = backend.dequeue("w", timedelta(seconds=-1))
        backend.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")
        reclaimed = backend.reclaim_expired_leases()
        assert len(reclaimed) == 1
        assert reclaimed[0].status == JobStatus.QUEUED

    def test_reclaimed_job_becomes_dequeable_again(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}, max_attempts=3))
        backend.dequeue("worker-A", timedelta(seconds=-1))
        backend.reclaim_expired_leases()
        # New worker can now pick it up
        new_job = backend.dequeue("worker-B", lease_5s)
        assert new_job is not None
        assert new_job.worker_id == "worker-B"
        assert new_job.attempts == 2  # incremented again


class TestStats:
    def test_empty_stats(self, backend):
        stats = backend.stats()
        for status in JobStatus:
            assert stats[status] == 0

    def test_counts_all_statuses(self, backend, lease_5s):
        backend.enqueue(Job.new("S-1", "r", {}))
        backend.enqueue(Job.new("S-2", "r", {}))
        backend.enqueue(Job.new("S-3", "r", {}))
        backend.dequeue("w", lease_5s)
        stats = backend.stats()
        assert stats[JobStatus.QUEUED] == 2
        assert stats[JobStatus.PICKED_UP] == 1
        assert stats[JobStatus.DONE] == 0
