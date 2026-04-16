"""Smoke tests for the yokai.queue public API: imports, exports, ABC."""

import inspect

import yokai.queue as q


class TestPublicApi:
    def test_exports_models(self):
        assert hasattr(q, "Job")
        assert hasattr(q, "JobResult")
        assert hasattr(q, "JobStatus")
        assert hasattr(q, "WorkerInfo")

    def test_exports_interfaces(self):
        assert hasattr(q, "JobQueue")
        assert hasattr(q, "ResultStore")
        assert hasattr(q, "WorkerRegistry")
        assert hasattr(q, "CoordinatorLock")

    def test_exports_exceptions(self):
        assert hasattr(q, "QueueError")
        assert hasattr(q, "InvalidStateTransition")
        assert hasattr(q, "DuplicateJobError")
        assert hasattr(q, "JobNotFound")
        assert hasattr(q, "LeaseExpiredError")

    def test_exports_state_machine_helpers(self):
        assert hasattr(q, "transition")
        assert hasattr(q, "is_allowed")
        assert hasattr(q, "is_terminal")


class TestInterfacesAreAbstract:
    def test_jobqueue_is_abstract(self):
        assert inspect.isabstract(q.JobQueue)

    def test_resultstore_is_abstract(self):
        assert inspect.isabstract(q.ResultStore)

    def test_workerregistry_is_abstract(self):
        assert inspect.isabstract(q.WorkerRegistry)

    def test_coordinatorlock_is_abstract(self):
        assert inspect.isabstract(q.CoordinatorLock)


class TestExceptionHierarchy:
    def test_invalid_state_transition_is_queue_error(self):
        assert issubclass(q.InvalidStateTransition, q.QueueError)

    def test_duplicate_job_is_queue_error(self):
        assert issubclass(q.DuplicateJobError, q.QueueError)

    def test_job_not_found_is_queue_error(self):
        assert issubclass(q.JobNotFound, q.QueueError)

    def test_queue_error_is_runtime_error(self):
        assert issubclass(q.QueueError, RuntimeError)
