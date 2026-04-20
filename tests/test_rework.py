"""Tests for the rework feature across all layers.

Covers: coordinator rework polling, tracker source rework bridge,
hosting checkout rework path, agent runner rework prompt, postprocessor
rework flow, and rework resolver PR lookup.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yokai.core.interfaces import (
    CodingAgent,
    IssueTracker,
    RepoHosting,
)
from yokai.core.models import (
    AgentResult,
    FileChange,
    PRComment,
    PullRequest,
    RepoLocation,
    Story,
)
from yokai.queue import (
    ComponentMapRouter,
    Coordinator,
    CoordinatorSettings,
    Job,
    JobStatus,
    StorySnapshot,
)
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.coordinator import ReworkResolver
from yokai.queue.models import JobResult
from yokai.queue.sources import IssueSource
from yokai.queue_adapters import (
    AgentCodingRunner,
    HostingRepoCheckout,
    HostingTrackerPostprocessor,
    TrackerIssueSource,
)


# ----------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------

class FakeReworkSource(IssueSource):
    def __init__(self):
        self._pending: list[StorySnapshot] = []
        self._rework: list[StorySnapshot] = []
        self.accepted: set[str] = set()
        self.rework_accepted: set[str] = set()

    def add_pending(self, story: StorySnapshot) -> None:
        self._pending.append(story)

    def add_rework(self, story: StorySnapshot) -> None:
        self._rework.append(story)

    def fetch_pending(self) -> list[StorySnapshot]:
        return [s for s in self._pending if s.key not in self.accepted]

    def fetch_rework(self) -> list[StorySnapshot]:
        return [s for s in self._rework if s.key not in self.rework_accepted]

    def mark_accepted(self, story_key: str) -> None:
        self.accepted.add(story_key)

    def mark_rework_accepted(self, story_key: str) -> None:
        self.rework_accepted.add(story_key)

    def mark_rejected(self, story_key: str, reason: str) -> None:
        pass


class FakeReworkResolver(ReworkResolver):
    def __init__(self, pr_info: dict | None = None):
        self._pr_info = pr_info
        self.resolve_calls: list[tuple[str, str]] = []

    def resolve(self, story_key: str, repo_slug: str) -> dict | None:
        self.resolve_calls.append((story_key, repo_slug))
        return self._pr_info


def make_story(key: str, components: list[str] | None = None) -> StorySnapshot:
    return StorySnapshot(
        key=key,
        title=f"Story {key}",
        description="desc",
        components=components or ["EMU-BE"],
        labels=[],
    )


SAMPLE_PR_INFO = {
    "branch_name": "feature/S-1-ai-20260420",
    "pr_id": "42",
    "pr_url": "https://bb.example/pr/42",
    "pr_comments": [
        {
            "id": "100",
            "author": "Francesco",
            "text": "Move validation to service",
            "file_path": "src/Controller.java",
            "line": 72,
            "severity": "NORMAL",
            "state": "OPEN",
            "created_at": "2026-04-20",
        }
    ],
}


# ----------------------------------------------------------------
# Coordinator rework tests
# ----------------------------------------------------------------

class TestCoordinatorRework:
    @pytest.fixture
    def backend(self):
        return InMemoryBackend()

    @pytest.fixture
    def source(self):
        return FakeReworkSource()

    @pytest.fixture
    def router(self):
        return ComponentMapRouter({"EMU-BE": "nova-be"})

    def test_enqueues_rework_story_with_pr_info(self, backend, source, router):
        resolver = FakeReworkResolver(SAMPLE_PR_INFO)
        coord = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
            rework_resolver=resolver,
        )
        source.add_rework(make_story("S-1"))
        stats = coord.run_once()

        assert stats.enqueued == 1
        queued = backend.list_by_status(JobStatus.QUEUED)
        assert len(queued) == 1
        assert queued[0].payload["job_type"] == "rework"
        assert queued[0].payload["branch_name"] == "feature/S-1-ai-20260420"
        assert queued[0].payload["pr_id"] == "42"
        assert len(queued[0].payload["pr_comments"]) == 1

    def test_marks_rework_accepted_after_enqueue(self, backend, source, router):
        resolver = FakeReworkResolver(SAMPLE_PR_INFO)
        coord = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
            rework_resolver=resolver,
        )
        source.add_rework(make_story("S-1"))
        coord.run_once()

        assert "S-1" in source.rework_accepted

    def test_skips_rework_when_no_pr_found(self, backend, source, router):
        resolver = FakeReworkResolver(None)
        coord = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
            rework_resolver=resolver,
        )
        source.add_rework(make_story("S-1"))
        stats = coord.run_once()

        assert stats.enqueued == 0
        assert stats.errors == 1

    def test_rework_and_pending_in_same_cycle(self, backend, source, router):
        resolver = FakeReworkResolver(SAMPLE_PR_INFO)
        coord = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
            rework_resolver=resolver,
        )
        source.add_pending(make_story("S-1"))
        source.add_rework(make_story("S-2"))
        stats = coord.run_once()

        assert stats.fetched == 2
        assert stats.enqueued == 2

    def test_resolver_exception_does_not_crash(self, backend, source, router):
        resolver = FakeReworkResolver(None)
        resolver.resolve = MagicMock(side_effect=RuntimeError("API down"))
        coord = Coordinator(
            source=source,
            router=router,
            queue=backend,
            lock=backend,
            settings=CoordinatorSettings(reclaim_interval_seconds=999),
            rework_resolver=resolver,
        )
        source.add_rework(make_story("S-1"))
        stats = coord.run_once()

        assert stats.enqueued == 0
        assert stats.errors == 1


# ----------------------------------------------------------------
# TrackerIssueSource rework tests
# ----------------------------------------------------------------

class TestTrackerIssueSourceRework:
    def test_fetch_rework_delegates_to_tracker(self):
        tracker = MagicMock(spec=IssueTracker)
        tracker.search_rework_stories.return_value = [
            Story(key="K-1", title="t", description="d"),
        ]
        source = TrackerIssueSource(tracker)
        snaps = source.fetch_rework()

        assert len(snaps) == 1
        assert snaps[0].key == "K-1"
        tracker.search_rework_stories.assert_called_once()

    def test_mark_rework_accepted_delegates_to_tracker(self):
        tracker = MagicMock(spec=IssueTracker)
        TrackerIssueSource(tracker).mark_rework_accepted("K-1")
        tracker.mark_rework_in_progress.assert_called_once_with("K-1")


# ----------------------------------------------------------------
# HostingRepoCheckout rework tests
# ----------------------------------------------------------------

class TestHostingRepoCheckoutRework:
    def test_rework_checks_out_existing_branch(self, tmp_path: Path):
        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        repo_path = tmp_path / "ws" / "r"
        hosting.clone_or_update.return_value = repo_path

        checkout = HostingRepoCheckout(
            hosting=hosting,
            workspace_dir=tmp_path / "ws",
        )
        job = Job.new("K-1", "r", {
            "title": "T",
            "job_type": "rework",
            "branch_name": "feature/K-1-ai-existing",
        })

        info = checkout.prepare(job)

        hosting.checkout_existing_branch.assert_called_once_with(
            repo_path, "feature/K-1-ai-existing"
        )
        hosting.create_branch.assert_not_called()
        assert info.branch_name == "feature/K-1-ai-existing"
        assert info.base_branch == "master"

    def test_rework_raises_when_no_branch_name(self, tmp_path: Path):
        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        hosting.clone_or_update.return_value = tmp_path / "ws" / "r"

        checkout = HostingRepoCheckout(
            hosting=hosting,
            workspace_dir=tmp_path / "ws",
        )
        job = Job.new("K-1", "r", {
            "title": "T",
            "job_type": "rework",
            "branch_name": "",
        })

        with pytest.raises(RuntimeError, match="no branch_name"):
            checkout.prepare(job)

    def test_normal_job_creates_new_branch(self, tmp_path: Path):
        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        hosting.clone_or_update.return_value = tmp_path / "ws" / "r"

        checkout = HostingRepoCheckout(
            hosting=hosting,
            workspace_dir=tmp_path / "ws",
        )
        job = Job.new("K-1", "r", {"title": "T", "job_type": "new"})

        checkout.prepare(job)

        hosting.create_branch.assert_called_once()
        hosting.checkout_existing_branch.assert_not_called()


# ----------------------------------------------------------------
# AgentCodingRunner rework tests
# ----------------------------------------------------------------

class TestAgentCodingRunnerRework:
    def test_rework_uses_rework_prompt(self, tmp_path: Path):
        agent = MagicMock(spec=CodingAgent)
        agent.run.return_value = AgentResult(
            success=True, output="fixed", duration_seconds=5.0
        )
        runner = AgentCodingRunner(agent)
        job = Job.new("K-1", "r", {
            "title": "T",
            "description": "D",
            "job_type": "rework",
            "pr_comments": [
                {
                    "id": "1",
                    "author": "Francesco",
                    "text": "Fix this",
                    "file_path": "src/Main.java",
                    "line": 42,
                    "severity": "NORMAL",
                    "state": "OPEN",
                    "created_at": "",
                }
            ],
        })

        execution = runner.run(job, tmp_path, timeout_seconds=30)

        assert execution.success is True
        passed_prompt = agent.run.call_args[0][1]
        assert "Review comments to address" in passed_prompt
        assert "Francesco" in passed_prompt
        assert "Fix this" in passed_prompt
        assert "src/Main.java" in passed_prompt
        assert "line 42" in passed_prompt

    def test_rework_without_comments_uses_fallback_text(self, tmp_path: Path):
        agent = MagicMock(spec=CodingAgent)
        agent.run.return_value = AgentResult(
            success=True, output="done", duration_seconds=1.0
        )
        runner = AgentCodingRunner(agent)
        job = Job.new("K-1", "r", {
            "title": "T",
            "description": "D",
            "job_type": "rework",
            "pr_comments": [],
        })

        execution = runner.run(job, tmp_path, timeout_seconds=30)

        assert execution.success is True
        passed_prompt = agent.run.call_args[0][1]
        assert "No review comments found" in passed_prompt

    def test_normal_job_uses_default_prompt(self, tmp_path: Path):
        agent = MagicMock(spec=CodingAgent)
        agent.run.return_value = AgentResult(
            success=True, output="ok", duration_seconds=1.0
        )
        runner = AgentCodingRunner(agent)
        job = Job.new("K-1", "r", {
            "title": "T",
            "description": "D",
            "job_type": "new",
        })

        runner.run(job, tmp_path, timeout_seconds=30)

        passed_prompt = agent.run.call_args[0][1]
        assert "Review comments to address" not in passed_prompt
        assert "Operating instructions" in passed_prompt


# ----------------------------------------------------------------
# Postprocessor rework tests
# ----------------------------------------------------------------

class TestPostprocessorRework:
    def _make(self, tmp_path):
        hosting = MagicMock(spec=RepoHosting)
        tracker = MagicMock(spec=IssueTracker)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        return hosting, tracker

    def test_rework_skips_pr_creation(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        job = Job.new("K-1", "r", {
            "title": "T",
            "job_type": "rework",
            "pr_url": "https://bb.example/pr/42",
        })
        result = JobResult(
            job_id=job.job_id,
            success=True,
            agent_output="fixed review comments",
            duration_seconds=5.0,
            branch_name="feature/K-1-ai",
        )

        outcome = post.run(job, result)

        assert outcome.success is True
        assert outcome.pr_url == "https://bb.example/pr/42"
        hosting.open_pull_request.assert_not_called()

    def test_rework_comments_on_jira(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        job = Job.new("K-1", "r", {
            "title": "T",
            "job_type": "rework",
            "pr_url": "",
        })
        result = JobResult(
            job_id=job.job_id,
            success=True,
            agent_output="rework done",
            duration_seconds=3.0,
            branch_name="feature/K-1-ai",
        )

        post.run(job, result)

        tracker.add_comment.assert_called_once()
        comment_body = tracker.add_comment.call_args[0][1]
        assert "Rework completed" in comment_body

    def test_rework_calls_mark_rework_done(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        job = Job.new("K-1", "r", {
            "title": "T",
            "job_type": "rework",
            "pr_url": "",
        })
        result = JobResult(
            job_id=job.job_id,
            success=True,
            agent_output="done",
            duration_seconds=1.0,
            branch_name="feature/K-1-ai",
        )

        post.run(job, result)

        tracker.mark_rework_done.assert_called_once_with("K-1")
        tracker.mark_done.assert_not_called()

    def test_rework_mark_done_failure_is_non_fatal(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        tracker.mark_rework_done.side_effect = RuntimeError("Jira down")
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        job = Job.new("K-1", "r", {
            "title": "T",
            "job_type": "rework",
            "pr_url": "",
        })
        result = JobResult(
            job_id=job.job_id,
            success=True,
            agent_output="done",
            duration_seconds=1.0,
            branch_name="feature/K-1-ai",
        )

        outcome = post.run(job, result)

        assert outcome.success is True


# ----------------------------------------------------------------
# HostingReworkResolver tests
# ----------------------------------------------------------------

class TestHostingReworkResolver:
    def test_finds_pr_by_story_key_in_branch(self):
        from yokai.queue_adapters.rework_resolver import HostingReworkResolver

        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        hosting.find_pull_requests.return_value = [
            PullRequest(
                id="10",
                url="https://bb.example/pr/10",
                title="[AI] S-1",
                source_branch="feature/S-1-ai-20260420",
                target_branch="master",
            ),
            PullRequest(
                id="11",
                url="https://bb.example/pr/11",
                title="Manual PR",
                source_branch="feature/manual-change",
                target_branch="master",
            ),
        ]
        hosting.get_pr_comments.return_value = [
            PRComment(
                id="100",
                author="Francesco",
                text="Fix this",
                file_path="src/Main.java",
                line=42,
            ),
        ]

        resolver = HostingReworkResolver(hosting)
        result = resolver.resolve("S-1", "r")

        assert result is not None
        assert result["branch_name"] == "feature/S-1-ai-20260420"
        assert result["pr_id"] == "10"
        assert len(result["pr_comments"]) == 1
        assert result["pr_comments"][0]["author"] == "Francesco"

    def test_returns_none_when_no_matching_pr(self):
        from yokai.queue_adapters.rework_resolver import HostingReworkResolver

        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        hosting.find_pull_requests.return_value = [
            PullRequest(
                id="11",
                url="",
                title="Other",
                source_branch="feature/other-branch",
                target_branch="master",
            ),
        ]

        resolver = HostingReworkResolver(hosting)
        result = resolver.resolve("S-1", "r")

        assert result is None

    def test_returns_none_when_repo_resolve_fails(self):
        from yokai.queue_adapters.rework_resolver import HostingReworkResolver

        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.side_effect = RuntimeError("repo gone")

        resolver = HostingReworkResolver(hosting)
        result = resolver.resolve("S-1", "r")

        assert result is None

    def test_returns_empty_comments_when_fetch_fails(self):
        from yokai.queue_adapters.rework_resolver import HostingReworkResolver

        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        hosting.find_pull_requests.return_value = [
            PullRequest(
                id="10",
                url="https://bb.example/pr/10",
                title="[AI] S-1",
                source_branch="feature/S-1-ai",
                target_branch="master",
            ),
        ]
        hosting.get_pr_comments.side_effect = RuntimeError("API error")

        resolver = HostingReworkResolver(hosting)
        result = resolver.resolve("S-1", "r")

        assert result is not None
        assert result["pr_comments"] == []

    def test_case_insensitive_branch_matching(self):
        from yokai.queue_adapters.rework_resolver import HostingReworkResolver

        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        hosting.find_pull_requests.return_value = [
            PullRequest(
                id="10",
                url="",
                title="AI",
                source_branch="feature/NOVA-101-ai",
                target_branch="master",
            ),
        ]
        hosting.get_pr_comments.return_value = []

        resolver = HostingReworkResolver(hosting)
        result = resolver.resolve("nova-101", "r")

        assert result is not None
        assert result["branch_name"] == "feature/NOVA-101-ai"
