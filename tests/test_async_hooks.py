"""Tests for hook emission in the async mode.

These tests verify that all nine lifecycle hooks (the same events
emitted by the monolithic Pipeline) are emitted by the Worker and
HostingTrackerPostprocessor, with compatible payloads so existing
plugins keep working.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yokai.core.hooks import HookRegistry
from yokai.core.interfaces import (
    CodingAgent,
    IssueTracker,
    RepoHosting,
)
from yokai.core.models import (
    AgentResult,
    CommitInfo,
    FileChange,
    PullRequest,
    RepoLocation,
    Story,
)
from yokai.queue import (
    AgentExecution,
    Job,
    JobResult,
    JobStatus,
    Worker,
    WorkerSettings,
)
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue_adapters import (
    AgentCodingRunner,
    HostingRepoCheckout,
    HostingTrackerPostprocessor,
)


@pytest.fixture
def backend():
    return InMemoryBackend()


@pytest.fixture
def recorded_events():
    """Shared list where hook callbacks write the events they receive."""
    return []


@pytest.fixture
def hooks(recorded_events):
    reg = HookRegistry()
    for event in (
            "before_process",
            "after_resolve_repo",
            "after_clone",
            "before_agent_run",
            "after_agent_run",
            "after_commit",
            "after_push",
            "after_pull_request",
            "on_success",
            "on_failure",
    ):
        def make_callback(e):
            def callback(payload):
                recorded_events.append((e, payload))
            return callback
        reg.register(event, make_callback(event))
    return reg

class TestWorkerHooks:
    def _make_worker(self, backend, hooks, tmp_path, agent_success=True):
        coding_agent = MagicMock(spec=CodingAgent)
        coding_agent.run.return_value = AgentResult(
            success=agent_success,
            output="hello" if agent_success else "",
            duration_seconds=1.5,
            error=None if agent_success else "boom",
        )
        runner = AgentCodingRunner(coding_agent)

        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        repo_path = tmp_path / "r"
        repo_path.mkdir(exist_ok=True)
        hosting.clone_or_update.return_value = repo_path
        checkout = HostingRepoCheckout(hosting, tmp_path, "feature/{issue_key}-ai")

        worker = Worker(
            queue=backend,
            results=backend,
            registry=backend,
            agent=runner,
            checkout=checkout,
            settings=WorkerSettings(
                retry_backoff_base_seconds=0,
                poll_interval_seconds=0.01,
            ),
            hooks=hooks,
        )
        return worker

    def test_happy_path_emits_before_clone_agent_run_events(
            self, backend, hooks, recorded_events, tmp_path
    ):
        worker = self._make_worker(backend, hooks, tmp_path)
        backend.enqueue(Job.new("K-1", "r", {"title": "T", "description": "D"}))

        worker.process_one_job()

        names = [e[0] for e in recorded_events]
        assert "before_process" in names
        assert "after_resolve_repo" in names
        assert "after_clone" in names
        assert "before_agent_run" in names
        assert "after_agent_run" in names

    def test_hook_payloads_carry_story(
            self, backend, hooks, recorded_events, tmp_path
    ):
        worker = self._make_worker(backend, hooks, tmp_path)
        backend.enqueue(Job.new("K-1", "r", {"title": "T", "description": "D"}))
        worker.process_one_job()

        worker_events = {
            "before_process",
            "after_resolve_repo",
            "after_clone",
            "before_agent_run",
            "after_agent_run",
        }
        for name, payload in recorded_events:
            if name in worker_events:
                assert "story" in payload
                assert isinstance(payload["story"], Story)
                assert payload["story"].key == "K-1"

    def test_after_resolve_repo_payload_has_repo_slug(
            self, backend, hooks, recorded_events, tmp_path
    ):
        worker = self._make_worker(backend, hooks, tmp_path)
        backend.enqueue(Job.new("K-1", "repo-xyz", {"title": "T"}))
        worker.process_one_job()

        arr = [p for (n, p) in recorded_events if n == "after_resolve_repo"]
        assert len(arr) == 1
        assert arr[0]["repo_slug"] == "repo-xyz"

    def test_after_agent_run_payload_carries_agent_result(
            self, backend, hooks, recorded_events, tmp_path
    ):
        worker = self._make_worker(backend, hooks, tmp_path)
        backend.enqueue(Job.new("K-1", "r", {"title": "T"}))
        worker.process_one_job()

        arr = [p for (n, p) in recorded_events if n == "after_agent_run"]
        assert len(arr) == 1
        agent_result = arr[0]["agent_result"]
        assert isinstance(agent_result, AgentResult)
        assert agent_result.success is True
        assert agent_result.output == "hello"

    def test_agent_failure_emits_on_failure(
            self, backend, hooks, recorded_events, tmp_path
    ):
        worker = self._make_worker(backend, hooks, tmp_path, agent_success=False)
        backend.enqueue(
            Job.new("K-1", "r", {"title": "T"}, max_attempts=1)
        )
        worker.process_one_job()

        names = [e[0] for e in recorded_events]
        assert "on_failure" in names

    def test_checkout_failure_emits_on_failure(
            self, backend, hooks, recorded_events, tmp_path
    ):
        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.side_effect = RuntimeError("clone denied")

        coding_agent = MagicMock(spec=CodingAgent)
        worker = Worker(
            queue=backend,
            results=backend,
            registry=backend,
            agent=AgentCodingRunner(coding_agent),
            checkout=HostingRepoCheckout(hosting, tmp_path),
            settings=WorkerSettings(
                retry_backoff_base_seconds=0,
                poll_interval_seconds=0.01,
            ),
            hooks=hooks,
        )
        backend.enqueue(
            Job.new("K-1", "r", {"title": "T"}, max_attempts=1)
        )
        worker.process_one_job()

        names = [e[0] for e in recorded_events]
        assert "on_failure" in names
        coding_agent.run.assert_not_called()

    def test_failing_hook_does_not_crash_worker(self, backend, tmp_path):
        """A plugin callback that throws must not break the pipeline."""
        reg = HookRegistry()

        def bad_callback(payload):
            raise RuntimeError("plugin bug")

        reg.register("after_agent_run", bad_callback)

        coding_agent = MagicMock(spec=CodingAgent)
        coding_agent.run.return_value = AgentResult(
            success=True, output="ok", duration_seconds=1.0
        )
        hosting = MagicMock(spec=RepoHosting)
        hosting.resolve_repo.return_value = RepoLocation(
            slug="r", namespace="ns", default_branch="master"
        )
        repo_path = tmp_path / "r"
        repo_path.mkdir(exist_ok=True)
        hosting.clone_or_update.return_value = repo_path

        worker = Worker(
            queue=backend,
            results=backend,
            registry=backend,
            agent=AgentCodingRunner(coding_agent),
            checkout=HostingRepoCheckout(hosting, tmp_path),
            settings=WorkerSettings(
                retry_backoff_base_seconds=0,
                poll_interval_seconds=0.01,
            ),
            hooks=reg,
        )
        backend.enqueue(Job.new("K-1", "r", {"title": "T"}))
        assert worker.process_one_job() is True
        assert backend.stats()[JobStatus.AGENT_COMPLETED] == 1

def _make_post_mocks(tmp_path):
    hosting = MagicMock(spec=RepoHosting)
    tracker = MagicMock(spec=IssueTracker)
    hosting.resolve_repo.return_value = RepoLocation(
        slug="r", namespace="ns", default_branch="master"
    )
    hosting.clone_or_update.return_value = tmp_path / "r"
    hosting.commit_changes.return_value = CommitInfo(
        sha="a" * 40, short_sha="aaaaaaa", message="msg",
        files_changed=2, insertions=10, deletions=3,
    )
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


class TestPostprocessorHooks:
    def test_happy_path_emits_commit_push_pr_success(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting,
            tracker=tracker,
            workspace_dir=tmp_path,
            hooks=hooks,
        )
        outcome = post.run(
            Job.new("K-1", "r", {"title": "T", "description": "D"}),
            JobResult(
                job_id="j",
                success=True,
                branch_name="feature/K-1-ai",
                duration_seconds=5.0,
                agent_output="done",
            ),
        )
        assert outcome.success is True

        names = [e[0] for e in recorded_events]
        assert names.count("after_commit") == 1
        assert names.count("after_push") == 1
        assert names.count("after_pull_request") == 1
        assert names.count("on_success") == 1
        assert "on_failure" not in names

    def test_after_commit_payload_has_commit(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting,
            tracker=tracker,
            workspace_dir=tmp_path,
            hooks=hooks,
        )
        post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="b", duration_seconds=1
            ),
        )
        commit_events = [p for (n, p) in recorded_events if n == "after_commit"]
        assert len(commit_events) == 1
        assert isinstance(commit_events[0]["commit"], CommitInfo)

    def test_after_pull_request_payload_has_pr(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting,
            tracker=tracker,
            workspace_dir=tmp_path,
            hooks=hooks,
        )
        post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="b", duration_seconds=1
            ),
        )
        pr_events = [
            p for (n, p) in recorded_events if n == "after_pull_request"
        ]
        assert len(pr_events) == 1
        assert isinstance(pr_events[0]["pull_request"], PullRequest)
        assert pr_events[0]["pull_request"].url == "https://bb.example/r/pr/42"

    def test_on_success_payload_has_pr(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker,
            workspace_dir=tmp_path, hooks=hooks,
        )
        post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="b", duration_seconds=1
            ),
        )
        success_events = [p for (n, p) in recorded_events if n == "on_success"]
        assert len(success_events) == 1
        assert isinstance(success_events[0]["pull_request"], PullRequest)

    def test_push_failure_emits_on_failure_not_after_push(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        hosting.push_branch.side_effect = RuntimeError("denied")
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker,
            workspace_dir=tmp_path, hooks=hooks,
        )
        post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="b", duration_seconds=1
            ),
        )
        names = [e[0] for e in recorded_events]
        assert "after_commit" in names
        assert "after_push" not in names
        assert "after_pull_request" not in names
        assert "on_success" not in names
        assert "on_failure" in names

    def test_pr_failure_emits_on_failure(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        hosting.open_pull_request.side_effect = RuntimeError("conflict")
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker,
            workspace_dir=tmp_path, hooks=hooks,
        )
        post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="b", duration_seconds=1
            ),
        )
        names = [e[0] for e in recorded_events]
        assert "after_push" in names
        assert "on_failure" in names
        assert "on_success" not in names

    def test_no_changes_emits_on_failure(
            self, hooks, recorded_events, tmp_path
    ):
        hosting, tracker = _make_post_mocks(tmp_path)
        hosting.commit_changes.return_value = None
        post = HostingTrackerPostprocessor(
            hosting=hosting, tracker=tracker,
            workspace_dir=tmp_path, hooks=hooks,
        )
        post.run(
            Job.new("K-1", "r", {"title": "x"}),
            JobResult(
                job_id="j", success=True, branch_name="b", duration_seconds=1
            ),
        )
        names = [e[0] for e in recorded_events]
        assert "on_failure" in names
        assert "after_commit" not in names
        assert "on_success" not in names

class TestLegacyPluginAgainstAsyncMode:
    """Verify that a plugin written against the monolithic Pipeline
    (using `pipeline._hooks.register(...)`) works in async mode too
    thanks to the _HookHost compatibility shim."""

    def test_pipeline_style_plugin_works_with_hook_host(self):
        from yokai.async_factory import _HookHost

        received = []

        def legacy_plugin(pipeline):
            pipeline._hooks.register(
                "after_pull_request",
                lambda payload: received.append(payload),
            )

        hooks = HookRegistry()
        host = _HookHost(hooks)
        legacy_plugin(host)

        hooks.emit("after_pull_request", {"story": None, "pull_request": "pr"})
        assert received == [{"story": None, "pull_request": "pr"}]
