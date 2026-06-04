"""Tests for the Coordinator using a FakeIssueSource."""

from datetime import timedelta

import pytest

from yokai.queue import (
    ComponentMapRouter,
    Coordinator,
    CoordinatorSettings,
    Job,
    JobStatus,
    StorySnapshot,
)
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.sources import IssueSource


class FakeIssueSource(IssueSource):
    """In-memory IssueSource for tests.

    Behaviour:
    - fetch_pending() returns whatever stories were added via add_story(),
      excluding any that have been mark_accepted'd.
    - mark_accepted moves a story to the 'accepted' set.
    - mark_rejected records the rejection.
    - All methods can be configured to raise via fail_next_*.
    """

    def __init__(self):
        self._pending: list[StorySnapshot] = []
        self.accepted: set[str] = set()
        self.rejected: dict[str, str] = {}
        self.fail_next_fetch: Exception | None = None
        self.fail_next_accept: Exception | None = None
        self.fetch_call_count = 0
        self.accept_call_count = 0

    def add_story(self, story: StorySnapshot) -> None:
        self._pending.append(story)

    def fetch_pending(self) -> list[StorySnapshot]:
        self.fetch_call_count += 1
        if self.fail_next_fetch is not None:
            err = self.fail_next_fetch
            self.fail_next_fetch = None
            raise err
        return [s for s in self._pending if s.key not in self.accepted]

    def mark_accepted(self, story_key: str) -> None:
        self.accept_call_count += 1
        if self.fail_next_accept is not None:
            err = self.fail_next_accept
            self.fail_next_accept = None
            raise err
        self.accepted.add(story_key)

    def mark_rejected(self, story_key: str, reason: str) -> None:
        self.rejected[story_key] = reason


def make_story(
    key: str,
    components: list[str] | None = None,
    labels: list[str] | None = None,
) -> StorySnapshot:
    return StorySnapshot(
        key=key,
        title=f"Story {key}",
        description="desc",
        components=components or [],
        labels=labels or [],
    )


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def source():
    return FakeIssueSource()


@pytest.fixture
def router():
    return ComponentMapRouter({"EMU-BE": "TEST-be", "EMU-FE": "TEST-fe"})


@pytest.fixture
def coordinator(backend, source, router):
    return Coordinator(
        source=source,
        router=router,
        queue=backend,
        lock=backend,
        settings=CoordinatorSettings(
            poll_interval_seconds=0.01,
            lease_duration_seconds=60,
            reclaim_interval_seconds=999,  # disable in tests
        ),
    )


class TestRunOnceHappyPath:
    def test_enqueues_routable_story(
        self, coordinator, source, backend
    ):
        source.add_story(make_story("S-1", components=["EMU-BE"]))
        stats = coordinator.run_once()
        assert stats.fetched == 1
        assert stats.enqueued == 1
        assert stats.unroutable == 0
        # Job is in the queue
        queued = backend.list_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        assert queued[0].story_key == "S-1"
        assert queued[0].repo_slug == "TEST-be"

    def test_marks_story_accepted_after_enqueue(self, coordinator, source):
        source.add_story(make_story("S-1", components=["EMU-BE"]))
        coordinator.run_once()
        assert "S-1" in source.accepted

    def test_passes_story_payload_to_job(self, coordinator, source, backend):
        story = StorySnapshot(
            key="S-1",
            title="Fix bug",
            description="Detailed description",
            components=["EMU-BE"],
            labels=["ai-pipeline"],
        )
        source.add_story(story)
        coordinator.run_once()
        job = backend.list_by_status(JobStatus.QUEUED)[0]
        assert job.payload["title"] == "Fix bug"
        assert job.payload["description"] == "Detailed description"
        assert job.payload["components"] == ["EMU-BE"]
        assert job.payload["labels"] == ["ai-pipeline"]

    def test_enqueues_multiple_stories_in_one_cycle(
        self, coordinator, source, backend
    ):
        source.add_story(make_story("S-1", components=["EMU-BE"]))
        source.add_story(make_story("S-2", components=["EMU-FE"]))
        source.add_story(make_story("S-3", components=["EMU-BE"]))
        stats = coordinator.run_once()
        assert stats.enqueued == 3
        assert backend.stats()[JobStatus.QUEUED] == 3


