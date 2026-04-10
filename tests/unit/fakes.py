"""Fake adapters used by orchestrator tests.

These are deliberately simple in-memory implementations of the framework
interfaces. They let us exercise the real Pipeline class without any
network, subprocess, or filesystem side effects.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from yokai.core.interfaces import (
    CodingAgent,
    IssueTracker,
    RepoHosting,
)
from yokai.core.models import (
    AgentResult,
    Branch,
    CommitInfo,
    FileChange,
    PullRequest,
    RepoLocation,
    Story,
)


@dataclass
class FakeTracker(IssueTracker):
    stories: list[Story] = field(default_factory=list)
    comments: dict[str, list[str]] = field(default_factory=dict)
    in_progress: set[str] = field(default_factory=set)
    failed: dict[str, str] = field(default_factory=dict)
    base_url: str = "https://fake-tracker"
    _search_call_count: int = 0

    def search_pending_stories(self) -> list[Story]:
        self._search_call_count += 1
        return [
            s
            for s in self.stories
            if s.key not in self.in_progress and s.key not in self.failed
        ]

    def mark_in_progress(self, story_key: str) -> None:
        self.in_progress.add(story_key)

    def mark_failed(self, story_key: str, reason: str) -> None:
        self.failed[story_key] = reason

    def add_comment(self, story_key: str, body: str) -> None:
        self.comments.setdefault(story_key, []).append(body)

    def get_story_url(self, story_key: str) -> str:
        return f"{self.base_url}/browse/{story_key}"


@dataclass
class FakeHosting(RepoHosting):
    default_branch: str = "master"
    clones: list[str] = field(default_factory=list)
    branches_created: list[str] = field(default_factory=list)
    commits_made: list[str] = field(default_factory=list)
    pushes: list[str] = field(default_factory=list)
    prs_opened: list[PullRequest] = field(default_factory=list)
    fail_clone_for: set[str] = field(default_factory=set)
    fake_changed_files: list[FileChange] = field(
        default_factory=lambda: [
            FileChange(path="src/main/Foo.java", added=10, removed=2),
            FileChange(path="src/test/FooTest.java", added=15, removed=0),
        ]
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _pr_counter: int = 0

    def resolve_repo(self, slug: str) -> RepoLocation:
        return RepoLocation(
            slug=slug,
            project_key="fake",
            default_branch=self.default_branch,
            clone_url=f"https://fake/{slug}.git",
            web_url=f"https://fake/{slug}",
        )

    def clone_or_update(self, repo: RepoLocation, workspace: Path) -> Path:
        if repo.slug in self.fail_clone_for:
            from yokai.core.exceptions import GitOperationError
            raise GitOperationError(f"fake clone failure for {repo.slug}")
        with self._lock:
            self.clones.append(repo.slug)
        path = workspace / repo.slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def create_branch(self, repo_path: Path, branch: Branch) -> None:
        with self._lock:
            self.branches_created.append(branch.name)

    def commit_changes(self, repo_path: Path, message: str) -> CommitInfo | None:
        with self._lock:
            self.commits_made.append(message)
        return CommitInfo(
            sha="0" * 40,
            short_sha="abc1234",
            message=message,
            files_changed=2,
            insertions=25,
            deletions=2,
        )

    def push_branch(self, repo_path: Path, branch_name: str) -> None:
        with self._lock:
            self.pushes.append(branch_name)

    def get_changed_files(self, repo_path: Path, base_branch: str) -> list[FileChange]:
        return list(self.fake_changed_files)

    def open_pull_request(
        self,
        repo: RepoLocation,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> PullRequest:
        with self._lock:
            self._pr_counter += 1
            pr_id = str(self._pr_counter)
        pr = PullRequest(
            id=pr_id,
            url=f"https://fake/pr/{pr_id}",
            title=title,
            source_branch=source_branch,
            target_branch=target_branch,
            description=description,
        )
        with self._lock:
            self.prs_opened.append(pr)
        return pr


@dataclass
class FakeAgent(CodingAgent):
    delay_seconds: float = 0.0
    fail: bool = False
    output: str = "Fake agent output: did some work"
    runs: list[Path] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, repo_path: Path, prompt: str) -> AgentResult:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        with self._lock:
            self.runs.append(repo_path)
        if self.fail:
            from yokai.core.exceptions import AgentExecutionError
            raise AgentExecutionError("fake agent failure")
        return AgentResult(
            success=True,
            output=self.output,
            duration_seconds=self.delay_seconds,
        )
