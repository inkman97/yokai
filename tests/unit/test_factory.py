"""Tests for the Pipeline factory."""

from pathlib import Path

import pytest

from yokai.core.config import (
    AgentConfig,
    FrameworkConfig,
    IssueTrackerConfig,
    OrchestratorConfig,
    RepoHostingConfig,
    RoutingConfig,
    StorageConfig,
)
from yokai.core.exceptions import ConfigurationError
from yokai.core.pipeline import Pipeline
from yokai.factory import (
    build_pipeline,
    register_agent,
    register_hosting,
    register_router,
    register_store,
    register_tracker,
)
from yokai.storage.memory_store import InMemoryExecutionStore
from yokai.storage.sqlite_store import SqliteExecutionStore


def make_config(tmp_path: Path, **overrides) -> FrameworkConfig:
    base = FrameworkConfig(
        issue_tracker=IssueTrackerConfig(
            type="jira_dc",
            base_url="https://jira.example.com",
            project="NOVA",
            trigger_label="ai-pipeline",
            processing_label="ai-processing",
            status="Backlog",
            username="user",
            token="jira-secret",
        ),
        repo_hosting=RepoHostingConfig(
            type="bitbucket_dc",
            base_url="https://code.example.com",
            project_key="myproj",
            username="user",
            token="bb-secret",
        ),
        agent=AgentConfig(type="claude_code"),
        routing=RoutingConfig(
            type="component_map",
            components={"BE": "backend-repo"},
        ),
        orchestrator=OrchestratorConfig(workspace_dir=str(tmp_path)),
        storage=StorageConfig(type="memory"),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class TestBuildPipeline:
    def test_builds_pipeline_with_known_adapters(self, tmp_path):
        config = make_config(tmp_path)
        pipeline = build_pipeline(config)
        assert isinstance(pipeline, Pipeline)

    def test_unknown_tracker_type_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.issue_tracker.type = "nonexistent_tracker"
        with pytest.raises(ConfigurationError, match="issue_tracker.type"):
            build_pipeline(config)

    def test_unknown_hosting_type_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.repo_hosting.type = "nonexistent_hosting"
        with pytest.raises(ConfigurationError, match="repo_hosting.type"):
            build_pipeline(config)

    def test_unknown_agent_type_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.agent.type = "nonexistent_agent"
        with pytest.raises(ConfigurationError, match="agent.type"):
            build_pipeline(config)

    def test_unknown_routing_type_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.routing.type = "nonexistent_router"
        with pytest.raises(ConfigurationError, match="routing.type"):
            build_pipeline(config)

    def test_unknown_storage_type_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.storage.type = "nonexistent_store"
        with pytest.raises(ConfigurationError, match="storage.type"):
            build_pipeline(config)

    def test_sqlite_storage_requires_path(self, tmp_path):
        config = make_config(tmp_path)
        config.storage.type = "sqlite"
        config.storage.path = None
        with pytest.raises(ConfigurationError, match="storage.path is required"):
            build_pipeline(config)

    def test_sqlite_storage_with_path_succeeds(self, tmp_path):
        config = make_config(tmp_path)
        config.storage.type = "sqlite"
        config.storage.path = str(tmp_path / "state.db")
        pipeline = build_pipeline(config)
        assert isinstance(pipeline, Pipeline)


class TestPluginRegistration:
    def test_register_custom_tracker(self, tmp_path):
        from yokai.adapters.jira_dc import JiraDataCenterTracker
        from yokai.adapters.jira_dc.tracker import JiraDataCenterSettings

        sentinel = {"called": False}

        def custom(config):
            sentinel["called"] = True
            return JiraDataCenterTracker(
                JiraDataCenterSettings(
                    base_url="x", project="x", username="x", token="x"
                )
            )

        register_tracker("custom_tracker", custom)
        config = make_config(tmp_path)
        config.issue_tracker.type = "custom_tracker"
        build_pipeline(config)
        assert sentinel["called"] is True

    def test_plugin_loading_invalid_path_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.plugins = ["no_separator_at_all"]
        with pytest.raises(ConfigurationError, match="Invalid plugin path"):
            build_pipeline(config)

    def test_plugin_loading_missing_module_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.plugins = ["nonexistent_module_xyz:func"]
        with pytest.raises(ConfigurationError, match="Failed to import"):
            build_pipeline(config)

    def test_plugin_loading_missing_attribute_raises(self, tmp_path):
        config = make_config(tmp_path)
        config.plugins = ["yokai:does_not_exist_attr"]
        with pytest.raises(ConfigurationError, match="not found"):
            build_pipeline(config)
