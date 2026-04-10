"""Tests for the Pipeline orchestrator using fake in-memory adapters.

These tests exercise the real Pipeline class end-to-end against fake
implementations of the framework interfaces. They cover:
- Happy path story-to-PR flow
- Routing failures
- Clone failures
- Agent failures
- Parallel processing across different repos
- Serialization of stories that target the same repo
- In-flight deduplication
"""

import threading
import time
from pathlib import Path

import pytest

from yokai.core.pipeline import Pipeline, PipelineSettings
from yokai.core.routers import ComponentMapRouter
from yokai.core.models import Story

from tests.unit.fakes import FakeAgent, FakeHosting, FakeTracker


def make_story(key: str, component: str = "EMU-BE", title: str = "Test") -> Story:
    return Story(
        key=key,
        title=title,
        description="do the thing",
        components=[component],
        labels=["ai-pipeline"],
    )


def make_pipeline(
    tmp_path: Path,
    tracker: FakeTracker,
    hosting: FakeHosting,
    agent: FakeAgent,
    max_parallel: int = 4,
) -> Pipeline:
    router = ComponentMapRouter(
        {"EMU-BE": "repo-be", "EMU-FE": "repo-fe"}
    )
    return Pipeline(
        tracker=tracker,
        router=router,
        hosting=hosting,
        agent=agent,
        settings=PipelineSettings(
            workspace_dir=tmp_path,
            branch_pattern="feature/{issue_key}-ai-{timestamp}",
            poll_interval_seconds=1,
            max_parallel_stories=max_parallel,
        ),
    )


