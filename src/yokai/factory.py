"""Factory that builds a Pipeline from a FrameworkConfig.

The factory is the only place that knows how to map a `type` string in
the YAML config to a concrete adapter class. Adding a new adapter means
adding it to one of the registry dicts here, or registering it
externally via the public register_* helpers.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Callable

from yokai.adapters.bitbucket_dc import BitbucketDataCenterHosting
from yokai.adapters.bitbucket_dc.hosting import (
    BitbucketDataCenterSettings,
)
from yokai.adapters.claude_code import ClaudeCodeAgent
from yokai.adapters.claude_code.agent import ClaudeCodeSettings
from yokai.adapters.jira_dc import JiraDataCenterTracker
from yokai.adapters.jira_dc.tracker import JiraDataCenterSettings
from yokai.core.config import FrameworkConfig
from yokai.core.exceptions import ConfigurationError
from yokai.core.interfaces import (
    CodingAgent,
    ExecutionStore,
    IssueTracker,
    NotificationSink,
    RepoHosting,
    StoryRouter,
)
from yokai.core.logging_setup import get_logger, register_secret
from yokai.core.pipeline import Pipeline, PipelineSettings
from yokai.core.routers import (
    ChainRouter,
    ComponentMapRouter,
    LabelPrefixRouter,
)
from yokai.storage.memory_store import InMemoryExecutionStore
from yokai.storage.sqlite_store import SqliteExecutionStore

log = get_logger("factory")


TrackerBuilder = Callable[[FrameworkConfig], IssueTracker]
HostingBuilder = Callable[[FrameworkConfig], RepoHosting]
AgentBuilder = Callable[[FrameworkConfig], CodingAgent]
RouterBuilder = Callable[[FrameworkConfig], StoryRouter]
StoreBuilder = Callable[[FrameworkConfig], ExecutionStore]


_TRACKER_BUILDERS: dict[str, TrackerBuilder] = {}
_HOSTING_BUILDERS: dict[str, HostingBuilder] = {}
_AGENT_BUILDERS: dict[str, AgentBuilder] = {}
_ROUTER_BUILDERS: dict[str, RouterBuilder] = {}
_STORE_BUILDERS: dict[str, StoreBuilder] = {}


def register_tracker(type_name: str, builder: TrackerBuilder) -> None:
    _TRACKER_BUILDERS[type_name] = builder


def register_hosting(type_name: str, builder: HostingBuilder) -> None:
    _HOSTING_BUILDERS[type_name] = builder


def register_agent(type_name: str, builder: AgentBuilder) -> None:
    _AGENT_BUILDERS[type_name] = builder


def register_router(type_name: str, builder: RouterBuilder) -> None:
    _ROUTER_BUILDERS[type_name] = builder


def register_store(type_name: str, builder: StoreBuilder) -> None:
    _STORE_BUILDERS[type_name] = builder


def _build_jira_dc(config: FrameworkConfig) -> IssueTracker:
    s = config.issue_tracker
    return JiraDataCenterTracker(
        JiraDataCenterSettings(
            base_url=s.base_url,
            project=s.project,
            username=s.username,
            token=s.token,
            trigger_label=s.trigger_label,
            processing_label=s.processing_label,
            status=s.status,
        )
    )


def _build_bitbucket_dc(config: FrameworkConfig) -> RepoHosting:
    s = config.repo_hosting
    return BitbucketDataCenterHosting(
        BitbucketDataCenterSettings(
            base_url=s.base_url,
            project_key=s.project_key,
            username=s.username,
            token=s.token,
            default_branch=s.default_branch,
        )
    )


def _build_claude_code(config: FrameworkConfig) -> CodingAgent:
    s = config.agent
    return ClaudeCodeAgent(
        ClaudeCodeSettings(
            command=s.command,
            flags=list(s.flags),
            timeout_seconds=s.timeout_seconds,
        )
    )


def _build_component_map_router(config: FrameworkConfig) -> StoryRouter:
    s = config.routing
    component_router = ComponentMapRouter(s.components)
    label_router = LabelPrefixRouter(s.label_prefix)
    return ChainRouter([component_router, label_router])


def _build_memory_store(config: FrameworkConfig) -> ExecutionStore:
    return InMemoryExecutionStore()


def _build_sqlite_store(config: FrameworkConfig) -> ExecutionStore:
    if not config.storage.path:
        raise ConfigurationError(
            "storage.path is required when storage.type is 'sqlite'"
        )
    return SqliteExecutionStore(config.storage.path)


_TRACKER_BUILDERS["jira_dc"] = _build_jira_dc
_HOSTING_BUILDERS["bitbucket_dc"] = _build_bitbucket_dc
_AGENT_BUILDERS["claude_code"] = _build_claude_code
_ROUTER_BUILDERS["component_map"] = _build_component_map_router
_STORE_BUILDERS["memory"] = _build_memory_store
_STORE_BUILDERS["sqlite"] = _build_sqlite_store


def _load_plugin(dotted_path: str) -> Callable:
    """Import a plugin from a dotted Python path.

    The path must point to a callable that takes a Pipeline as its only
    argument. The plugin is responsible for registering its own hooks
    or sinks against the pipeline.
    """
    if ":" in dotted_path:
        module_name, attr = dotted_path.split(":", 1)
    elif "." in dotted_path:
        module_name, attr = dotted_path.rsplit(".", 1)
    else:
        raise ConfigurationError(
            f"Invalid plugin path: {dotted_path} (use 'module:function')"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ConfigurationError(
            f"Failed to import plugin module {module_name}: {e}"
        ) from e
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise ConfigurationError(
            f"Plugin {dotted_path}: {attr} not found in {module_name}"
        ) from e


def build_pipeline(config: FrameworkConfig) -> Pipeline:
    """Build a fully wired Pipeline from a FrameworkConfig.

    Registers tokens with the logging redaction filter so they never
    appear in any log output.
    """
    register_secret(config.issue_tracker.token)
    register_secret(config.repo_hosting.token)

    tracker_type = config.issue_tracker.type
    if tracker_type not in _TRACKER_BUILDERS:
        raise ConfigurationError(
            f"Unknown issue_tracker.type: {tracker_type}. "
            f"Known: {sorted(_TRACKER_BUILDERS)}"
        )
    tracker = _TRACKER_BUILDERS[tracker_type](config)

    hosting_type = config.repo_hosting.type
    if hosting_type not in _HOSTING_BUILDERS:
        raise ConfigurationError(
            f"Unknown repo_hosting.type: {hosting_type}. "
            f"Known: {sorted(_HOSTING_BUILDERS)}"
        )
    hosting = _HOSTING_BUILDERS[hosting_type](config)

    agent_type = config.agent.type
    if agent_type not in _AGENT_BUILDERS:
        raise ConfigurationError(
            f"Unknown agent.type: {agent_type}. "
            f"Known: {sorted(_AGENT_BUILDERS)}"
        )
    agent = _AGENT_BUILDERS[agent_type](config)

    router_type = config.routing.type
    if router_type not in _ROUTER_BUILDERS:
        raise ConfigurationError(
            f"Unknown routing.type: {router_type}. "
            f"Known: {sorted(_ROUTER_BUILDERS)}"
        )
    router = _ROUTER_BUILDERS[router_type](config)

    store_type = config.storage.type
    if store_type not in _STORE_BUILDERS:
        raise ConfigurationError(
            f"Unknown storage.type: {store_type}. "
            f"Known: {sorted(_STORE_BUILDERS)}"
        )
    store = _STORE_BUILDERS[store_type](config)

    settings = PipelineSettings(
        workspace_dir=Path(config.orchestrator.workspace_dir).expanduser(),
        branch_pattern=config.repo_hosting.branch_pattern,
        poll_interval_seconds=config.orchestrator.poll_interval_seconds,
        max_parallel_stories=config.orchestrator.max_parallel_stories,
    )

    pipeline = Pipeline(
        tracker=tracker,
        router=router,
        hosting=hosting,
        agent=agent,
        settings=settings,
        execution_store=store,
    )

    for plugin_path in config.plugins:
        log.info(f"Loading plugin: {plugin_path}")
        plugin_fn = _load_plugin(plugin_path)
        plugin_fn(pipeline)

    return pipeline
