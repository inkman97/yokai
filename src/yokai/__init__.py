"""yokai: a framework for spec-driven development pipelines.

Public API surface lives in this module. Anything outside this file may
change without notice.
"""

from yokai.core.branch_naming import render_branch_name, slugify
from yokai.core.config import FrameworkConfig, load_config
from yokai.core.exceptions import (
    AgentExecutionError,
    AgentTimeoutError,
    ConfigurationError,
    GitOperationError,
    IssueTrackerError,
    RepoHostingError,
    RoutingError,
    SpecPipelineError,
    StorageError,
)
from yokai.core.hooks import HookCallback, HookRegistry
from yokai.core.interfaces import (
    CodingAgent,
    ExecutionStore,
    IssueTracker,
    NotificationSink,
    RepoHosting,
    StoryRouter,
)
from yokai.core.logging_setup import (
    configure_logging,
    get_logger,
    register_secret,
)
from yokai.core.models import (
    AgentResult,
    Branch,
    CommitInfo,
    FileChange,
    PullRequest,
    RepoLocation,
    Story,
    StoryExecution,
    StoryStatus,
)
from yokai.core.pipeline import Pipeline, PipelineSettings
from yokai.core.prompts import PromptBuilder, default_prompt_builder
from yokai.core.routers import (
    ChainRouter,
    ComponentMapRouter,
    LabelPrefixRouter,
)
from yokai.storage.memory_store import InMemoryExecutionStore
from yokai.storage.sqlite_store import SqliteExecutionStore

def _read_version() -> str:
    try:
        from importlib.metadata import version
        return version("yokai")
    except Exception:
        return "0.0.0+unknown"


__version__ = _read_version()

__all__ = [
    "AgentExecutionError",
    "AgentResult",
    "AgentTimeoutError",
    "Branch",
    "ChainRouter",
    "CodingAgent",
    "CommitInfo",
    "ComponentMapRouter",
    "ConfigurationError",
    "ExecutionStore",
    "FileChange",
    "FrameworkConfig",
    "GitOperationError",
    "HookCallback",
    "HookRegistry",
    "InMemoryExecutionStore",
    "IssueTracker",
    "IssueTrackerError",
    "LabelPrefixRouter",
    "NotificationSink",
    "Pipeline",
    "PipelineSettings",
    "PromptBuilder",
    "PullRequest",
    "RepoHosting",
    "RepoHostingError",
    "RepoLocation",
    "RoutingError",
    "SpecPipelineError",
    "SqliteExecutionStore",
    "Story",
    "StoryExecution",
    "StoryRouter",
    "StoryStatus",
    "StorageError",
    "__version__",
    "configure_logging",
    "default_prompt_builder",
    "get_logger",
    "load_config",
    "register_secret",
    "render_branch_name",
    "slugify",
]