class TestRunOnceUnroutable:
    def test_unroutable_story_is_not_enqueued(
        self, coordinator, source, backend
    ):
        source.add_story(make_story("S-1", components=["UNKNOWN-COMP"]))
        stats = coordinator.run_once()
        assert stats.fetched == 1
        assert stats.enqueued == 0
        assert stats.unroutable == 1
        assert backend.stats()[JobStatus.QUEUED] == 0

    def test_unroutable_story_is_marked_rejected(self, coordinator, source):
        source.add_story(make_story("S-1", components=["UNKNOWN"]))
        coordinator.run_once()
        assert "S-1" in source.rejected

    def test_routable_and_unroutable_in_same_cycle(
        self, coordinator, source, backend
    ):
        source.add_story(make_story("S-OK", components=["EMU-BE"]))
        source.add_story(make_story("S-BAD", components=["UNKNOWN"]))
        stats = coordinator.run_once()
        assert stats.enqueued == 1
        assert stats.unroutable == 1
        queued = backend.list_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        assert queued[0].story_key == "S-OK"


class TestRunOnceDuplicates:
    def test_re_enqueue_same_story_in_flight_is_duplicate(
        self, coordinator, source, backend
    ):
        # First cycle enqueues
        source.add_story(make_story("S-1", components=["EMU-BE"]))
        first = coordinator.run_once()
        assert first.enqueued == 1

        # Reset accepted so the source returns the story again
        source.accepted.clear()
        second = coordinator.run_once()
        assert second.fetched == 1
        assert second.enqueued == 0
        assert second.duplicates == 1


class TestErrorHandling:
    def test_fetch_failure_does_not_crash_loop(self, coordinator, source):
        source.fail_next_fetch = RuntimeError("Jira down")
        stats = coordinator.run_once()
        assert stats.fetched == 0
        assert stats.errors == 1

    def test_accept_failure_does_not_un_enqueue_job(
        self, coordinator, source, backend
    ):
        source.add_story(make_story("S-1", components=["EMU-BE"]))
        source.fail_next_accept = RuntimeError("Jira PUT failed")
        stats = coordinator.run_once()
        # Job is still enqueued, only the accept marker failed
        assert stats.enqueued == 1
        assert backend.stats()[JobStatus.QUEUED] == 1
        assert "S-1" not in source.accepted


class TestReclaim:
    def test_reclaim_runs_when_interval_elapsed(
        self, source, router, backend
    ):
        # Set interval to 0 so it always runs
        coord = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=0),
        )
        # Add an expired-lease job
        backend.enqueue(Job.new("S-1", "r", {}))
        backend.dequeue("dead-worker", timedelta(seconds=-1))
        stats = coord.run_once()
        assert stats.reclaimed == 1


class TestLeaderElection:
    def test_only_first_coordinator_acquires(
        self, source, router, backend
    ):
        c1 = Coordinator(
            source=source, router=router, queue=backend, lock=backend
        )
        c2 = Coordinator(
            source=source, router=router, queue=backend, lock=backend
        )
        assert c1._acquire_or_renew_lock() is True
        assert c2._acquire_or_renew_lock() is False

    def test_second_coordinator_acquires_after_first_releases(
        self, source, router, backend
    ):
        c1 = Coordinator(
            source=source, router=router, queue=backend, lock=backend
        )
        c2 = Coordinator(
            source=source, router=router, queue=backend, lock=backend
        )
        c1._acquire_or_renew_lock()
        backend.release(c1.coordinator_id)
        assert c2._acquire_or_renew_lock() is True

    def test_leader_can_renew_indefinitely(
        self, source, router, backend
    ):
        c1 = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(lease_duration_seconds=60),
        )
        for _ in range(5):
            assert c1._acquire_or_renew_lock() is True


class TestRunLoop:
    def test_run_exits_on_stop(self, coordinator, source):
        # Run in a thread, signal stop after a short delay
        import threading

        def stopper():
            import time
            time.sleep(0.05)
            coordinator.stop()

        threading.Thread(target=stopper, daemon=True).start()
        # Should return without raising
        coordinator.run()

    def test_run_does_at_least_one_cycle(self, coordinator, source):
        import threading

        source.add_story(make_story("S-1", components=["EMU-BE"]))

        def stopper():
            import time
            time.sleep(0.1)
            coordinator.stop()

        threading.Thread(target=stopper, daemon=True).start()
        coordinator.run()
        assert "S-1" in source.accepted
