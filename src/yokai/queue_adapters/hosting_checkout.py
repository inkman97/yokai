"""Bridge from yokai.core.RepoHosting to yokai.queue.RepoCheckout.

The queue subsystem expects a single `prepare(job) -> CheckoutInfo` call
that produces a working tree on a fresh feature branch. The yokai
RepoHosting interface splits this into resolve_repo + clone_or_update +
create_branch. This adapter does the orchestration.

Branch name is rendered using the same `render_branch_name` helper used
by the legacy Pipeline, so naming conventions stay consistent across
modes.
"""

from __future__ import annotations

from pathlib import Path

from yokai.core.branch_naming import render_branch_name
from yokai.core.interfaces import RepoHosting
from yokai.core.models import Branch
from yokai.queue.agent import CheckoutInfo, CommitPushResult, RepoCheckout
from yokai.queue.models import Job


class HostingRepoCheckout(RepoCheckout):
    def __init__(
            self,
            hosting: RepoHosting,
            workspace_dir: Path,
            branch_pattern: str = "feature/{issue_key}-ai-{timestamp}",
    ) -> None:
        self._hosting = hosting
        self._workspace_dir = Path(workspace_dir).expanduser()
        self._branch_pattern = branch_pattern

    def prepare(self, job: Job) -> CheckoutInfo:
        repo = self._hosting.resolve_repo(job.repo_slug)
        repo_path = self._hosting.clone_or_update(repo, self._workspace_dir)
        branch_name = render_branch_name(
            self._branch_pattern,
            issue_key=job.story_key,
            title=job.payload.get("title", ""),
        )
        self._hosting.create_branch(
            repo_path, Branch(name=branch_name, base=repo.default_branch)
        )
        return CheckoutInfo(
            repo_path=repo_path,
            branch_name=branch_name,
            base_branch=repo.default_branch,
        )

    def commit_and_push(
            self,
            checkout_info: CheckoutInfo,
            message: str,
    ) -> CommitPushResult | None:
        repo_path = checkout_info.repo_path
        branch_name = checkout_info.branch_name

        commit = self._hosting.commit_changes(repo_path, message)
        if commit is None:
            return None

        self._hosting.push_branch(repo_path, branch_name)
        return CommitPushResult(
            commit_sha=commit.sha,
            short_sha=commit.short_sha,
            branch_name=branch_name,
            files_changed=commit.files_changed,
            insertions=commit.insertions,
            deletions=commit.deletions,
        )

    def cleanup(self, job: Job) -> None:
        return
