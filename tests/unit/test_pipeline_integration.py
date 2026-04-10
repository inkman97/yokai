"""Tests for Pipeline integration with hooks, notification sinks, and stores."""

from pathlib import Path

import pytest

from yokai.core.hooks import HookRegistry
from yokai.core.interfaces import NotificationSink
from yokai.core.models import PullRequest, Story
from yokai.core.pipeline import Pipeline, PipelineSettings
from yokai.core.routers import ComponentMapRouter
from yokai.storage.memory_store import InMemoryExecutionStore

from tests.unit.fakes import FakeAgent, FakeHosting, FakeTracker


def make_story(key="N-1", component="EMU-BE"):
    return Story(
        key=key,
        title="Test story",
        description="do the thing",
        components=[component],
        labels=["ai-pipeline"],
    )


def make_pipeline(
    tmp_path: Path,
    *,
    hooks=None,
    sinks=None,
    store=None,
):
    return Pipeline(
        tracker=FakeTracker(stories=[make_story()]),
        router=ComponentMapRouter({"EMU-BE": "repo-be"}),
        hosting=FakeHosting(),
        agent=FakeAgent(),
        settings=PipelineSettings(
            workspace_dir=tmp_path,
            poll_interval_seconds=1,
        ),
        hooks=hooks,
        notification_sinks=sinks,
        execution_store=store,
    )


class CapturingSink(NotificationSink):
    def __init__(self):
        self.started = []
        self.succeeded = []
        self.failed = []

    def notify_started(self, story, repo_slug):
        self.started.append((story.key, repo_slug))

    def notify_succeeded(self, story, pr):
        self.succeeded.append((story.key, pr.url))

    def notify_failed(self, story, error):
        self.failed.append((story.key, error))


class TestHookEmissions:
    def test_happy_path_emits_all_lifecycle_events(self, tmp_path):
        events = []
        hooks = HookRegistry()
        for ev in [
            "before_process",
            "after_resolve_repo",
            "after_clone",
            "before_agent_run",
            "after_agent_run",
            "after_commit",
            "after_push",
            "after_pull_request",
            "on_success",
        ]:
            hooks.register(ev, lambda payload, e=ev: events.append(e))

        pipeline = make_pipeline(tmp_path, hooks=hooks)
        pipeline.process_story(make_story())

        assert events == [
            "before_process",
            "after_resolve_repo",
            "after_clone",
            "before_agent_run",
            "after_agent_run",
            "after_commit",
            "after_push",
            "after_pull_request",
            "on_success",
        ]

    def test_failure_emits_on_failure(self, tmp_path):
        events = []
        hooks = HookRegistry()
        hooks.register("on_failure", lambda payload: events.append(payload))
        hooks.register("on_success", lambda payload: events.append("success"))

        story = Story(
            key="N-1", title="x", description="", components=["UNKNOWN"]
        )
        pipeline = Pipeline(
            tracker=FakeTracker(stories=[story]),
            router=ComponentMapRouter({"EMU-BE": "repo-be"}),
            hosting=FakeHosting(),
            agent=FakeAgent(),
            settings=PipelineSettings(workspace_dir=tmp_path),
            hooks=hooks,
        )

        from yokai.core.exceptions import RoutingError
        with pytest.raises(RoutingError):
            pipeline.process_story(story)

        assert "success" not in events
        assert len(events) == 1
        assert events[0]["story"].key == "N-1"

    def test_payload_contains_expected_keys(self, tmp_path):
        captured = {}
        hooks = HookRegistry()
        hooks.register(
            "after_pull_request",
            lambda payload: captured.update(payload),
        )

        pipeline = make_pipeline(tmp_path, hooks=hooks)
        pipeline.process_story(make_story())

        assert "story" in captured
        assert "pull_request" in captured
        assert isinstance(captured["pull_request"], PullRequest)


class TestNotificationSinks:
    def test_success_path_calls_started_and_succeeded(self, tmp_path):
        sink = CapturingSink()
        pipeline = make_pipeline(tmp_path, sinks=[sink])
        pipeline.process_story(make_story())

        assert sink.started == [("N-1", "repo-be")]
        assert len(sink.succeeded) == 1
        assert sink.succeeded[0][0] == "N-1"
        assert sink.failed == []

    def test_failure_path_calls_failed(self, tmp_path):
        sink = CapturingSink()
        story = Story(
            key="N-1", title="x", description="", components=["UNKNOWN"]
        )
        pipeline = Pipeline(
            tracker=FakeTracker(stories=[story]),
            router=ComponentMapRouter({"EMU-BE": "repo-be"}),
            hosting=FakeHosting(),
            agent=FakeAgent(),
            settings=PipelineSettings(workspace_dir=tmp_path),
            notification_sinks=[sink],
        )
        from yokai.core.exceptions import RoutingError
        with pytest.raises(RoutingError):
            pipeline.process_story(story)

        assert sink.started == []
        assert len(sink.failed) == 1

    def test_failing_sink_does_not_break_pipeline(self, tmp_path):
        class BrokenSink(NotificationSink):
            def notify_started(self, story, repo_slug):
                raise RuntimeError("broken")
            def notify_succeeded(self, story, pr):
                raise RuntimeError("broken")
            def notify_failed(self, story, error):
                raise RuntimeError("broken")

        good_sink = CapturingSink()
        pipeline = make_pipeline(
            tmp_path, sinks=[BrokenSink(), good_sink]
        )
        pipeline.process_story(make_story())
        assert len(good_sink.succeeded) == 1


class TestExecutionStoreIntegration:
    def test_success_records_completed(self, tmp_path):
        store = InMemoryExecutionStore()
        pipeline = make_pipeline(tmp_path, store=store)
        pipeline.process_story(make_story())

        records = store.list_recent()
        assert len(records) == 1
        assert records[0]["status"] == "completed"
        assert records[0]["pr_url"] is not None

    def test_failure_records_failed(self, tmp_path):
        store = InMemoryExecutionStore()
        story = Story(
            key="N-1", title="x", description="", components=["UNKNOWN"]
        )
        pipeline = Pipeline(
            tracker=FakeTracker(stories=[story]),
            router=ComponentMapRouter({"EMU-BE": "repo-be"}),
            hosting=FakeHosting(),
            agent=FakeAgent(),
            settings=PipelineSettings(workspace_dir=tmp_path),
            execution_store=store,
        )

        from yokai.core.exceptions import RoutingError
        with pytest.raises(RoutingError):
            pipeline.process_story(story)

        records = store.list_recent()
        assert len(records) == 1
        assert records[0]["status"] == "failed"
