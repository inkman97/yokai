"""YAML configuration loading and validation.

The framework is configured through a single YAML file. Environment
variables can be referenced as ${VAR_NAME} and are expanded at load time.
Missing required fields raise ConfigurationError with a clear message
pointing at the offending key path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from yokai.core.exceptions import ConfigurationError


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


@dataclass
class IssueTrackerConfig:
    type: str
    base_url: str
    project: str
    trigger_label: str
    processing_label: str
    status: str
    username: str
    token: str


@dataclass
class RepoHostingConfig:
    type: str
    base_url: str
    project_key: str
    username: str
    token: str
    default_branch: str = "master"
    branch_pattern: str = "feature/{issue_key}-ai-{timestamp}"


@dataclass
class AgentConfig:
    type: str
    command: str = "claude"
    flags: list[str] = field(default_factory=list)
    timeout_seconds: int = 1800


@dataclass
class RoutingConfig:
    type: str = "component_map"
    components: dict[str, str] = field(default_factory=dict)
    label_prefix: str = "repo:"


@dataclass
class OrchestratorConfig:
    poll_interval_seconds: int = 30
    max_parallel_stories: int = 4
    workspace_dir: str = "~/yokai-workspace"


@dataclass
class StorageConfig:
    type: str = "memory"
    path: str | None = None


@dataclass
class FrameworkConfig:
    issue_tracker: IssueTrackerConfig
    repo_hosting: RepoHostingConfig
    agent: AgentConfig
    routing: RoutingConfig
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    plugins: list[str] = field(default_factory=list)


def expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR_NAME} placeholders in strings."""
    if isinstance(value, str):
        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                raise ConfigurationError(
                    f"Environment variable {var_name} is referenced "
                    f"in config but not set"
                )
            return env_value
        return _ENV_VAR_PATTERN.sub(replacer, value)
    if isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    return value


def _require(data: dict, key: str, parent: str = "") -> Any:
    if key not in data:
        path = f"{parent}.{key}" if parent else key
        raise ConfigurationError(f"Required config key missing: {path}")
    return data[key]


def _parse_issue_tracker(data: dict) -> IssueTrackerConfig:
    return IssueTrackerConfig(
        type=_require(data, "type", "issue_tracker"),
        base_url=_require(data, "base_url", "issue_tracker"),
        project=_require(data, "project", "issue_tracker"),
        trigger_label=data.get("trigger_label", "ai-pipeline"),
        processing_label=data.get("processing_label", "ai-processing"),
        status=data.get("status", "Backlog"),
        username=_require(data, "username", "issue_tracker"),
        token=_require(data, "token", "issue_tracker"),
    )


def _parse_repo_hosting(data: dict) -> RepoHostingConfig:
    return RepoHostingConfig(
        type=_require(data, "type", "repo_hosting"),
        base_url=_require(data, "base_url", "repo_hosting"),
        project_key=_require(data, "project_key", "repo_hosting"),
        username=_require(data, "username", "repo_hosting"),
        token=_require(data, "token", "repo_hosting"),
        default_branch=data.get("default_branch", "master"),
        branch_pattern=data.get(
            "branch_pattern", "feature/{issue_key}-ai-{timestamp}"
        ),
    )


def _parse_agent(data: dict) -> AgentConfig:
    return AgentConfig(
        type=_require(data, "type", "agent"),
        command=data.get("command", "claude"),
        flags=data.get("flags", []),
        timeout_seconds=int(data.get("timeout_seconds", 1800)),
    )


def _parse_routing(data: dict) -> RoutingConfig:
    return RoutingConfig(
        type=data.get("type", "component_map"),
        components=data.get("components", {}),
        label_prefix=data.get("label_prefix", "repo:"),
    )


def _parse_orchestrator(data: dict) -> OrchestratorConfig:
    return OrchestratorConfig(
        poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
        max_parallel_stories=int(data.get("max_parallel_stories", 4)),
        workspace_dir=data.get("workspace_dir", "~/yokai-workspace"),
    )


def _parse_storage(data: dict) -> StorageConfig:
    return StorageConfig(
        type=data.get("type", "memory"),
        path=data.get("path"),
    )


def load_config(path: str | Path) -> FrameworkConfig:
    """Load and validate the framework configuration from a YAML file."""
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise ConfigurationError(f"Configuration file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        try:
            raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML in {config_path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"Configuration root must be a mapping, got {type(raw).__name__}"
        )

    expanded = expand_env_vars(raw)

    return FrameworkConfig(
        issue_tracker=_parse_issue_tracker(_require(expanded, "issue_tracker")),
        repo_hosting=_parse_repo_hosting(_require(expanded, "repo_hosting")),
        agent=_parse_agent(_require(expanded, "agent")),
        routing=_parse_routing(_require(expanded, "routing")),
        orchestrator=_parse_orchestrator(expanded.get("orchestrator", {})),
        storage=_parse_storage(expanded.get("storage", {})),
        plugins=expanded.get("plugins", []),
    )
