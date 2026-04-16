"""Abstract interfaces for queue backends.

Three orthogonal concerns:

- JobQueue: enqueue, dequeue (with visibility lease), update status,
  reclaim stuck jobs, list jobs by status.
- ResultStore: store and retrieve JobResult objects keyed by job_id.
- WorkerRegistry: track which workers are alive via heartbeats.

A single backend (e.g. SqliteBackend) typically implements all three
on top of the same storage. They are kept as separate ABCs so a future
backend could mix and match (e.g. Redis for queue, S3 for results).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Iterator

from yokai.queue.models import Job, JobResult, JobStatus, WorkerInfo


class JobQueue(ABC):
    """Abstract job queue with visibility-lease semantics."""

    @abstractmethod
    def enqueue(self, job: Job) -> Job:
        """Add a new job. Returns the persisted job (with status QUEUED).

        Raises DuplicateJobError if another job with the same story_key
        is currently in flight (status not in TERMINAL_STATES).
        """

    @abstractmethod
    def dequeue(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> Job | None:
        """Atomically claim the next available job and assign it to a
        worker for `lease_duration`. Returns None if no job is available.

        The returned job has status PICKED_UP and worker_id set.
        """

    @abstractmethod
    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        worker_id: str | None = None,
        error: str | None = None,
        not_before: datetime | None = None,
    ) -> Job:
        """Transition a job to new_status (validated via state machine).

        If worker_id is provided, the call is rejected unless that
        worker still holds the lease on this job (raises LeaseExpiredError).
        Raises InvalidStateTransition if the transition is not allowed.
        Raises JobNotFound if job_id does not exist.

        If not_before is provided AND new_status is QUEUED, the job
        will not be returned by dequeue() until that time. Used to
        implement retry backoff.
        """

    @abstractmethod
    def get(self, job_id: str) -> Job:
        """Retrieve a job by id. Raises JobNotFound if not present."""

    @abstractmethod
    def list_by_status(
        self,
        status: JobStatus,
        limit: int = 100,
    ) -> list[Job]:
        """Return jobs currently in the given status, oldest first."""

    @abstractmethod
    def reclaim_expired_leases(self) -> list[Job]:
        """Find jobs whose lease has expired (worker presumed dead) and
        return them to the QUEUED state. Returns the list of reclaimed
        jobs.

        This is typically called periodically by the coordinator.
        """

    @abstractmethod
    def stats(self) -> dict[JobStatus, int]:
        """Return a count of jobs per status. Useful for `yokai status`."""


class ResultStore(ABC):
    """Abstract storage for JobResult objects."""

    @abstractmethod
    def put(self, result: JobResult) -> None:
        """Store a result. Overwrites any previous result for the same
        job_id (which should not normally happen)."""

    @abstractmethod
    def get_result(self, job_id: str) -> JobResult | None:
        """Retrieve a result by job_id, or None if not present.

        Named `get_result` (not `get`) so a single backend can implement
        both JobQueue and ResultStore without method-name collision."""

    @abstractmethod
    def pending_for_postprocessing(self, limit: int = 50) -> list[JobResult]:
        """Return successful results whose corresponding job is still
        in AGENT_COMPLETED status (i.e. not yet postprocessed into a PR).
        """


class WorkerRegistry(ABC):
    """Abstract registry tracking live workers via heartbeats."""

    @abstractmethod
    def register(self, worker: WorkerInfo) -> None:
        """Record a new worker. Idempotent on worker_id."""

    @abstractmethod
    def heartbeat(self, worker_id: str, current_job_id: str | None) -> None:
        """Update the last_heartbeat_at timestamp for a worker."""

    @abstractmethod
    def deregister(self, worker_id: str) -> None:
        """Remove a worker (graceful shutdown)."""

    @abstractmethod
    def list_alive(self, max_age: timedelta) -> list[WorkerInfo]:
        """Return workers whose last heartbeat is within max_age."""

    @abstractmethod
    def list_dead(self, max_age: timedelta) -> list[WorkerInfo]:
        """Return workers whose last heartbeat is older than max_age."""


class CoordinatorLock(ABC):
    """Abstract leader-election lock to ensure at most one coordinator
    polls Jira at a time, even if the user accidentally launches more
    than one coordinator process."""

    @abstractmethod
    def acquire(
        self,
        owner_id: str,
        lease_duration: timedelta,
    ) -> bool:
        """Try to acquire the coordinator lock for owner_id. Returns
        True if acquired or already owned by owner_id, False if held
        by someone else."""

    @abstractmethod
    def renew(self, owner_id: str, lease_duration: timedelta) -> bool:
        """Extend the lease. Returns False if owner_id is not the
        current holder."""

    @abstractmethod
    def release(self, owner_id: str) -> None:
        """Release the lock if owner_id holds it. No-op otherwise."""

    @abstractmethod
    def current_owner(self) -> str | None:
        """Return the current owner_id, or None if no one holds the lock."""
