"""Tests for ResultHandler with a fake Postprocessor."""

from datetime import timedelta

import pytest

from yokai.queue import (
    Job,
    JobResult,
    JobStatus,
    PostprocessOutcome,
    Postprocessor,
    ResultHandler,
    ResultHandlerSettings,
)
from yokai.queue.backends.memory import InMemoryBackend


class FakePostprocessor(Postprocessor):
    def __init__(self):
        self.outcomes_to_return: list[PostprocessOutcome] = []
        self.default_outcome = PostprocessOutcome(
            success=True, pr_url="https://bb.example/pr/1"
        )
        self.raise_exc: Exception | None = None
        self.invocations: list[str] = []

    def run(self, job, result) -> PostprocessOutcome:
        self.invocations.append(job.job_id)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.outcomes_to_return:
            return self.outcomes_to_return.pop(0)
        return self.default_outcome


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def postproc():
    return FakePostprocessor()


@pytest.fixture
def handler(backend, postproc):
    return ResultHandler(
        queue=backend,
        results=backend,
        postprocessor=postproc,
        settings=ResultHandlerSettings(
            poll_interval_seconds=0.01, batch_size=10
        ),
    )


def setup_completed_job(
    backend, story_key="S-1", success_result=True
) -> Job:
    """Helper: enqueue + dequeue + run agent + write result, leaving
    the job in AGENT_COMPLETED with the requested result."""
    j = backend.enqueue(Job.new(story_key, "r", {}))
    backend.dequeue("w", timedelta(seconds=60))
    backend.update_status(j.job_id, JobStatus.AGENT_RUNNING, "w")
    backend.update_status(j.job_id, JobStatus.AGENT_COMPLETED, "w")
    backend.put(
        JobResult(
            job_id=j.job_id, success=success_result, agent_output="output"
        )
    )
    return j


class TestRunOnceHappyPath:
    def test_processes_completed_job_to_done(
        self, handler, backend, postproc
    ):
        j = setup_completed_job(backend)
        stats = handler.run_once()
        assert stats.fetched == 1
        assert stats.succeeded == 1
        assert stats.failed == 0
        retrieved = backend.get(j.job_id)
        assert retrieved.status == JobStatus.DONE
        assert postproc.invocations == [j.job_id]

    def test_returns_zero_stats_when_no_pending(self, handler):
        stats = handler.run_once()
        assert stats.fetched == 0
        assert stats.processed == 0
        assert stats.succeeded == 0

    def test_processes_multiple_jobs_in_batch(
        self, handler, backend
    ):
        for i in range(3):
            setup_completed_job(backend, story_key=f"S-{i}")
        stats = handler.run_once()
        assert stats.fetched == 3
        assert stats.succeeded == 3
        assert backend.stats()[JobStatus.DONE] == 3


class TestRunOncePostprocessorFailure:
    def test_outcome_failure_marks_job_failed(
        self, handler, backend, postproc
    ):
        j = setup_completed_job(backend)
        postproc.default_outcome = PostprocessOutcome(
            success=False, error="git push rejected"
        )
        stats = handler.run_once()
        assert stats.failed == 1
        assert stats.succeeded == 0
        retrieved = backend.get(j.job_id)
        assert retrieved.status == JobStatus.FAILED
        assert "git push rejected" in retrieved.last_error

    def test_postprocessor_exception_marks_job_failed(
        self, handler, backend, postproc
    ):
        j = setup_completed_job(backend)
        postproc.raise_exc = RuntimeError("connection refused")
        stats = handler.run_once()
        assert stats.failed == 1
        retrieved = backend.get(j.job_id)
        assert retrieved.status == JobStatus.FAILED
        assert "connection refused" in retrieved.last_error


class TestClaimRace:
    def test_two_handlers_only_one_processes(
        self, backend, postproc
    ):
        j = setup_completed_job(backend)

        h1 = ResultHandler(
            queue=backend, results=backend, postprocessor=postproc
        )
        h2 = ResultHandler(
            queue=backend, results=backend, postprocessor=postproc
        )

        s1 = h1.run_once()
        s2 = h2.run_once()

        # First handler processed it; second saw it already POSTPROCESSING
        # and skipped
        assert s1.succeeded == 1
        assert s2.skipped == 1 or s2.fetched == 0
        assert backend.get(j.job_id).status == JobStatus.DONE
        # Postprocessor was only invoked once
        assert postproc.invocations == [j.job_id]


class TestSkipsNonCompletedJobs:
    def test_does_not_touch_queued_job(self, handler, backend):
        # Queued job with no result - shouldn't appear in
        # pending_for_postprocessing anyway
        backend.enqueue(Job.new("S-1", "r", {}))
        stats = handler.run_once()
        assert stats.fetched == 0

    def test_does_not_touch_failed_result(self, handler, backend, postproc):
        # Setup an AGENT_COMPLETED job with a failed result
        setup_completed_job(backend, success_result=False)
        stats = handler.run_once()
        # pending_for_postprocessing filters out failed results
        assert stats.fetched == 0
        assert postproc.invocations == []


class TestBatchSize:
    def test_respects_batch_size(self, backend, postproc):
        for i in range(20):
            setup_completed_job(backend, story_key=f"S-{i}")
        handler = ResultHandler(
            queue=backend,
            results=backend,
            postprocessor=postproc,
            settings=ResultHandlerSettings(batch_size=5),
        )
        stats = handler.run_once()
        assert stats.fetched == 5
        assert stats.succeeded == 5


class TestRunLoop:
    def test_run_exits_on_stop(self, handler):
        import threading
        import time

        def stopper():
            time.sleep(0.05)
            handler.stop()

        threading.Thread(target=stopper, daemon=True).start()
        handler.run()  # should return without raising

    def test_run_processes_pending_jobs(self, handler, backend):
        import threading
        import time

        for i in range(3):
            setup_completed_job(backend, story_key=f"S-{i}")

        def stopper():
            time.sleep(0.1)
            handler.stop()

        threading.Thread(target=stopper, daemon=True).start()
        handler.run()
        assert backend.stats()[JobStatus.DONE] == 3
