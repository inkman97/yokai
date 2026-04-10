"""Domain models shared across the framework.

These are plain data containers with no behavior. They are deliberately
provider-agnostic: a Story does not know whether it came from Jira, Linear
or GitHub Issues; a RepoLocation does not know if it lives on Bitbucket,
GitHub or GitLab.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class StoryStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Story:
    """A unit of work to be implemented by the agent."""

    key: str
    title: str
    description: str
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def has_label(self, label: str) -> bool:
        return label in self.labels

    def has_component(self, component: str) -> bool:
        return component in self.components


@dataclass
class RepoLocation:
    """Identifies a repository on a hosting provider."""

    slug: str
    project_key: str
    default_branch: str = "master"
    clone_url: str | None = None
    web_url: str | None = None


@dataclass
class Branch:
    name: str
    base: str
    head_commit: str | None = None


@dataclass
class CommitInfo:
    sha: str
    short_sha: str
    message: str
    files_changed: int
    insertions: int
    deletions: int


@dataclass
class FileChange:
    path: str
    added: int
    removed: int

    @property
    def is_test(self) -> bool:
        return "/test/" in self.path or "/tests/" in self.path


@dataclass
class PullRequest:
    id: str
    url: str
    title: str
    source_branch: str
    target_branch: str
    description: str = ""


@dataclass
class AgentResult:
    """Outcome of running a coding agent on a repository."""

    success: bool
    output: str
    duration_seconds: float
    error: str | None = None


@dataclass
class StoryExecution:
    """Tracks a story through the pipeline. Used by storage and observers."""

    story_key: str
    status: StoryStatus
    repo_slug: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
