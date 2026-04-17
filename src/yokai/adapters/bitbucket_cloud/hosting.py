"""Bitbucket Cloud adapter implementing RepoHosting.

Targets the Bitbucket Cloud REST API v2 (api.bitbucket.org/2.0) and
uses HTTP Basic authentication with a Bitbucket account username and
an app password generated from the Bitbucket Cloud account settings.

Mapping to FrameworkConfig fields:
- RepoHostingConfig.base_url   -> https://api.bitbucket.org/2.0
- RepoHostingConfig.namespace  -> Bitbucket Cloud workspace slug
                                   (Cloud has no "project" concept)
- RepoHostingConfig.account    -> Bitbucket account username (the one
                                   visible at id.atlassian.com, not
                                   the email)
- RepoHostingConfig.token      -> Bitbucket Cloud app password

Local git operations use the same subprocess pattern as the DC adapter
but the clone URL embeds the username and app password as basic auth
userinfo, which is the pattern Bitbucket Cloud documents for HTTPS
clone with an app password.

Pull request creation uses the v2 schema with `source` and `destination`
objects rather than `fromRef`/`toRef`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

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
    PullRequest,
    RepoLocation,
)

log = get_logger("adapters.bitbucket_cloud")


@dataclass
class BitbucketCloudSettings:
    base_url: str
    workspace: str
    account: str
    token: str
    default_branch: str = "main"
    request_timeout: int = 30


class BitbucketCloudHosting(RepoHosting):
    def __init__(self, settings: BitbucketCloudSettings):
        self._settings = settings

    def resolve_repo(self, slug: str) -> RepoLocation:
        s = self._settings
        clone_url = (
            f"https://{s.account}:{s.token}"
            f"@bitbucket.org/{s.workspace}/{slug}.git"
        )
        web_url = f"https://bitbucket.org/{s.workspace}/{slug}"
        return RepoLocation(
            slug=slug,
            namespace=s.workspace,
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
            self._run_git(
                ["checkout", default_branch], cwd=repo_path, check=False
            )
            self._run_git(
                ["pull", "origin", default_branch],
                cwd=repo_path,
                check=False,
            )
        else:
            log.info(f"Cloning repository: {repo.slug}")
            assert repo.clone_url is not None
            self._run_git(["clone", repo.clone_url, str(repo_path)])

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
        self._run_git(
            ["push", "-u", "origin", branch_name], cwd=repo_path
        )

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
        url = (
            f"{s.base_url}/repositories/{s.workspace}/{repo.slug}"
            f"/pullrequests"
        )
        payload = {
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": target_branch}},
            "close_source_branch": False,
        }
        try:
            response = requests.post(
                url,
                auth=HTTPBasicAuth(s.account, s.token),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=s.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise RepoHostingError(
                f"Failed to create pull request on {repo.slug}: {e}"
            ) from e

        body = response.json()
        pr_id = str(body.get("id", "?"))
        pr_url = (
            body.get("links", {}).get("html", {}).get("href", "")
        )
        return PullRequest(
            id=pr_id,
            url=pr_url,
            title=title,
            source_branch=source_branch,
            target_branch=target_branch,
            description=description,
        )

    def _run_git(
        self,
        args: list[str],
        cwd: Path | None = None,
        check: bool = True,
    ) -> str:
        cmd = ["git"] + args
        log.info(f"git {' '.join(_redact_args(args))}")
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
                f"git {' '.join(_redact_args(args))} failed: "
                f"{result.stderr.strip()}"
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


def _redact_args(args: list[str]) -> list[str]:
    """Mask credentials embedded in clone URLs before logging.

    Bitbucket Cloud requires the app password to be embedded in the
    HTTPS clone URL as `https://user:password@host/...`. We never want
    that string to land in a log file, so any argument that looks like
    such a URL has its userinfo replaced with `***:***`.
    """
    redacted: list[str] = []
    for arg in args:
        if "@bitbucket.org" in arg and "://" in arg:
            scheme, rest = arg.split("://", 1)
            if "@" in rest:
                _, host_and_path = rest.split("@", 1)
                redacted.append(f"{scheme}://***:***@{host_and_path}")
                continue
        redacted.append(arg)
    return redacted
