"""In-memory thread-safe backend.

A reference implementation that holds all queue state in process memory.
Intended for unit tests, development, and the legacy `yokai run` mode
where there is no separate worker process.

All four interfaces (JobQueue, ResultStore, WorkerRegistry,
CoordinatorLock) are implemented on top of a single `threading.RLock`
so cross-interface invariants (e.g. enqueue + dedupe check) hold
atomically. Performance is fine for hundreds of jobs; for thousands
use the SQLite backend.

Note: in-memory state is lost on process restart. Do not use this
backend for the coordinator/worker split mode where workers run as
separate processes - they would each see their own empty queue.
"""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timedelta, timezone

from yokai.queue.exceptions import (
    DuplicateJobError,
    JobNotFound,
    LeaseExpiredError,
)
from yokai.queue.interfaces import (
    CoordinatorLock,
    JobQueue,
    ResultStore,
    WorkerRegistry,
)
from yokai.queue.models import (
    TERMINAL_STATES,
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)
from yokai.queue.state_machine import needs_recovery, transition


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryBackend(JobQueue, ResultStore, WorkerRegistry, CoordinatorLock):
    """Single class implementing all four queue interfaces in memory."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._results: dict[str, JobResult] = {}
        self._workers: dict[str, WorkerInfo] = {}
        self._lease_expiry: dict[str, datetime] = {}
        self._coord_owner: str | None = None
        self._coord_expiry: datetime | None = None

    def enqueue(self, job: Job) -> Job:
        with self._lock:
            for existing in self._jobs.values():
                if (
                    existing.story_key == job.story_key
                    and existing.status not in TERMINAL_STATES
                ):
                    raise DuplicateJobError(
                        f"Story {job.story_key} already has an in-flight "
                        f"job: {existing.job_id} (status={existing.status.value})"
                    )
            persisted = copy.deepcopy(job)
            persisted.status = transition(persisted.status, JobStatus.QUEUED)
            persisted.updated_at = _utcnow()
            self._jobs[persisted.job_id] = persisted
            return copy.deepcopy(persisted)

    def dequeue(
        self,
        worker_id: str,
        lease_duration: timedelta,
    ) -> Job | None:
        with self._lock:
            now = _utcnow()
            candidates = [
                j for j in self._jobs.values()
                if j.status == JobStatus.QUEUED
                and (j.not_before is None or j.not_before <= now)
            ]
            candidates.sort(key=lambda j: j.created_at)
            if not candidates:
                return None
            job = candidates[0]
            job.status = transition(job.status, JobStatus.PICKED_UP)
            job.worker_id = worker_id
            job.picked_up_at = _utcnow()
            job.updated_at = _utcnow()
            job.attempts += 1
            self._lease_expiry[job.job_id] = _utcnow() + lease_duration
            return copy.deepcopy(job)

    def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        worker_id: str | None = None,
        error: str | None = None,
        not_before: datetime | None = None,
    ) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            if worker_id is not None and job.worker_id != worker_id:
                raise LeaseExpiredError(
                    f"Worker {worker_id} does not hold lease on job {job_id} "
                    f"(current owner: {job.worker_id})"
                )
            job.status = transition(job.status, new_status)
            if error is not None:
                job.last_error = error[:2000]
            job.updated_at = _utcnow()
            if new_status in TERMINAL_STATES:
                job.completed_at = _utcnow()
                self._lease_expiry.pop(job_id, None)
                job.not_before = None
            elif new_status == JobStatus.QUEUED:
                # Reclaim or retry: clear worker assignment
                job.worker_id = None
                self._lease_expiry.pop(job_id, None)
                # Apply backoff if requested (only meaningful when going
                # back to QUEUED)
                job.not_before = not_before
            return copy.deepcopy(job)

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(f"Job {job_id} not found")
            return copy.deepcopy(job)

    def list_by_status(
        self,
        status: JobStatus,
        limit: int = 100,
    ) -> list[Job]:
        with self._lock:
            jobs = [j for j in self._jobs.values() if j.status == status]
            jobs.sort(key=lambda j: j.created_at)
            return [copy.deepcopy(j) for j in jobs[:limit]]

    def reclaim_expired_leases(self) -> list[Job]:
        with self._lock:
            now = _utcnow()
            reclaimed: list[Job] = []
            for job_id, expiry in list(self._lease_expiry.items()):
                if expiry > now:
                    continue
                job = self._jobs.get(job_id)
                if job is None or not needs_recovery(job.status):
                    self._lease_expiry.pop(job_id, None)
                    continue
                # Decide: retry or dead-letter
                if job.attempts >= job.max_attempts:
                    job.status = transition(
                        job.status, JobStatus.AGENT_FAILED
                    )
                    job.status = transition(
                        job.status, JobStatus.DEAD_LETTERED
                    )
                    job.completed_at = now
                    job.last_error = (
                        f"Worker {job.worker_id} lease expired and retry "
                        f"budget exhausted"
                    )
                else:
                    job.status = transition(job.status, JobStatus.QUEUED)
                    job.worker_id = None
                    job.last_error = (
                        f"Worker lease expired, returning to queue "
                        f"(attempt {job.attempts}/{job.max_attempts})"
                    )
                job.updated_at = now
                self._lease_expiry.pop(job_id, None)
                reclaimed.append(copy.deepcopy(job))
            return reclaimed

    def stats(self) -> dict[JobStatus, int]:
        with self._lock:
            counts: dict[JobStatus, int] = {s: 0 for s in JobStatus}
            for j in self._jobs.values():
                counts[j.status] += 1
            return counts

    def put(self, result: JobResult) -> None:
        with self._lock:
            self._results[result.job_id] = copy.deepcopy(result)

    def get_result(self, job_id: str) -> JobResult | None:
        # Renamed to avoid collision with JobQueue.get; we expose it
        # under both names below via a thin wrapper.
        with self._lock:
            r = self._results.get(job_id)
            return copy.deepcopy(r) if r is not None else None

    def pending_for_postprocessing(
        self, limit: int = 50
    ) -> list[JobResult]:
        with self._lock:
            out: list[JobResult] = []
            for job_id, result in self._results.items():
                if not result.success:
                    continue
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                if job.status == JobStatus.AGENT_COMPLETED:
                    out.append(copy.deepcopy(result))
                if len(out) >= limit:
                    break
            return out

    def register(self, worker: WorkerInfo) -> None:
        with self._lock:
            self._workers[worker.worker_id] = copy.deepcopy(worker)

    def heartbeat(
        self, worker_id: str, current_job_id: str | None
    ) -> None:
        with self._lock:
            w = self._workers.get(worker_id)
            if w is None:
                return
            w.last_heartbeat_at = _utcnow()
            w.current_job_id = current_job_id

    def deregister(self, worker_id: str) -> None:
        with self._lock:
            self._workers.pop(worker_id, None)

    def list_alive(self, max_age: timedelta) -> list[WorkerInfo]:
        with self._lock:
            cutoff = _utcnow() - max_age
            return [
                copy.deepcopy(w)
                for w in self._workers.values()
                if w.last_heartbeat_at >= cutoff
            ]

    def list_dead(self, max_age: timedelta) -> list[WorkerInfo]:
        with self._lock:
            cutoff = _utcnow() - max_age
            return [
                copy.deepcopy(w)
                for w in self._workers.values()
                if w.last_heartbeat_at < cutoff
            ]

    def acquire(
        self,
        owner_id: str,
        lease_duration: timedelta,
    ) -> bool:
        with self._lock:
            now = _utcnow()
            if (
                self._coord_owner is None
                or self._coord_expiry is None
                or self._coord_expiry <= now
                or self._coord_owner == owner_id
            ):
                self._coord_owner = owner_id
                self._coord_expiry = now + lease_duration
                return True
            return False

    def renew(
        self, owner_id: str, lease_duration: timedelta
    ) -> bool:
        with self._lock:
            if self._coord_owner != owner_id:
                return False
            self._coord_expiry = _utcnow() + lease_duration
            return True

    def release(self, owner_id: str) -> None:
        with self._lock:
            if self._coord_owner == owner_id:
                self._coord_owner = None
                self._coord_expiry = None

    def current_owner(self) -> str | None:
        with self._lock:
            now = _utcnow()
            if (
                self._coord_owner is not None
                and self._coord_expiry is not None
                and self._coord_expiry > now
            ):
                return self._coord_owner
            return None
