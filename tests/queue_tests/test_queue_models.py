"""Unit tests for yokai.queue.models."""

from datetime import datetime

from yokai.queue.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    Job,
    JobResult,
    JobStatus,
)


class TestJobStatus:
    def test_all_status_values_are_lowercase_strings(self):
        for status in JobStatus:
            assert isinstance(status.value, str)
            assert status.value == status.value.lower()

    def test_terminal_states_are_a_subset_of_all_status(self):
        for s in TERMINAL_STATES:
            assert isinstance(s, JobStatus)

    def test_terminal_states_have_no_outgoing_transitions(self):
        for status in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[status] == frozenset()

    def test_every_status_appears_in_allowed_transitions(self):
        for status in JobStatus:
            assert status in ALLOWED_TRANSITIONS


class TestJob:
    def test_new_generates_uuid(self):
        a = Job.new("KEY-1", "repo-x", {"foo": "bar"})
        b = Job.new("KEY-2", "repo-y", {})
        assert a.job_id != b.job_id
        assert len(a.job_id) >= 32  # uuid4 hex form

    def test_new_default_status_is_pending(self):
        job = Job.new("K-1", "r", {})
        assert job.status == JobStatus.PENDING

    def test_new_default_attempts_zero(self):
        job = Job.new("K-1", "r", {})
        assert job.attempts == 0

    def test_new_default_max_attempts_three(self):
        job = Job.new("K-1", "r", {})
        assert job.max_attempts == 3

    def test_new_custom_max_attempts(self):
        job = Job.new("K-1", "r", {}, max_attempts=10)
        assert job.max_attempts == 10

    def test_created_at_and_updated_at_are_set(self):
        job = Job.new("K-1", "r", {})
        assert isinstance(job.created_at, datetime)
        assert isinstance(job.updated_at, datetime)

    def test_picked_up_at_initially_none(self):
        job = Job.new("K-1", "r", {})
        assert job.picked_up_at is None
        assert job.completed_at is None
        assert job.worker_id is None
        assert job.last_error is None

    def test_is_terminal_true_for_done(self):
        job = Job.new("K-1", "r", {})
        job.status = JobStatus.DONE
        assert job.is_terminal()

    def test_is_terminal_true_for_failed(self):
        job = Job.new("K-1", "r", {})
        job.status = JobStatus.FAILED
        assert job.is_terminal()

    def test_is_terminal_true_for_dead_lettered(self):
        job = Job.new("K-1", "r", {})
        job.status = JobStatus.DEAD_LETTERED
        assert job.is_terminal()

    def test_is_terminal_false_for_running(self):
        job = Job.new("K-1", "r", {})
        job.status = JobStatus.AGENT_RUNNING
        assert not job.is_terminal()

    def test_can_retry_within_attempt_budget(self):
        job = Job.new("K-1", "r", {}, max_attempts=3)
        job.attempts = 1
        assert job.can_retry()

    def test_cannot_retry_when_max_attempts_reached(self):
        job = Job.new("K-1", "r", {}, max_attempts=3)
        job.attempts = 3
        assert not job.can_retry()

    def test_cannot_retry_when_terminal(self):
        job = Job.new("K-1", "r", {}, max_attempts=10)
        job.attempts = 0
        job.status = JobStatus.DONE
        assert not job.can_retry()

    def test_payload_preserves_arbitrary_dict(self):
        payload = {"summary": "fix bug", "components": ["EMU-BE"]}
        job = Job.new("K-1", "r", payload)
        assert job.payload == payload


class TestJobResult:
    def test_default_success_required(self):
        r = JobResult(job_id="x", success=True)
        assert r.success is True
        assert r.agent_output == ""
        assert r.error is None

    def test_failed_result_carries_error(self):
        r = JobResult(
            job_id="x",
            success=False,
            error="agent crashed",
            traceback="Traceback ...",
        )
        assert r.success is False
        assert r.error == "agent crashed"
        assert r.traceback == "Traceback ..."

    def test_completed_at_default_utc(self):
        r = JobResult(job_id="x", success=True)
        assert isinstance(r.completed_at, datetime)
        assert r.completed_at.tzinfo is not None
