"""End-to-end integration tests: Coordinator + Worker working together.

These tests exercise the full pipeline: stories appear in the issue
source, the coordinator polls and enqueues, workers dequeue and
process, results land in the result store.

Both backends (memory and sqlite) are exercised.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from yokai.queue import (
    AgentExecution,
    AgentRunner,
    CheckoutInfo,
    ComponentMapRouter,
    Coordinator,
    CoordinatorSettings,
    Job,
    JobStatus,
    RepoCheckout,
    StorySnapshot,
    Worker,
    WorkerSettings,
)
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.backends.sqlite import SqliteBackend
from yokai.queue.sources import IssueSource


class FakeIssueSource(IssueSource):
    def __init__(self):
        self._pending: list[StorySnapshot] = []
        self.accepted: set[str] = set()
        self.rejected: dict[str, str] = {}

    def add_story(self, key: str, components=None) -> None:
        self._pending.append(
            StorySnapshot(
                key=key,
                title=f"Story {key}",
                description="desc",
                components=components or ["EMU-BE"],
            )
        )

    def fetch_pending(self):
        return [s for s in self._pending if s.key not in self.accepted]

    def mark_accepted(self, story_key: str) -> None:
        self.accepted.add(story_key)

    def mark_rejected(self, story_key: str, reason: str) -> None:
        self.rejected[story_key] = reason


class FakeAgent(AgentRunner):
    def __init__(self, success: bool = True, output: str = "ok"):
        self.success = success
        self.output = output
        self.invocations: list[str] = []

    def run(self, job, repo_path, timeout_seconds):
        self.invocations.append(job.story_key)
        return AgentExecution(success=self.success, output=self.output)


class FakeCheckout(RepoCheckout):
    def __init__(self, tmp: Path):
        self._tmp = tmp

    def prepare(self, job):
        p = self._tmp / job.repo_slug / job.story_key
        p.mkdir(parents=True, exist_ok=True)
        return CheckoutInfo(
            repo_path=p,
            branch_name=f"feature/{job.story_key}-ai",
            base_branch="master",
        )

    def cleanup(self, job):
        pass


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, tmp_path):
    if request.param == "memory":
        return InMemoryBackend()
    return SqliteBackend(tmp_path / "queue.db")


@pytest.fixture
def source():
    return FakeIssueSource()


@pytest.fixture
def router():
    return ComponentMapRouter({"EMU-BE": "TEST-be", "EMU-FE": "TEST-fe"})


@pytest.fixture
def agent():
    return FakeAgent()


@pytest.fixture
def checkout(tmp_path):
    return FakeCheckout(tmp_path / "workspace")


@pytest.fixture
def coordinator(backend, source, router):
    return Coordinator(
        source=source,
        router=router,
        queue=backend,
        lock=backend,
        settings=CoordinatorSettings(
            poll_interval_seconds=0.01,
            reclaim_interval_seconds=999,
        ),
    )


@pytest.fixture
def worker(backend, agent, checkout):
    return Worker(
        queue=backend,
        results=backend,
        registry=backend,
        agent=agent,
        checkout=checkout,
        settings=WorkerSettings(
            poll_interval_seconds=0.01,
            agent_timeout_seconds=10,
        ),
    )


class TestEndToEnd:
    def test_single_story_full_lifecycle(
        self, coordinator, worker, source, agent, backend
    ):
        # Coordinator polls and enqueues
        source.add_story("S-1", components=["EMU-BE"])
        coordinator.run_once()
        assert backend.stats()[JobStatus.QUEUED] == 1

        # Worker picks up and processes
        assert worker.process_one_job() is True

        # Verify final state
        jobs = backend.list_by_status(JobStatus.AGENT_COMPLETED)
        assert len(jobs) == 1
        assert jobs[0].story_key == "S-1"

        result = backend.get_result(jobs[0].job_id)
        assert result is not None
        assert result.success is True
        assert result.branch_name == "feature/S-1-ai"

        # Agent was called exactly once
        assert agent.invocations == ["S-1"]

        # Source was marked
        assert "S-1" in source.accepted

    def test_multiple_stories_processed_correctly(
        self, coordinator, worker, source, backend
    ):
        for i in range(5):
            source.add_story(f"S-{i}", components=["EMU-BE"])

        coordinator.run_once()
        assert backend.stats()[JobStatus.QUEUED] == 5

        # Drain
        for _ in range(5):
            assert worker.process_one_job() is True

        assert worker.process_one_job() is False
        assert backend.stats()[JobStatus.AGENT_COMPLETED] == 5
        assert backend.stats()[JobStatus.QUEUED] == 0

    def test_unroutable_story_does_not_reach_worker(
        self, coordinator, worker, source, agent, backend
    ):
        source.add_story("S-OK", components=["EMU-BE"])
        source.add_story("S-BAD", components=["UNKNOWN"])

        coordinator.run_once()
        worker.process_one_job()

        # Only S-OK reached the agent
        assert agent.invocations == ["S-OK"]
        assert "S-BAD" in source.rejected
        assert "S-OK" not in source.rejected

    def test_failed_job_retries_via_coordinator_reclaim(
        self, source, router, backend, checkout
    ):
        # Set up a worker whose agent always fails
        failing_agent = FakeAgent(success=False)
        worker = Worker(
            queue=backend,
            results=backend,
            registry=backend,
            agent=failing_agent,
            checkout=checkout,
            settings=WorkerSettings(
                poll_interval_seconds=0.01,
                lease_duration_seconds=60,
                retry_backoff_base_seconds=0,
            ),
        )

        coordinator = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
        )

        source.add_story("S-1", components=["EMU-BE"])
        coordinator.run_once()

        # Process up to max_attempts (default 3)
        worker.process_one_job()
        worker.process_one_job()
        worker.process_one_job()

        # Should be dead-lettered now
        dead = backend.list_by_status(JobStatus.DEAD_LETTERED)
        assert len(dead) == 1
        assert dead[0].story_key == "S-1"
        assert failing_agent.invocations == ["S-1", "S-1", "S-1"]


class TestCoordinatorWorkerSeparateBackends:
    """Verify that coordinator and worker can use distinct backend
    instances (simulating separate processes) on the same SQLite file."""

    def test_two_processes_one_file(self, tmp_path, source, router, agent, checkout):
        db = tmp_path / "queue.db"

        coord_backend = SqliteBackend(db)
        worker_backend = SqliteBackend(db)

        coordinator = Coordinator(
            source=source,
            router=router,
            queue=coord_backend,
            lock=coord_backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
        )
        worker = Worker(
            queue=worker_backend,
            results=worker_backend,
            registry=worker_backend,
            agent=agent,
            checkout=checkout,
            settings=WorkerSettings(poll_interval_seconds=0.01),
        )

        source.add_story("S-1", components=["EMU-BE"])
        coordinator.run_once()
        worker.process_one_job()

        # Coordinator can see the result
        completed = coord_backend.list_by_status(JobStatus.AGENT_COMPLETED)
        assert len(completed) == 1
        assert completed[0].story_key == "S-1"
