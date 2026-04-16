"""Worker-side abstractions.

The Worker depends on AgentRunner and RepoCheckout. Concrete
implementations:
  - ClaudeCodeRunner (in yokai.adapters.claude_code) - launches the
    `claude` CLI as a subprocess
  - FakeAgentRunner (in tests) - returns canned results
  - BitbucketCheckout (in yokai.adapters.bitbucket_dc) - clones via git
  - FakeCheckout (in tests) - returns prepared temp dirs

Keeping these as ABCs here means the worker can be unit-tested with
no dependency on the actual claude binary, git, or any HTTP library.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from yokai.queue.models import Job


@dataclass
class AgentExecution:
    """The raw outcome of a single agent invocation."""

    success: bool
    output: str = ""
    error: str | None = None
    traceback: str | None = None


@dataclass
class CheckoutInfo:
    """Information about the prepared workspace for a job."""

    repo_path: Path
    branch_name: str
    base_branch: str


class AgentRunner(ABC):
    """A coding agent that can be invoked on a repository checkout."""

    @abstractmethod
    def run(
        self,
        job: Job,
        repo_path: Path,
        timeout_seconds: float,
    ) -> AgentExecution:
        """Invoke the agent on repo_path, blocking until done or timeout.

        Timeouts must be enforced and reported as success=False with an
        appropriate error message - they must not raise.
        """


class RepoCheckout(ABC):
    """Prepares a working tree where the agent can make changes.

    Implementations clone the remote repo (if not already present),
    fetch the latest base branch, and create a fresh feature branch.
    The returned CheckoutInfo tells the worker where the agent should
    operate and what branch will eventually be pushed.
    """

    @abstractmethod
    def prepare(self, job: Job) -> CheckoutInfo:
        """Prepare the workspace for this job. Returns when ready."""

    @abstractmethod
    def cleanup(self, job: Job) -> None:
        """Optional post-job cleanup. Worker calls this regardless of
        agent outcome. Default implementation may be a no-op."""