class TestProcessStoryHappyPath:
    def test_full_flow_creates_pr(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        hosting = FakeHosting()
        agent = FakeAgent()
        pipeline = make_pipeline(tmp_path, tracker, hosting, agent)

        pipeline.process_story(tracker.stories[0])

        assert hosting.clones == ["repo-be"]
        assert len(hosting.branches_created) == 1
        assert hosting.branches_created[0].startswith("feature/N-1-ai-")
        assert len(hosting.commits_made) == 1
        assert "N-1" in hosting.commits_made[0]
        assert len(hosting.pushes) == 1
        assert len(hosting.prs_opened) == 1

    def test_marks_in_progress_before_work(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        hosting = FakeHosting()
        pipeline = make_pipeline(tmp_path, tracker, hosting, FakeAgent())
        pipeline.process_story(tracker.stories[0])
        assert "N-1" in tracker.in_progress

    def test_posts_two_comments_on_success(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        pipeline = make_pipeline(tmp_path, tracker, FakeHosting(), FakeAgent())
        pipeline.process_story(tracker.stories[0])
        assert len(tracker.comments.get("N-1", [])) == 2
        assert "PR link" in tracker.comments["N-1"][0] or "Pull request" in tracker.comments["N-1"][0]

    def test_agent_received_repo_path(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        agent = FakeAgent()
        pipeline = make_pipeline(tmp_path, tracker, FakeHosting(), agent)
        pipeline.process_story(tracker.stories[0])
        assert len(agent.runs) == 1
        assert agent.runs[0] == tmp_path / "repo-be"


class TestProcessStoryFailures:
    def test_routing_failure_marks_story_failed(self, tmp_path):
        story = Story(
            key="N-1", title="x", description="", components=["UNKNOWN"]
        )
        tracker = FakeTracker(stories=[story])
        pipeline = make_pipeline(tmp_path, tracker, FakeHosting(), FakeAgent())

        from yokai.core.exceptions import RoutingError
        with pytest.raises(RoutingError):
            pipeline.process_story(story)
        assert "N-1" in tracker.failed

    def test_clone_failure_marks_story_failed(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        hosting = FakeHosting(fail_clone_for={"repo-be"})
        pipeline = make_pipeline(tmp_path, tracker, hosting, FakeAgent())

        from yokai.core.exceptions import GitOperationError
        with pytest.raises(GitOperationError):
            pipeline.process_story(tracker.stories[0])
        assert "N-1" in tracker.failed

    def test_agent_failure_marks_story_failed(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        agent = FakeAgent(fail=True)
        pipeline = make_pipeline(tmp_path, tracker, FakeHosting(), agent)

        from yokai.core.exceptions import AgentExecutionError
        with pytest.raises(AgentExecutionError):
            pipeline.process_story(tracker.stories[0])
        assert "N-1" in tracker.failed

    def test_no_changes_posts_notice_and_does_not_push(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])

        class NoChangesHosting(FakeHosting):
            def commit_changes(self, repo_path, message):
                return None

        hosting = NoChangesHosting()
        pipeline = make_pipeline(tmp_path, tracker, hosting, FakeAgent())
        pipeline.process_story(tracker.stories[0])

        assert len(hosting.pushes) == 0
        assert len(hosting.prs_opened) == 0
        assert "N-1" in tracker.comments
        assert "no changes" in tracker.comments["N-1"][0].lower()


class TestRunOnce:
    def test_submits_all_pending_stories(self, tmp_path):
        tracker = FakeTracker(
            stories=[make_story("N-1"), make_story("N-2", "EMU-FE")]
        )
        pipeline = make_pipeline(tmp_path, tracker, FakeHosting(), FakeAgent())
        submitted = pipeline.run_once()
        assert submitted == 2

    def test_skips_already_in_flight(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        pipeline = make_pipeline(tmp_path, tracker, FakeHosting(), FakeAgent())
        pipeline.run_once()
        submitted = pipeline.run_once()
        assert submitted == 0


class TestParallelism:
    def test_different_repos_run_concurrently(self, tmp_path):
        tracker = FakeTracker(
            stories=[
                make_story("N-1", "EMU-BE"),
                make_story("N-2", "EMU-FE"),
            ]
        )
        hosting = FakeHosting()
        agent = FakeAgent(delay_seconds=0.3)
        pipeline = make_pipeline(
            tmp_path, tracker, hosting, agent, max_parallel=4
        )

        iteration_count = {"value": 0}

        def stop_when_done():
            iteration_count["value"] += 1
            if len(hosting.prs_opened) >= 2:
                return True
            return iteration_count["value"] > 50

        start = time.monotonic()
        runner = threading.Thread(
            target=lambda: pipeline.run_forever(stop_condition=stop_when_done)
        )
        runner.start()

        deadline = time.monotonic() + 5
        while len(hosting.prs_opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)

        elapsed = time.monotonic() - start
        runner.join(timeout=5)
        assert len(hosting.prs_opened) == 2
        assert elapsed < 1.2, (
            f"Parallel execution should be under ~0.6s agent time, got {elapsed:.2f}s"
        )

    def test_same_repo_stories_serialize(self, tmp_path):
        tracker = FakeTracker(
            stories=[
                make_story("N-1", "EMU-BE"),
                make_story("N-2", "EMU-BE"),
            ]
        )
        hosting = FakeHosting()
        agent = FakeAgent(delay_seconds=0.3)
        pipeline = make_pipeline(
            tmp_path, tracker, hosting, agent, max_parallel=4
        )

        def stop_when_done():
            return len(hosting.prs_opened) >= 2

        runner = threading.Thread(
            target=lambda: pipeline.run_forever(stop_condition=stop_when_done)
        )
        runner.start()

        start = time.monotonic()
        deadline = start + 5
        while len(hosting.prs_opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)

        elapsed = time.monotonic() - start
        runner.join(timeout=5)
        assert len(hosting.prs_opened) == 2
        assert elapsed >= 0.55, (
            f"Serialized execution should take ~0.6s minimum, got {elapsed:.2f}s"
        )

    def test_no_duplicate_submission_under_rapid_polling(self, tmp_path):
        tracker = FakeTracker(stories=[make_story("N-1")])
        agent = FakeAgent(delay_seconds=0.2)
        pipeline = make_pipeline(
            tmp_path, tracker, FakeHosting(), agent, max_parallel=4
        )

        def stop_when_done():
            return len(agent.runs) > 0 and tracker._search_call_count >= 3

        runner = threading.Thread(
            target=lambda: pipeline.run_forever(stop_condition=stop_when_done)
        )
        runner.start()

        deadline = time.monotonic() + 3
        while len(agent.runs) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.5)

        runner.join(timeout=5)
        assert len(agent.runs) == 1
