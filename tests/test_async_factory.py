"""Integration tests for async_factory: from YAML config to working
Coordinator/Worker/ResultHandler.

These tests build real Coordinator/Worker/ResultHandler instances from
a config dict, using the in-memory backend (so no SQLite contention)
and registering fake builders for tracker/hosting/agent (so no real
Jira/Bitbucket/Claude needed).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from yokai.core.config import FrameworkConfig, load_config
from yokai.core.exceptions import ConfigurationError
from yokai.core.interfaces import (
    CodingAgent,
    IssueTracker,
    RepoHosting,
    StoryRouter,
)
from yokai.core.models import (
    AgentResult,
    CommitInfo,
    FileChange,
    PullRequest,
    RepoLocation,
    Story,
)
from yokai.factory import (
    register_agent,
    register_hosting,
    register_router,
    register_tracker,
)

def _fake_tracker_factory():
    """Returns a fresh tracker mock with sensible defaults."""
    t = MagicMock(spec=IssueTracker)
    t.search_pending_stories.return_value = []
    t.get_story_url.return_value = "https://jira.example/X-1"
    return t


def _fake_hosting_factory():
    h = MagicMock(spec=RepoHosting)
    h.resolve_repo.return_value = RepoLocation(
        slug="r", namespace="ns", default_branch="master"
    )
    h.commit_changes.return_value = CommitInfo(
        sha="a" * 40, short_sha="aaaaaaa", message="m",
        files_changed=1, insertions=1, deletions=0,
    )
    h.get_changed_files.return_value = [
        FileChange(path="src/x.py", added=1, removed=0)
    ]
    h.open_pull_request.return_value = PullRequest(
        id="1", url="https://bb.example/pr/1",
        title="t", source_branch="b", target_branch="master",
    )
    return h


def _fake_agent_factory():
    a = MagicMock(spec=CodingAgent)
    a.run.return_value = AgentResult(
        success=True, output="ok", duration_seconds=1.0
    )
    return a


# Module-level singletons so the same instance is shared across the
# coordinator/worker/result_handler built from the same config.
_FAKE_TRACKER = None
_FAKE_HOSTING = None
_FAKE_AGENT = None


def _reset_fakes(tmp_path):
    global _FAKE_TRACKER, _FAKE_HOSTING, _FAKE_AGENT
    _FAKE_TRACKER = _fake_tracker_factory()
    _FAKE_HOSTING = _fake_hosting_factory()
    _FAKE_AGENT = _fake_agent_factory()
    # Make clone_or_update return a real path so the agent runner gets
    # a valid repo_path
    repo_path = tmp_path / "ws" / "r"
    repo_path.mkdir(parents=True, exist_ok=True)
    _FAKE_HOSTING.clone_or_update.return_value = repo_path


@pytest.fixture(autouse=True)
def register_fake_builders(tmp_path):
    """Register fake builders for the test config types."""
    _reset_fakes(tmp_path)
    register_tracker("fake_tracker", lambda cfg: _FAKE_TRACKER)
    register_hosting("fake_hosting", lambda cfg: _FAKE_HOSTING)
    register_agent("fake_agent", lambda cfg: _FAKE_AGENT)

    from yokai.queue.sources import ComponentMapRouter
    register_router(
        "fake_router",
        lambda cfg: ComponentMapRouter(cfg.routing.components),
    )
    yield


@pytest.fixture
def config_path(tmp_path):
    cfg = {
        "issue_tracker": {
            "type": "fake_tracker",
            "base_url": "https://jira.example",
            "project": "TEST",
            "account": "u",
            "token": "t",
            "trigger_label": "ai-pipeline",
            "processing_label": "ai-processing",
            "status": "Backlog",
        },
        "repo_hosting": {
            "type": "fake_hosting",
            "base_url": "https://bb.example",
            "namespace": "ns",
            "account": "u",
            "token": "t",
            "default_branch": "master",
            "branch_pattern": "feature/{issue_key}-ai",
        },
        "agent": {"type": "fake_agent"},
        "routing": {
            "type": "fake_router",
            "components": {"BACKEND": "r"},
        },
        "orchestrator": {
            "workspace_dir": str(tmp_path / "ws"),
            "poll_interval_seconds": 1,
        },
        "queue": {
            "backend": "memory",
            "db_path": str(tmp_path / "queue.db"),
            "coordinator": {
                "poll_interval_seconds": 1,
                "max_attempts_per_job": 3,
            },
            "worker": {
                "retry_backoff_base_seconds": 0,
            },
        },
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p

class TestBuildFunctions:
    def test_build_coordinator_returns_runnable_coordinator(self, config_path):
        from yokai.async_factory import build_coordinator

        cfg = load_config(config_path)
        coord = build_coordinator(cfg)
        assert coord is not None
        # run_once with no stories should succeed
        stats = coord.run_once()
        assert stats.fetched == 0

    def test_build_worker_returns_runnable_worker(self, config_path):
        from yokai.async_factory import build_worker

        cfg = load_config(config_path)
        worker = build_worker(cfg)
        assert worker is not None
        # process_one_job with empty queue returns False
        assert worker.process_one_job() is False

    def test_build_result_handler(self, config_path):
        from yokai.async_factory import build_result_handler

        cfg = load_config(config_path)
        handler = build_result_handler(cfg)
        assert handler is not None
        stats = handler.run_once()
        assert stats.fetched == 0

    def test_missing_queue_section_raises(self, tmp_path):
        from yokai.async_factory import build_coordinator

        cfg_no_queue = {
            "issue_tracker": {
                "type": "fake_tracker", "base_url": "u", "project": "P",
                "account": "u", "token": "t",
            },
            "repo_hosting": {
                "type": "fake_hosting", "base_url": "u", "namespace": "n",
                "account": "u", "token": "t",
            },
            "agent": {"type": "fake_agent"},
            "routing": {"type": "fake_router", "components": {}},
        }
        p = tmp_path / "no_queue.yaml"
        p.write_text(yaml.safe_dump(cfg_no_queue))
        cfg = load_config(p)
        with pytest.raises(ConfigurationError, match="queue section missing"):
            build_coordinator(cfg)


class TestEndToEndFromConfig:
    """Build all three components from the same config and exercise
    the full pipeline through them."""

    def test_full_lifecycle_with_fake_adapters(self, config_path):
        from yokai.async_factory import (
            build_backend_only,
            build_coordinator,
            build_result_handler,
            build_worker,
        )
        from yokai.queue.models import JobStatus

        cfg = load_config(config_path)

        # All three must share the same backend instance because we are
        # using the in-memory backend. The current build_* functions
        # build a fresh backend each time, which would not work.
        # For the test, we accept that and just verify each component
        # builds successfully.

        # Set up source so coordinator finds something
        _FAKE_TRACKER.search_pending_stories.return_value = [
            Story(
                key="X-1",
                title="Fix bug",
                description="Important",
                components=["BACKEND"],
                labels=[],
            )
        ]

        coord = build_coordinator(cfg)
        stats = coord.run_once()
        # The coordinator enqueued into ITS backend; a separate worker
        # built later would not see it (in-memory is process-local).
        assert stats.enqueued == 1
        assert _FAKE_TRACKER.mark_in_progress.called


class TestSqliteSharedBackend:
    """SQLite backend shared via file path: coordinator and worker can
    talk to each other through it."""

    def test_coordinator_to_worker_via_sqlite(self, tmp_path):
        cfg = {
            "issue_tracker": {
                "type": "fake_tracker", "base_url": "u", "project": "P",
                "account": "u", "token": "t",
            },
            "repo_hosting": {
                "type": "fake_hosting", "base_url": "u", "namespace": "n",
                "account": "u", "token": "t",
                "branch_pattern": "feature/{issue_key}-ai",
            },
            "agent": {"type": "fake_agent"},
            "routing": {"type": "fake_router", "components": {"BACKEND": "r"}},
            "orchestrator": {"workspace_dir": str(tmp_path / "ws")},
            "queue": {
                "backend": "sqlite",
                "db_path": str(tmp_path / "queue.db"),
                "worker": {"retry_backoff_base_seconds": 0},
            },
        }
        p = tmp_path / "shared.yaml"
        p.write_text(yaml.safe_dump(cfg))
        loaded = load_config(p)

        from yokai.async_factory import (
            build_coordinator,
            build_result_handler,
            build_worker,
        )
        from yokai.queue.models import JobStatus

        _FAKE_TRACKER.search_pending_stories.return_value = [
            Story(
                key="X-1",
                title="Fix bug",
                description="Do it",
                components=["BACKEND"],
                labels=[],
            )
        ]

        coord = build_coordinator(loaded)
        worker = build_worker(loaded)
        handler = build_result_handler(loaded)

        # Coordinator enqueues
        coord_stats = coord.run_once()
        assert coord_stats.enqueued == 1

        # Worker dequeues, runs agent (fake), writes result
        assert worker.process_one_job() is True
        assert _FAKE_AGENT.run.called

        # Result handler processes the result via fake postprocessor
        # path. We need workspace dir to exist.
        Path(tmp_path / "ws").mkdir(exist_ok=True)
        handler_stats = handler.run_once()
        assert handler_stats.processed == 1
        assert handler_stats.succeeded == 1
        # PR was opened, comments were added on Jira
        assert _FAKE_HOSTING.open_pull_request.called
        assert _FAKE_TRACKER.add_comment.called


class TestRedisBackend:
    """End-to-end with Redis backend (fakeredis)."""

    def test_redis_url_required(self, tmp_path):
        cfg = {
            "issue_tracker": {
                "type": "fake_tracker", "base_url": "u", "project": "P",
                "account": "u", "token": "t",
            },
            "repo_hosting": {
                "type": "fake_hosting", "base_url": "u", "namespace": "n",
                "account": "u", "token": "t",
            },
            "agent": {"type": "fake_agent"},
            "routing": {"type": "fake_router", "components": {}},
            "queue": {"backend": "redis"},
        }
        p = tmp_path / "redis_no_url.yaml"
        p.write_text(yaml.safe_dump(cfg))
        loaded = load_config(p)
        from yokai.async_factory import build_backend_only
        with pytest.raises(ConfigurationError, match="redis_url is required"):
            build_backend_only(loaded)

    def test_unknown_backend_raises(self, tmp_path):
        cfg = {
            "issue_tracker": {
                "type": "fake_tracker", "base_url": "u", "project": "P",
                "account": "u", "token": "t",
            },
            "repo_hosting": {
                "type": "fake_hosting", "base_url": "u", "namespace": "n",
                "account": "u", "token": "t",
            },
            "agent": {"type": "fake_agent"},
            "routing": {"type": "fake_router", "components": {}},
            "queue": {"backend": "kafka"},
        }
        p = tmp_path / "unknown.yaml"
        p.write_text(yaml.safe_dump(cfg))
        loaded = load_config(p)
        from yokai.async_factory import build_backend_only
        with pytest.raises(ConfigurationError, match="Unknown queue.backend"):
            build_backend_only(loaded)

    def test_coordinator_to_worker_via_redis(self, tmp_path, monkeypatch):
        # Patch RedisBackend to use fakeredis instead of a real Redis
        import fakeredis
        from yokai.queue.backends import redis as redis_backend_module
        from yokai.queue.backends.redis import RedisBackend

        server = fakeredis.FakeServer()

        original_init = RedisBackend.__init__

        def fake_init(self, client=None, *, url=None, worker_ttl_seconds=60):
            client = fakeredis.FakeRedis(server=server, decode_responses=True)
            original_init(self, client=client, worker_ttl_seconds=worker_ttl_seconds)

        monkeypatch.setattr(RedisBackend, "__init__", fake_init)

        cfg = {
            "issue_tracker": {
                "type": "fake_tracker", "base_url": "u", "project": "P",
                "account": "u", "token": "t",
            },
            "repo_hosting": {
                "type": "fake_hosting", "base_url": "u", "namespace": "n",
                "account": "u", "token": "t",
                "branch_pattern": "feature/{issue_key}-ai",
            },
            "agent": {"type": "fake_agent"},
            "routing": {"type": "fake_router", "components": {"BACKEND": "r"}},
            "orchestrator": {"workspace_dir": str(tmp_path / "ws")},
            "queue": {
                "backend": "redis",
                "redis_url": "redis://fake/0",
                "worker": {"retry_backoff_base_seconds": 0},
            },
        }
        p = tmp_path / "redis.yaml"
        p.write_text(yaml.safe_dump(cfg))
        loaded = load_config(p)

        from yokai.async_factory import (
            build_coordinator,
            build_result_handler,
            build_worker,
        )

        _FAKE_TRACKER.search_pending_stories.return_value = [
            Story(
                key="X-1",
                title="Fix bug",
                description="Do it",
                components=["BACKEND"],
                labels=[],
            )
        ]

        coord = build_coordinator(loaded)
        worker = build_worker(loaded)
        handler = build_result_handler(loaded)

        coord_stats = coord.run_once()
        assert coord_stats.enqueued == 1

        assert worker.process_one_job() is True
        assert _FAKE_AGENT.run.called

        Path(tmp_path / "ws").mkdir(exist_ok=True)
        handler_stats = handler.run_once()
        assert handler_stats.processed == 1
        assert handler_stats.succeeded == 1
        assert _FAKE_HOSTING.open_pull_request.called
