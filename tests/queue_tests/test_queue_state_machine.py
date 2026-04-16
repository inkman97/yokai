"""Unit tests for yokai.queue.state_machine."""

import pytest

from yokai.queue.exceptions import InvalidStateTransition
from yokai.queue.models import JobStatus, TERMINAL_STATES
from yokai.queue.state_machine import (
    can_be_picked_up,
    is_allowed,
    is_terminal,
    needs_recovery,
    transition,
)


class TestIsAllowed:
    def test_pending_to_queued_is_allowed(self):
        assert is_allowed(JobStatus.PENDING, JobStatus.QUEUED)

    def test_queued_to_picked_up_is_allowed(self):
        assert is_allowed(JobStatus.QUEUED, JobStatus.PICKED_UP)

    def test_pending_to_done_is_not_allowed(self):
        assert not is_allowed(JobStatus.PENDING, JobStatus.DONE)

    def test_done_to_anything_is_not_allowed(self):
        for target in JobStatus:
            assert not is_allowed(JobStatus.DONE, target)

    def test_failed_to_anything_is_not_allowed(self):
        for target in JobStatus:
            assert not is_allowed(JobStatus.FAILED, target)

    def test_agent_running_can_go_back_to_queued_for_reclaim(self):
        # Important for worker-died recovery
        assert is_allowed(JobStatus.AGENT_RUNNING, JobStatus.QUEUED)


class TestTransition:
    def test_returns_target_when_allowed(self):
        assert transition(JobStatus.PENDING, JobStatus.QUEUED) == JobStatus.QUEUED

    def test_idempotent_self_transition(self):
        # Allow re-asserting same status without error (useful for
        # backends that re-set status on every heartbeat)
        assert (
            transition(JobStatus.AGENT_RUNNING, JobStatus.AGENT_RUNNING)
            == JobStatus.AGENT_RUNNING
        )

    def test_raises_on_disallowed(self):
        with pytest.raises(InvalidStateTransition, match="Cannot transition"):
            transition(JobStatus.PENDING, JobStatus.DONE)

    def test_error_message_lists_allowed_destinations(self):
        with pytest.raises(InvalidStateTransition) as exc:
            transition(JobStatus.PENDING, JobStatus.AGENT_RUNNING)
        # PENDING can only go to QUEUED
        assert "queued" in str(exc.value)

    def test_error_message_for_terminal_state(self):
        with pytest.raises(InvalidStateTransition) as exc:
            transition(JobStatus.DONE, JobStatus.QUEUED)
        assert "(none, terminal)" in str(exc.value)


class TestIsTerminal:
    def test_done_is_terminal(self):
        assert is_terminal(JobStatus.DONE)

    def test_failed_is_terminal(self):
        assert is_terminal(JobStatus.FAILED)

    def test_dead_lettered_is_terminal(self):
        assert is_terminal(JobStatus.DEAD_LETTERED)

    def test_pending_is_not_terminal(self):
        assert not is_terminal(JobStatus.PENDING)

    def test_running_is_not_terminal(self):
        assert not is_terminal(JobStatus.AGENT_RUNNING)


class TestCanBePickedUp:
    def test_only_queued_can_be_picked_up(self):
        for status in JobStatus:
            if status == JobStatus.QUEUED:
                assert can_be_picked_up(status)
            else:
                assert not can_be_picked_up(status)


class TestNeedsRecovery:
    def test_picked_up_needs_recovery(self):
        assert needs_recovery(JobStatus.PICKED_UP)

    def test_agent_running_needs_recovery(self):
        assert needs_recovery(JobStatus.AGENT_RUNNING)

    def test_pending_does_not_need_recovery(self):
        assert not needs_recovery(JobStatus.PENDING)

    def test_terminal_states_never_need_recovery(self):
        for status in TERMINAL_STATES:
            assert not needs_recovery(status)


class TestStateMachineCompleteness:
    """Defensive tests that catch mistakes when extending the state machine."""

    def test_full_happy_path(self):
        """PENDING -> QUEUED -> PICKED_UP -> AGENT_RUNNING ->
        AGENT_COMPLETED -> POSTPROCESSING -> DONE"""
        path = [
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.PICKED_UP,
            JobStatus.AGENT_RUNNING,
            JobStatus.AGENT_COMPLETED,
            JobStatus.POSTPROCESSING,
            JobStatus.DONE,
        ]
        for src, dst in zip(path, path[1:]):
            assert transition(src, dst) == dst

    def test_retry_path(self):
        """AGENT_RUNNING -> AGENT_FAILED -> QUEUED is the retry loop."""
        assert transition(JobStatus.AGENT_RUNNING, JobStatus.AGENT_FAILED)
        assert transition(JobStatus.AGENT_FAILED, JobStatus.QUEUED)

    def test_dead_letter_path(self):
        """AGENT_FAILED -> DEAD_LETTERED when retry budget exhausted."""
        assert transition(JobStatus.AGENT_FAILED, JobStatus.DEAD_LETTERED)

    def test_reclaim_path(self):
        """AGENT_RUNNING -> QUEUED when worker presumed dead."""
        assert transition(JobStatus.AGENT_RUNNING, JobStatus.QUEUED)
