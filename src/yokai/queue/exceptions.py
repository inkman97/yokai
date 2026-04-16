"""Exception hierarchy for the queue subsystem.

Distinct exception types let the coordinator and worker take different
recovery actions (retry vs dead-letter vs hard fail).
"""


class QueueError(RuntimeError):
    """Base class for all queue subsystem errors."""


class InvalidStateTransition(QueueError):
    """Raised when an attempted state transition is not allowed by the
    state machine."""


class JobNotFound(QueueError):
    """Raised when a job_id is referenced but does not exist in the
    backend."""


class DuplicateJobError(QueueError):
    """Raised when a job for a story_key is enqueued while another for
    the same story_key is still in flight."""


class QueueBackendError(QueueError):
    """Raised when the underlying backend (SQLite, Redis, ...) reports
    an error during a queue operation."""


class WorkerNotFound(QueueError):
    """Raised when a worker_id is referenced but no heartbeat record
    exists in the backend."""


class LeaseExpiredError(QueueError):
    """Raised when a worker tries to update a job whose visibility
    lease has expired and the job has been reclaimed by the queue."""
