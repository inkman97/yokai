"""Tests for the queue_adapters bridge layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yokai.core.interfaces import (
    CodingAgent,
    IssueTracker,
    RepoHosting,
)
from yokai.core.models import (
    AgentResult,
    Branch,
    CommitInfo,
    FileChange,
    PullRequest,
    RepoLocation,
    Story,
)
from yokai.queue.models import Job, JobResult
from yokai.queue_adapters import (
    AgentCodingRunner,
    HostingRepoCheckout,
    HostingTrackerPostprocessor,
    TrackerIssueSource,
    job_to_story,
    story_to_snapshot,
)

class TestConversions:
    def test_story_to_snapshot_preserves_fields(self):
        story = Story(
            key="K-1",
            title="t",
            description="d",
            components=["A"],
            labels=["x"],
            url="http://example/K-1",
            raw={"foo": "bar"},
        )
        snap = story_to_snapshot(story)
        assert snap.key == "K-1"
        assert snap.title == "t"
        assert snap.components == ["A"]
        assert snap.labels == ["x"]
        assert snap.raw == {"foo": "bar"}

    def test_job_to_story_from_payload(self):
        job = Job.new(
            "K-1",
            "repo",
            {
                "title": "T",
                "description": "D",
                "components": ["A"],
                "labels": ["L"],
            },
        )
        story = job_to_story(job)
        assert story.key == "K-1"
        assert story.title == "T"
        assert story.description == "D"
        assert story.components == ["A"]
        assert story.labels == ["L"]

class TestTrackerIssueSource:
    def test_fetch_pending_returns_snapshots(self):
        tracker = MagicMock(spec=IssueTracker)
        tracker.search_pending_stories.return_value = [
            Story(key="K-1", title="t1", description="d1"),
            Story(key="K-2", title="t2", description="d2"),
        ]
        source = TrackerIssueSource(tracker)
        snaps = source.fetch_pending()
        assert [s.key for s in snaps] == ["K-1", "K-2"]
        tracker.search_pending_stories.assert_called_once()

    def test_mark_accepted_calls_mark_in_progress(self):
        tracker = MagicMock(spec=IssueTracker)
        TrackerIssueSource(tracker).mark_accepted("K-1")
        tracker.mark_in_progress.assert_called_once_with("K-1")

    def test_mark_rejected_calls_mark_failed_with_reason(self):
        tracker = MagicMock(spec=IssueTracker)
        TrackerIssueSource(tracker).mark_rejected("K-1", "no repo")
        tracker.mark_failed.assert_called_once_with("K-1", "no repo")

class TestHostingRepoCheckout:
    def test_prepare_resolves_clones_branches(self, tmp_path: Path):
        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        repo_path = tmp_path / "ws" / "r"
        hosting.clone_or_update.return_value = repo_path

        checkout = HostingRepoCheckout(
            hosting=hosting,
            workspace_dir=tmp_path / "ws",
            branch_pattern="feature/{issue_key}-ai",
        )
        job = Job.new("K-1", "r", {"title": "Fix bug"})

        info = checkout.prepare(job)

        hosting.resolve_repo.assert_called_once_with("r")
        hosting.clone_or_update.assert_called_once()
        hosting.create_branch.assert_called_once()
        branch_arg = hosting.create_branch.call_args[0][1]
        assert isinstance(branch_arg, Branch)
        assert branch_arg.name.startswith("feature/K-1-ai")
        assert branch_arg.base == "master"
        assert info.repo_path == repo_path
        assert info.branch_name == branch_arg.name
        assert info.base_branch == "master"

    def test_cleanup_is_noop(self):
        hosting = MagicMock(spec=RepoHosting)
        checkout = HostingRepoCheckout(hosting, Path("/tmp/ws"))
        checkout.cleanup(Job.new("K-1", "r", {}))  # should not raise

class TestAgentCodingRunner:
    def test_run_calls_agent_with_built_prompt(self, tmp_path: Path):
        agent = MagicMock(spec=CodingAgent)
        agent.run.return_value = AgentResult(
            success=True, output="implemented", duration_seconds=12.0
        )
        runner = AgentCodingRunner(agent)
        job = Job.new(
            "K-1",
            "r",
            {"title": "T", "description": "D"},
        )
        execution = runner.run(job, tmp_path, timeout_seconds=30)

        agent.run.assert_called_once()
        passed_path = agent.run.call_args[0][0]
        passed_prompt = agent.run.call_args[0][1]
        assert passed_path == tmp_path
        assert "K-1" in passed_prompt
        assert "T" in passed_prompt
        assert "D" in passed_prompt
        assert execution.success is True
        assert execution.output == "implemented"

    def test_run_translates_failure(self, tmp_path: Path):
        agent = MagicMock(spec=CodingAgent)
        agent.run.return_value = AgentResult(
            success=False,
            output="",
            duration_seconds=1.0,
            error="agent reported failure",
        )
        execution = AgentCodingRunner(agent).run(
            Job.new("K-1", "r", {"title": "x"}), tmp_path, 10
        )
        assert execution.success is False
        assert execution.error == "agent reported failure"

    def test_run_catches_agent_timeout(self, tmp_path: Path):
        from yokai.core.exceptions import AgentTimeoutError

        agent = MagicMock(spec=CodingAgent)
        agent.run.side_effect = AgentTimeoutError("timed out after 1800s")
        execution = AgentCodingRunner(agent).run(
            Job.new("K-1", "r", {"title": "x"}), tmp_path, 10
        )
        assert execution.success is False
        assert "timed out" in execution.error.lower()
        assert execution.traceback is not None

    def test_run_catches_unexpected_exception(self, tmp_path: Path):
        agent = MagicMock(spec=CodingAgent)
        agent.run.side_effect = RuntimeError("subprocess died")
        execution = AgentCodingRunner(agent).run(
            Job.new("K-1", "r", {"title": "x"}), tmp_path, 10
        )
        assert execution.success is False
        assert "subprocess died" in execution.error
        assert execution.traceback is not None

class TestPostprocessorHappyPath:
    def _make(self, tmp_path):
        hosting = MagicMock(spec=RepoHosting)
        tracker = MagicMock(spec=IssueTracker)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        repo_dir = tmp_path / "r"
        repo_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo_dir, capture_output=True)
        hosting.clone_or_update.return_value = repo_dir
        hosting.get_changed_files.return_value = [
            FileChange(path="src/x.py", added=5, removed=1),
        ]
        hosting.open_pull_request.return_value = PullRequest(
            id="42",
            url="https://bb.example/r/pr/42",
            title="t",
            source_branch="feature/K-1-ai",
            target_branch="master",
        )
        tracker.get_story_url.return_value = "https://jira.example/K-1"
        return hosting, tracker

    def test_full_flow_returns_success_with_pr_url(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        job = Job.new("K-1", "r", {"title": "T", "description": "D"})
        result = JobResult(
            job_id=job.job_id,
            success=True,
            agent_output="ok",
            duration_seconds=5.0,
            branch_name="feature/K-1-ai",
        )
        outcome = post.run(job, result)
        assert outcome.success is True
        assert outcome.pr_url == "https://bb.example/r/pr/42"

        # Worker did commit+push, so Postprocessor should NOT call them
        hosting.commit_changes.assert_not_called()
        hosting.push_branch.assert_not_called()
        # But PR and comments should happen
        hosting.open_pull_request.assert_called_once()
        assert tracker.add_comment.call_count == 2

    def test_pr_creation_failure_returns_error(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        hosting.open_pull_request.side_effect = RuntimeError("conflict")
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        outcome = post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="feature/x",
                duration_seconds=1,
            ),
        )
        assert outcome.success is False
        assert "conflict" in outcome.error

    def test_comment_failure_does_not_invalidate_success(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        tracker.add_comment.side_effect = RuntimeError("Jira down")
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        outcome = post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="feature/x",
                duration_seconds=1,
            ),
        )
        assert outcome.success is True
        assert outcome.pr_url == "https://bb.example/r/pr/42"

    def test_rejects_non_success_result(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        outcome = post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=False, error="agent failed",
                branch_name="feature/x", duration_seconds=1,
            ),
        )
        assert outcome.success is False
        hosting.open_pull_request.assert_not_called()

    def test_rejects_result_without_branch_name(self, tmp_path):
        hosting, tracker = self._make(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker, workspace_dir=tmp_path
        )
        outcome = post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name=None,
                duration_seconds=1,
            ),
        )
        assert outcome.success is False
        assert "branch_name" in outcome.error
