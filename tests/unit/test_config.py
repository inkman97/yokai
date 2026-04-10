"""Unit tests for the YAML configuration loader."""

import os
from pathlib import Path

import pytest

from yokai.core.config import expand_env_vars, load_config
from yokai.core.exceptions import ConfigurationError


VALID_YAML = """
issue_tracker:
  type: jira_dc
  base_url: https://flow.example.com
  project: NOVA
  trigger_label: ai-pipeline
  processing_label: ai-processing
  status: Backlog
  username: testuser
  token: ${TEST_JIRA_TOKEN}

repo_hosting:
  type: bitbucket_dc
  base_url: https://code.example.com
  project_key: myproj
  username: testuser
  token: ${TEST_BB_TOKEN}
  default_branch: master
  branch_pattern: "feature/{issue_key}-ai-{timestamp}"

agent:
  type: claude_code
  command: claude
  flags:
    - --print
    - --dangerously-skip-permissions
  timeout_seconds: 1800

routing:
  type: component_map
  components:
    EMU-BE: nova-masterdata-editor-commons
    EMU-FE: nova-masterdata-editor-ui-commons

orchestrator:
  poll_interval_seconds: 30
  max_parallel_stories: 4
  workspace_dir: ~/workspace

storage:
  type: memory
"""


@pytest.fixture
def env_with_tokens(monkeypatch):
    monkeypatch.setenv("TEST_JIRA_TOKEN", "jira-token-value")
    monkeypatch.setenv("TEST_BB_TOKEN", "bb-token-value")


@pytest.fixture
def valid_config_file(tmp_path: Path, env_with_tokens) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(VALID_YAML)
    return cfg


class TestExpandEnvVars:
    def test_expands_single_variable(self, monkeypatch):
        monkeypatch.setenv("FOO", "hello")
        assert expand_env_vars("${FOO}") == "hello"

    def test_expands_inside_string(self, monkeypatch):
        monkeypatch.setenv("USER", "alice")
        assert expand_env_vars("user-${USER}-suffix") == "user-alice-suffix"

    def test_expands_inside_dict(self, monkeypatch):
        monkeypatch.setenv("KEY", "value")
        result = expand_env_vars({"a": "${KEY}", "b": "static"})
        assert result == {"a": "value", "b": "static"}

    def test_expands_inside_nested_list(self, monkeypatch):
        monkeypatch.setenv("X", "1")
        result = expand_env_vars(["${X}", "${X}-${X}"])
        assert result == ["1", "1-1"]

    def test_missing_variable_raises(self):
        with pytest.raises(ConfigurationError, match="MISSING_VAR"):
            expand_env_vars("${MISSING_VAR}")

    def test_non_string_values_pass_through(self):
        assert expand_env_vars(42) == 42
        assert expand_env_vars(None) is None
        assert expand_env_vars(True) is True


class TestLoadConfig:
    def test_loads_valid_yaml_file(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config.issue_tracker.type == "jira_dc"
        assert config.issue_tracker.base_url == "https://flow.example.com"
        assert config.issue_tracker.project == "NOVA"
        assert config.issue_tracker.token == "jira-token-value"

    def test_loads_repo_hosting_section(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config.repo_hosting.type == "bitbucket_dc"
        assert config.repo_hosting.project_key == "myproj"
        assert config.repo_hosting.token == "bb-token-value"
        assert config.repo_hosting.default_branch == "master"

    def test_loads_agent_with_flags(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config.agent.type == "claude_code"
        assert config.agent.command == "claude"
        assert "--print" in config.agent.flags
        assert "--dangerously-skip-permissions" in config.agent.flags
        assert config.agent.timeout_seconds == 1800

    def test_loads_routing_components(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config.routing.type == "component_map"
        assert config.routing.components["EMU-BE"] == "nova-masterdata-editor-commons"
        assert config.routing.components["EMU-FE"] == "nova-masterdata-editor-ui-commons"

    def test_loads_orchestrator_section(self, valid_config_file):
        config = load_config(valid_config_file)
        assert config.orchestrator.poll_interval_seconds == 30
        assert config.orchestrator.max_parallel_stories == 4

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            load_config(tmp_path / "nonexistent.yaml")

    def test_invalid_yaml_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: valid: yaml: at: all:::\n - [")
        with pytest.raises(ConfigurationError, match="Invalid YAML"):
            load_config(bad)

    def test_missing_required_section_raises(self, tmp_path, env_with_tokens):
        cfg = tmp_path / "incomplete.yaml"
        cfg.write_text("issue_tracker:\n  type: jira_dc\n")
        with pytest.raises(ConfigurationError, match="missing"):
            load_config(cfg)

    def test_missing_required_field_in_section_raises(self, tmp_path):
        cfg = tmp_path / "broken.yaml"
        cfg.write_text(
            "issue_tracker:\n"
            "  type: jira_dc\n"
            "  base_url: https://example.com\n"
            "repo_hosting: {}\n"
            "agent: {}\n"
            "routing: {}\n"
        )
        with pytest.raises(ConfigurationError, match="issue_tracker"):
            load_config(cfg)

    def test_root_must_be_mapping(self, tmp_path):
        cfg = tmp_path / "list.yaml"
        cfg.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigurationError, match="mapping"):
            load_config(cfg)

    def test_defaults_applied_when_optional_sections_missing(
        self, tmp_path, env_with_tokens
    ):
        cfg = tmp_path / "minimal.yaml"
        cfg.write_text(
            "issue_tracker:\n"
            "  type: jira_dc\n"
            "  base_url: https://flow.example.com\n"
            "  project: NOVA\n"
            "  username: u\n"
            "  token: ${TEST_JIRA_TOKEN}\n"
            "repo_hosting:\n"
            "  type: bitbucket_dc\n"
            "  base_url: https://code.example.com\n"
            "  project_key: p\n"
            "  username: u\n"
            "  token: ${TEST_BB_TOKEN}\n"
            "agent:\n"
            "  type: claude_code\n"
            "routing:\n"
            "  type: component_map\n"
        )
        config = load_config(cfg)
        assert config.orchestrator.poll_interval_seconds == 30
        assert config.orchestrator.max_parallel_stories == 4
        assert config.storage.type == "memory"
        assert config.issue_tracker.trigger_label == "ai-pipeline"
        assert config.issue_tracker.status == "Backlog"
