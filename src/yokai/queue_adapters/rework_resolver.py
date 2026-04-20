"""Bridge from RepoHosting to ReworkResolver.

Finds the existing PR for a story key by searching open PRs whose
branch name contains the story key. Then fetches the review comments
from that PR and serializes them into the job payload.
"""

from __future__ import annotations

from yokai.core.interfaces import RepoHosting
from yokai.core.logging_setup import get_logger
from yokai.core.models import PullRequest
from yokai.queue.coordinator import ReworkResolver

log = get_logger("queue_adapters.rework_resolver")


class HostingReworkResolver(ReworkResolver):
    def __init__(self, hosting: RepoHosting) -> None:
        self._hosting = hosting

    def resolve(
        self, story_key: str, repo_slug: str
    ) -> dict | None:
        try:
            repo = self._hosting.resolve_repo(repo_slug)
        except Exception as e:
            log.warning(f"Could not resolve repo {repo_slug}: {e}")
            return None

        try:
            all_prs = self._hosting.find_pull_requests(repo, "")
        except Exception:
            all_prs = []

        if not all_prs:
            try:
                all_prs = self._list_open_prs(repo)
            except Exception as e:
                log.warning(f"Could not list PRs for {repo_slug}: {e}")
                return None

        matching_pr = None
        for pr in all_prs:
            if story_key.lower() in pr.source_branch.lower():
                matching_pr = pr
                break

        if matching_pr is None:
            log.warning(
                f"No open PR found with branch containing {story_key} "
                f"in {repo_slug}"
            )
            return None

        log.info(
            f"Found PR #{matching_pr.id} for {story_key} "
            f"(branch={matching_pr.source_branch})"
        )

        try:
            comments = self._hosting.get_pr_comments(repo, matching_pr.id)
        except Exception as e:
            log.warning(f"Could not fetch PR comments: {e}")
            comments = []

        serialized_comments = [
            {
                "id": c.id,
                "author": c.author,
                "text": c.text,
                "file_path": c.file_path,
                "line": c.line,
                "severity": c.severity,
                "state": c.state,
                "created_at": c.created_at,
            }
            for c in comments
        ]

        log.info(
            f"Resolved rework for {story_key}: PR #{matching_pr.id}, "
            f"{len(serialized_comments)} review comments"
        )

        return {
            "branch_name": matching_pr.source_branch,
            "pr_id": matching_pr.id,
            "pr_url": matching_pr.url,
            "pr_comments": serialized_comments,
        }

    def _list_open_prs(self, repo) -> list[PullRequest]:
        import requests

        s = self._hosting._settings
        project_upper = s.namespace.upper()
        url = (
            f"{s.base_url}/rest/api/1.0/projects/{project_upper}"
            f"/repos/{repo.slug}/pull-requests"
        )
        headers = {
            "Authorization": f"Bearer {s.token}",
            "Accept": "application/json",
        }
        params = {"state": "OPEN", "limit": 100}

        response = requests.get(
            url, headers=headers, params=params, timeout=s.request_timeout
        )
        response.raise_for_status()

        prs = []
        for pr_data in response.json().get("values", []):
            from_ref = pr_data.get("fromRef", {})
            to_ref = pr_data.get("toRef", {})
            pr_url = (
                pr_data.get("links", {})
                .get("self", [{}])[0]
                .get("href", "")
            )
            prs.append(PullRequest(
                id=str(pr_data.get("id", "")),
                url=pr_url,
                title=pr_data.get("title", ""),
                source_branch=from_ref.get("displayId", ""),
                target_branch=to_ref.get("displayId", ""),
                description=pr_data.get("description", ""),
            ))
        return prs
