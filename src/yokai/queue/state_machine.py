"""State machine for Job lifecycle.

Centralizes the transition rules so the SQLite, Redis, and in-memory
backends do not each implement their own (subtly different) version.

Usage:

    from yokai.queue.state_machine import transition
    new_status = transition(job.status, JobStatus.QUEUED)

The transition function does not mutate the job in place; the caller
(usually the backend) writes the new status to its store.
"""

from __future__ import annotations

from yokai.queue.exceptions import InvalidStateTransition
from yokai.queue.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    JobStatus,
)


def is_allowed(current: JobStatus, target: JobStatus) -> bool:
    """Return True if the transition current -> target is allowed."""
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def transition(current: JobStatus, target: JobStatus) -> JobStatus:
    """Validate and return the target status.

    Raises InvalidStateTransition with a descriptive message if the
    transition is not allowed by ALLOWED_TRANSITIONS.
    """
    if current == target:
        return current
    if not is_allowed(current, target):
        allowed = sorted(s.value for s in ALLOWED_TRANSITIONS.get(current, []))
        raise InvalidStateTransition(
            f"Cannot transition from {current.value} to {target.value}. "
            f"Allowed from {current.value}: {allowed or '(none, terminal)'}"
        )
    return target


def is_terminal(status: JobStatus) -> bool:
    return status in TERMINAL_STATES


def can_be_picked_up(status: JobStatus) -> bool:
    """Whether a job in this status can be claimed by a worker."""
    return status == JobStatus.QUEUED


def needs_recovery(status: JobStatus) -> bool:
    """Whether this status indicates a job a worker was processing
    when it crashed (so the queue should reclaim the job)."""
    return status in (JobStatus.PICKED_UP, JobStatus.AGENT_RUNNING)
