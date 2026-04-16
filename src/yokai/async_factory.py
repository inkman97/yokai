"""Factory for the async coordinator/worker/result-handler pipeline.

Reads FrameworkConfig.queue and produces ready-to-run components.
Reuses the same tracker/hosting/agent/router builders as the legacy
build_pipeline, then wraps them with the queue_adapters.

Usage:
    config = load_config("config.yaml")
    if config.queue is None:
        raise ConfigurationError("queue: section is required for async mode")
    coord = build_coordinator(config)
    coord.run()
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yokai.core.config import FrameworkConfig
from yokai.core.exceptions import ConfigurationError
from yokai.core.hooks import HookRegistry
from yokai.core.logging_setup import get_logger, register_secret
from yokai.factory import (
    _AGENT_BUILDERS,
    _HOSTING_BUILDERS,
    _ROUTER_BUILDERS,
    _TRACKER_BUILDERS,
    _load_plugin,
)
from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.backends.sqlite import SqliteBackend
from yokai.queue.coordinator import Coordinator, CoordinatorSettings
from yokai.queue.result_handler import ResultHandler, ResultHandlerSettings
from yokai.queue.worker import Worker, WorkerSettings
from yokai.queue_adapters import (
    AgentCodingRunner,
    HostingRepoCheckout,
    HostingTrackerPostprocessor,
    TrackerIssueSource,
)

log = get_logger("async_factory")


class _HookHost:
    """Minimal Pipeline-compatible object that exposes `_hooks`.

    Plugins written against the monolithic Pipeline typically do
    `pipeline._hooks.register(...)`. In async mode there is no
    Pipeline, so we hand them this tiny shim instead. All that the
    existing plugins access is `_hooks`, so this is sufficient for
    backwards compatibility.
    """

    def __init__(self, hooks: HookRegistry) -> None:
        self._hooks = hooks

    @property
    def hooks(self) -> HookRegistry:
        return self._hooks


def _build_hooks(config: FrameworkConfig) -> HookRegistry:
    """Construct a HookRegistry and run all plugins against it.

    Plugins defined in config.plugins receive a _HookHost (shim that
    exposes ._hooks) so that plugin code using pipeline._hooks keeps
    working in async mode without modification.
    """
    hooks = HookRegistry()
    host = _HookHost(hooks)
    for plugin_path in config.plugins:
        log.info(f"Loading plugin (async): {plugin_path}")
        try:
            plugin_fn = _load_plugin(plugin_path)
            plugin_fn(host)
        except Exception:
            log.exception(f"Plugin {plugin_path} failed to install")
    return hooks


def _build_backend(config: FrameworkConfig) -> Any:
    if config.queue is None:
        raise ConfigurationError(
            "queue section missing in config; required for async mode"
        )
    qc = config.queue
    if qc.backend == "memory":
        log.warning(
            "Using in-memory backend - state is lost on restart and "
            "not shared between processes. Use sqlite or redis for any real use."
        )
        return InMemoryBackend()
    if qc.backend == "sqlite":
        db_path = Path(qc.db_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return SqliteBackend(db_path)
    if qc.backend == "redis":
        try:
            from yokai.queue.backends.redis import RedisBackend
        except ImportError as e:
            raise ConfigurationError(
                f"queue.backend=redis requires the 'redis' package: {e}. "
                f"Install with: pip install redis"
            )
        if not qc.redis_url:
            raise ConfigurationError(
                "queue.redis_url is required when queue.backend is 'redis'"
            )
        # Register Redis URL as secret (may contain password) so logging
        # filter redacts it.
        try:
            register_secret(qc.redis_url)
        except Exception:
            pass
        return RedisBackend(url=qc.redis_url)
    raise ConfigurationError(
        f"Unknown queue.backend: {qc.backend}. Known: memory, sqlite, redis"
    )


def _build_tracker_and_hosting_and_agent(config: FrameworkConfig):
    """Build the underlying yokai adapters using the existing registries."""
    register_secret(config.issue_tracker.token)
    register_secret(config.repo_hosting.token)

    if config.issue_tracker.type not in _TRACKER_BUILDERS:
        raise ConfigurationError(
            f"Unknown issue_tracker.type: {config.issue_tracker.type}"
        )
    tracker = _TRACKER_BUILDERS[config.issue_tracker.type](config)

    if config.repo_hosting.type not in _HOSTING_BUILDERS:
        raise ConfigurationError(
            f"Unknown repo_hosting.type: {config.repo_hosting.type}"
        )
    hosting = _HOSTING_BUILDERS[config.repo_hosting.type](config)

    if config.agent.type not in _AGENT_BUILDERS:
        raise ConfigurationError(f"Unknown agent.type: {config.agent.type}")
    agent = _AGENT_BUILDERS[config.agent.type](config)

    if config.routing.type not in _ROUTER_BUILDERS:
        raise ConfigurationError(f"Unknown routing.type: {config.routing.type}")
    router = _ROUTER_BUILDERS[config.routing.type](config)

    return tracker, hosting, agent, router


def build_coordinator(config: FrameworkConfig) -> Coordinator:
    """Build a ready-to-run Coordinator."""
    if config.queue is None:
        raise ConfigurationError("queue section missing")

    backend = _build_backend(config)
    tracker, _hosting, _agent, router = _build_tracker_and_hosting_and_agent(
        config
    )

    source = TrackerIssueSource(tracker)
    cc = config.queue.coordinator
    settings = CoordinatorSettings(
        poll_interval_seconds=cc.poll_interval_seconds,
        lease_duration_seconds=cc.lease_duration_seconds,
        reclaim_interval_seconds=cc.reclaim_interval_seconds,
        max_attempts_per_job=cc.max_attempts_per_job,
    )
    return Coordinator(
        source=source,
        router=router,
        queue=backend,
        lock=backend,
        settings=settings,
    )


def build_worker(config: FrameworkConfig) -> Worker:
    """Build a ready-to-run Worker."""
    if config.queue is None:
        raise ConfigurationError("queue section missing")

    backend = _build_backend(config)
    _tracker, hosting, agent, _router = _build_tracker_and_hosting_and_agent(
        config
    )

    workspace_dir = Path(config.orchestrator.workspace_dir).expanduser()
    checkout = HostingRepoCheckout(
        hosting=hosting,
        workspace_dir=workspace_dir,
        branch_pattern=config.repo_hosting.branch_pattern,
    )
    runner = AgentCodingRunner(agent)

    wc = config.queue.worker
    settings = WorkerSettings(
        poll_interval_seconds=wc.poll_interval_seconds,
        lease_duration_seconds=wc.lease_duration_seconds,
        agent_timeout_seconds=wc.agent_timeout_seconds,
        heartbeat_interval_seconds=wc.heartbeat_interval_seconds,
        retry_backoff_base_seconds=wc.retry_backoff_base_seconds,
        retry_backoff_cap_seconds=wc.retry_backoff_cap_seconds,
    )
    hooks = _build_hooks(config)
    return Worker(
        queue=backend,
        results=backend,
        registry=backend,
        agent=runner,
        checkout=checkout,
        settings=settings,
        hooks=hooks,
    )


def build_result_handler(config: FrameworkConfig) -> ResultHandler:
    """Build a ready-to-run ResultHandler."""
    if config.queue is None:
        raise ConfigurationError("queue section missing")

    backend = _build_backend(config)
    tracker, hosting, _agent, _router = _build_tracker_and_hosting_and_agent(
        config
    )

    hooks = _build_hooks(config)
    postprocessor = HostingTrackerPostprocessor(
        hosting=hosting,
        tracker=tracker,
        workspace_dir=Path(config.orchestrator.workspace_dir).expanduser(),
        hooks=hooks,
    )
    rc = config.queue.result_handler
    settings = ResultHandlerSettings(
        poll_interval_seconds=rc.poll_interval_seconds,
        batch_size=rc.batch_size,
    )
    return ResultHandler(
        queue=backend,
        results=backend,
        postprocessor=postprocessor,
        settings=settings,
    )


def build_backend_only(config: FrameworkConfig):
    """For status / retry commands that only need queue state, not adapters."""
    return _build_backend(config)
