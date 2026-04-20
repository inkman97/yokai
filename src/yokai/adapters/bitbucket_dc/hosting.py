"""Bitbucket Data Center adapter implementing RepoHosting.

Handles two distinct concerns:
1. Local git operations (clone, branch, commit, push) via subprocess.
2. Pull request creation via REST API v1.0.

Authentication is done with HTTP access tokens. The token is passed to
git via http.extraheader to avoid placing it in the clone URL, which
breaks on tokens containing slash or plus characters.

Mapping to FrameworkConfig fields:
- RepoHostingConfig.base_url   -> https://your-bitbucket-dc-host
- RepoHostingConfig.namespace  -> Bitbucket DC project key (e.g. MVP_OEVP)
- RepoHostingConfig.account    -> username associated with the token
- RepoHostingConfig.token      -> HTTP Access Token (Bearer)

Note on case sensitivity: Bitbucket DC uses the project key in lowercase
for the /scm/ clone path but uppercase for the REST API path. The
adapter normalizes case as needed so the user can supply either form
in the namespace field.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

from yokai.core.exceptions import (
    GitOperationError,
    RepoHostingError,
)
from yokai.core.interfaces import RepoHosting
from yokai.core.logging_setup import get_logger
from yokai.core.models import (
    Branch,
    CommitInfo,
    FileChange,
    PRComment,
    PullRequest,
    RepoLocation,
)

log = get_logger("adapters.bitbucket_dc")


@dataclass
class BitbucketDataCenterSettings:
    base_url: str
    namespace: str
    account: str
    token: str
    default_branch: str = "master"
    request_timeout: int = 30


class BitbucketDataCenterHosting(RepoHosting):
    def __init__(self, settings: BitbucketDataCenterSettings):
        self._settings = settings

    def resolve_repo(self, slug: str) -> RepoLocation:
        s = self._settings
        clone_url = (
            f"{s.base_url}/scm/{s.namespace.lower()}/{slug}.git"
        )
        web_url = (
            f"{s.base_url}/projects/{s.namespace.upper()}/repos/{slug}/browse"
        )
        return RepoLocation(
            slug=slug,
            namespace=s.namespace,
            default_branch=s.default_branch,
            clone_url=clone_url,
            web_url=web_url,
        )

    def clone_or_update(self, repo: RepoLocation, workspace: Path) -> Path:
        repo_path = workspace / repo.slug
        workspace.mkdir(parents=True, exist_ok=True)

        if repo_path.exists():
            log.info(f"Repository already present, updating: {repo_path}")
            self._run_git(
                [
                    "config",
                    "--local",
                    "http.extraheader",
                    f"Authorization: Bearer {self._settings.token}",
                ],
                cwd=repo_path,
            )
            default_branch = self._detect_default_branch(repo_path) or repo.default_branch
            self._run_git(["fetch", "origin"], cwd=repo_path)
            self._run_git(["checkout", default_branch], cwd=repo_path, check=False)
            self._run_git(
                ["pull", "origin", default_branch], cwd=repo_path, check=False
            )
        else:
            log.info(f"Cloning repository: {repo.slug}")
            assert repo.clone_url is not None
            self._run_git(
                self._auth_args() + ["clone", repo.clone_url, str(repo_path)]
            )
            self._run_git(
                [
                    "config",
                    "--local",
                    "http.extraheader",
                    f"Authorization: Bearer {self._settings.token}",
                ],
                cwd=repo_path,
            )

        return repo_path

    def create_branch(self, repo_path: Path, branch: Branch) -> None:
        self._run_git(["checkout", "-b", branch.name], cwd=repo_path)

    def commit_changes(
        self, repo_path: Path, message: str
    ) -> CommitInfo | None:
        self._run_git(["add", "-A"], cwd=repo_path)
        status = self._run_git(["status", "--porcelain"], cwd=repo_path)
        if not status.strip():
            log.warning("No changes to commit")
            return None

        self._run_git(["commit", "-m", message], cwd=repo_path)
        sha = self._run_git(["rev-parse", "HEAD"], cwd=repo_path).strip()
        short_sha = self._run_git(
            ["rev-parse", "--short", "HEAD"], cwd=repo_path
        ).strip()
        stat = self._run_git(
            ["show", "--stat", "--format=", "HEAD"], cwd=repo_path
        )
        files_changed, insertions, deletions = self._parse_show_stat(stat)
        return CommitInfo(
            sha=sha,
            short_sha=short_sha,
            message=message,
            files_changed=files_changed,
            insertions=insertions,
            deletions=deletions,
        )

    def push_branch(self, repo_path: Path, branch_name: str) -> None:
        self._run_git(["push", "-u", "origin", branch_name], cwd=repo_path)

    def get_changed_files(
        self, repo_path: Path, base_branch: str
    ) -> list[FileChange]:
        try:
            output = self._run_git(
                ["diff", "--numstat", f"origin/{base_branch}...HEAD"],
                cwd=repo_path,
            )
        except GitOperationError:
            return []

        result: list[FileChange] = []
        for line in output.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                added, removed, path = parts
                result.append(
                    FileChange(
                        path=path,
                        added=int(added) if added.isdigit() else 0,
                        removed=int(removed) if removed.isdigit() else 0,
                    )
                )
        return result

    def open_pull_request(
        self,
        repo: RepoLocation,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> PullRequest:
        s = self._settings
        project_upper = s.namespace.upper()
        url = (
            f"{s.base_url}/rest/api/1.0/projects/{project_upper}"
            f"/repos/{repo.slug}/pull-requests"
        )
        payload = {
            "title": title,
            "description": description,
            "fromRef": {
                "id": f"refs/heads/{source_branch}",
                "repository": {
                    "slug": repo.slug,
                    "project": {"key": project_upper},
                },
            },
            "toRef": {
                "id": f"refs/heads/{target_branch}",
                "repository": {
                    "slug": repo.slug,
                    "project": {"key": project_upper},
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {s.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=s.request_timeout
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise RepoHostingError(
                f"Failed to create pull request on {repo.slug}: {e}"
            ) from e

        body = response.json()
        pr_id = str(body.get("id", "?"))
        pr_url = (
            body.get("links", {}).get("self", [{}])[0].get("href", "")
        )
        return PullRequest(
            id=pr_id,
            url=pr_url,
            title=title,
            source_branch=source_branch,
            target_branch=target_branch,
            description=description,
        )

    def find_pull_requests(
        self, repo: RepoLocation, branch_name: str
    ) -> list[PullRequest]:
        s = self._settings
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
        try:
            response = requests.get(
                url, headers=headers, params=params, timeout=s.request_timeout
            )
            response.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"Failed to find pull requests: {e}")
            return []

        prs = []
        for pr_data in response.json().get("values", []):
            from_ref = pr_data.get("fromRef", {})
            to_ref = pr_data.get("toRef", {})
            source = from_ref.get("displayId", "")
            if branch_name and source != branch_name:
                continue
            pr_url = (
                pr_data.get("links", {})
                .get("self", [{}])[0]
                .get("href", "")
            )
            prs.append(PullRequest(
                id=str(pr_data.get("id", "")),
                url=pr_url,
                title=pr_data.get("title", ""),
                source_branch=source,
                target_branch=to_ref.get("displayId", ""),
                description=pr_data.get("description", ""),
            ))
        return prs

    def get_pr_comments(
        self, repo: RepoLocation, pr_id: str
    ) -> list[PRComment]:
        s = self._settings
        project_upper = s.namespace.upper()
        url = (
            f"{s.base_url}/rest/api/1.0/projects/{project_upper}"
            f"/repos/{repo.slug}/pull-requests/{pr_id}/activities"
        )
        headers = {
            "Authorization": f"Bearer {s.token}",
            "Accept": "application/json",
        }
        comments: list[PRComment] = []
        start = 0
        while True:
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params={"start": start, "limit": 100},
                    timeout=s.request_timeout,
                )
                response.raise_for_status()
            except requests.RequestException as e:
                log.warning(f"Failed to get PR comments for PR {pr_id}: {e}")
                break

            body = response.json()
            for activity in body.get("values", []):
                if activity.get("action") != "COMMENTED":
                    continue
                comment = activity.get("comment", {})
                author = comment.get("author", {}).get("displayName", "unknown")
                text = comment.get("text", "")
                if not text or "yokai" in author.lower():
                    continue
                anchor = activity.get("commentAnchor", {})
                file_path = anchor.get("path") if anchor else None
                line = anchor.get("line") if anchor else None
                severity = comment.get("severity", "NORMAL")
                comments.append(PRComment(
                    id=str(comment.get("id", "")),
                    author=author,
                    text=text,
                    file_path=file_path,
                    line=line,
                    created_at=str(comment.get("createdDate", "")),
                    severity=severity,
                ))

            if body.get("isLastPage", True):
                break
            start = body.get("nextPageStart", start + 100)

        return comments

    def checkout_existing_branch(
        self, repo_path: Path, branch_name: str
    ) -> None:
        self._run_git(
            self._auth_args() + ["fetch", "origin", branch_name],
            cwd=repo_path,
        )
        self._run_git(
            ["checkout", branch_name],
            cwd=repo_path,
        )
        self._run_git(
            self._auth_args() + ["pull", "origin", branch_name],
            cwd=repo_path,
        )

    def _auth_args(self) -> list[str]:
        return [
            "-c",
            f"http.extraheader=Authorization: Bearer {self._settings.token}",
        ]

    def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> str:
        cmd = ["git"] + args
        log.info(f"git {' '.join(args)}")
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.stdout.strip():
            log.info(result.stdout.strip())
        if result.stderr.strip():
            log.warning(result.stderr.strip())
        if check and result.returncode != 0:
            raise GitOperationError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout

    def _detect_default_branch(self, repo_path: Path) -> str | None:
        try:
            output = self._run_git(
                ["symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo_path,
                check=False,
            )
            if output.strip():
                return output.strip().split("/")[-1]
        except GitOperationError:
            pass
        return None

    @staticmethod
    def _parse_show_stat(stat_output: str) -> tuple[int, int, int]:
        files_changed = insertions = deletions = 0
        for line in stat_output.splitlines():
            line = line.strip()
            if "file" in line and ("changed" in line or "changes" in line):
                tokens = line.split(",")
                for token in tokens:
                    token = token.strip()
                    if "file" in token:
                        files_changed = int(token.split()[0])
                    elif "insertion" in token:
                        insertions = int(token.split()[0])
                    elif "deletion" in token:
                        deletions = int(token.split()[0])
        return files_changed, insertions, deletions
