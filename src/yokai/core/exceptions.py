"""Framework exception hierarchy.

All framework-raised errors inherit from SpecPipelineError so user code
can catch the whole family with a single except clause.
"""


class SpecPipelineError(Exception):
    """Base exception for all framework errors."""


class ConfigurationError(SpecPipelineError):
    """Raised when configuration is missing, invalid, or incomplete."""


class IssueTrackerError(SpecPipelineError):
    """Raised when an issue tracker operation fails."""


class RepoHostingError(SpecPipelineError):
    """Raised when a repository hosting operation fails."""


class GitOperationError(RepoHostingError):
    """Raised when a local git operation fails."""


class AgentExecutionError(SpecPipelineError):
    """Raised when the coding agent fails to run or returns a non-zero exit."""


class AgentTimeoutError(AgentExecutionError):
    """Raised when the coding agent exceeds its allotted time budget."""


class RoutingError(SpecPipelineError):
    """Raised when a story cannot be routed to a repository."""


class StorageError(SpecPipelineError):
    """Raised when persistent state storage fails."""
