"""Domain models for the asynchronous queue subsystem.

A Job is a unit of work that flows through the queue: a story to be
processed by a coding agent. A JobResult is what the worker writes
back when the agent finishes (successfully or not). The state machine
governs the allowed transitions between JobStatus values.

These models are intentionally provider-agnostic. They do not know
whether the underlying queue is in-memory, SQLite, or Redis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class JobStatus(str, Enum):
    """Lifecycle states of a Job in the queue."""

    PENDING = "pending"
    QUEUED = "queued"
    PICKED_UP = "picked_up"
    AGENT_RUNNING = "agent_running"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    POSTPROCESSING = "postprocessing"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


# Terminal states do not allow further transitions.
TERMINAL_STATES: frozenset[JobStatus] = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD_LETTERED}
)


# Allowed transitions: source -> set of allowed destinations.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.QUEUED}),
    JobStatus.QUEUED: frozenset({JobStatus.PICKED_UP, JobStatus.DEAD_LETTERED}),
    JobStatus.PICKED_UP: frozenset(
        {
            JobStatus.AGENT_RUNNING,
            JobStatus.AGENT_FAILED,  # worker died before agent even started
            JobStatus.QUEUED,  # reclaimed if worker dies
            JobStatus.FAILED,
        }
    ),
    JobStatus.AGENT_RUNNING: frozenset(
        {
            JobStatus.AGENT_COMPLETED,
            JobStatus.AGENT_FAILED,
            JobStatus.QUEUED,  # reclaimed if worker dies
        }
    ),
    JobStatus.AGENT_COMPLETED: frozenset(
        {JobStatus.POSTPROCESSING, JobStatus.FAILED}
    ),
    JobStatus.AGENT_FAILED: frozenset(
        {JobStatus.QUEUED, JobStatus.DEAD_LETTERED, JobStatus.FAILED}
    ),
    JobStatus.POSTPROCESSING: frozenset({JobStatus.DONE, JobStatus.FAILED}),
    JobStatus.DONE: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.DEAD_LETTERED: frozenset(),
}


@dataclass
class Job:
    """A unit of work in the queue.

    The job_id is generated server-side at enqueue time. The story_key
    is the deduplication key: a coordinator must not enqueue two jobs
    for the same story_key while one is in flight.

    The not_before field implements retry backoff. A job with
    not_before > now will not be returned by dequeue() even if its
    status is QUEUED. The Worker sets this when requeueing a failed
    job so retries are spaced out.
    """

    job_id: str
    story_key: str
    repo_slug: str
    payload: dict
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    picked_up_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    worker_id: str | None = None
    not_before: datetime | None = None

    @staticmethod
    def new(
        story_key: str,
        repo_slug: str,
        payload: dict,
        max_attempts: int = 3,
    ) -> "Job":
        return Job(
            job_id=str(uuid.uuid4()),
            story_key=story_key,
            repo_slug=repo_slug,
            payload=payload,
            max_attempts=max_attempts,
        )

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def can_retry(self) -> bool:
        return self.attempts < self.max_attempts and not self.is_terminal()


@dataclass
class JobResult:
    """The output of a worker's execution of a job.

    Successful results carry the agent's stdout and the list of files
    the agent modified (so the postprocessing step can build a PR).
    Failed results carry an error message and a traceback if available.
    """

    job_id: str
    success: bool
    agent_output: str = ""
    error: str | None = None
    traceback: str | None = None
    duration_seconds: float = 0.0
    branch_name: str | None = None
    commit_sha: str | None = None
    completed_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class WorkerInfo:
    """Heartbeat record for a worker process."""

    worker_id: str
    hostname: str
    pid: int
    started_at: datetime
    last_heartbeat_at: datetime
    current_job_id: str | None = None
