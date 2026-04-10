"""Abstract interfaces for swappable components.

The framework is built around these protocols. Each concrete adapter
(Jira Data Center, Bitbucket Data Center, Claude Code, etc.) implements
one or more of these interfaces. The orchestrator depends only on the
interfaces, never on concrete adapters, which keeps it provider-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from yokai.core.models import (
    AgentResult,
    Branch,
    CommitInfo,
    FileChange,
    PullRequest,
    RepoLocation,
    Story,
)


class IssueTracker(ABC):
    """A system that holds work items (stories, tickets, issues)."""

    @abstractmethod
    def search_pending_stories(self) -> list[Story]:
        """Return stories that match the configured trigger criteria."""

    @abstractmethod
    def mark_in_progress(self, story_key: str) -> None:
        """Mark a story as taken so it is not picked up twice."""

    @abstractmethod
    def mark_failed(self, story_key: str, reason: str) -> None:
        """Mark a story as failed and record the reason."""

    @abstractmethod
    def add_comment(self, story_key: str, body: str) -> None:
        """Post a comment on a story."""

    @abstractmethod
    def get_story_url(self, story_key: str) -> str:
        """Return the human-readable URL for a story."""


class RepoHosting(ABC):
    """A system that hosts git repositories and pull requests."""

    @abstractmethod
    def resolve_repo(self, slug: str) -> RepoLocation:
        """Resolve a repository slug into a full RepoLocation."""

    @abstractmethod
    def clone_or_update(self, repo: RepoLocation, workspace: Path) -> Path:
        """Clone the repository under workspace or fetch and reset if it exists.

        Returns the local path to the repository working tree.
        """

    @abstractmethod
    def create_branch(self, repo_path: Path, branch: Branch) -> None:
        """Create and checkout a new branch from the current HEAD."""

    @abstractmethod
    def commit_changes(
        self, repo_path: Path, message: str
    ) -> CommitInfo | None:
        """Stage all changes and commit them.

        Returns CommitInfo on success, None if there were no changes to commit.
        """

    @abstractmethod
    def push_branch(self, repo_path: Path, branch_name: str) -> None:
        """Push a branch to the remote."""

    @abstractmethod
    def get_changed_files(
        self, repo_path: Path, base_branch: str
    ) -> list[FileChange]:
        """Return the list of files changed in the current branch vs base."""

    @abstractmethod
    def open_pull_request(
        self,
        repo: RepoLocation,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> PullRequest:
        """Open a pull request and return its identifiers."""


class CodingAgent(ABC):
    """An AI coding agent that can implement a story inside a repository."""

    @abstractmethod
    def run(self, repo_path: Path, prompt: str) -> AgentResult:
        """Execute the agent in the given repository with the given prompt.

        The agent is expected to read files, modify files, and possibly run
        commands. The framework only cares that the agent returns control
        and that any modifications are visible in the working tree.
        """


class StoryRouter(ABC):
    """Decides which repository handles a given story."""

    @abstractmethod
    def resolve_repo(self, story: Story) -> str | None:
        """Return the repository slug for a story, or None if no match."""


class NotificationSink(ABC):
    """A destination for human-facing notifications about pipeline events."""

    @abstractmethod
    def notify_started(self, story: Story, repo_slug: str) -> None: ...

    @abstractmethod
    def notify_succeeded(self, story: Story, pr: PullRequest) -> None: ...

    @abstractmethod
    def notify_failed(self, story: Story, error: str) -> None: ...


class ExecutionStore(ABC):
    """Persistent storage for story execution state."""

    @abstractmethod
    def is_in_flight(self, story_key: str) -> bool: ...

    @abstractmethod
    def mark_in_flight(self, story_key: str) -> bool:
        """Atomically mark a story as in-flight. Returns False if already taken."""

    @abstractmethod
    def mark_completed(self, story_key: str, pr_url: str) -> None: ...

    @abstractmethod
    def mark_failed(self, story_key: str, error: str) -> None: ...

    @abstractmethod
    def list_recent(self, limit: int = 50) -> list[dict]: ...
