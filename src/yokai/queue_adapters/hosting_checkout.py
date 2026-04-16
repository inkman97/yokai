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
from yokai.queue.agent import CheckoutInfo, RepoCheckout
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

    def cleanup(self, job: Job) -> None:
        # The legacy Pipeline does not clean up either: workspaces are
        # reused across runs and only refreshed on next clone_or_update.
        # No-op here keeps behaviour parity.
        return
