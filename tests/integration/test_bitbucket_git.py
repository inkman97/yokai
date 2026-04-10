"""Integration tests for BitbucketDataCenterHosting git operations.

These tests do not need a real Bitbucket server. They create a local
bare git repository (acting as the "remote") and exercise clone, branch,
commit, push, and diff stat parsing against it. This validates the
git plumbing of the adapter without any network access.

PR creation is not covered here because it goes through the Bitbucket
REST API. That part is unit-tested with HTTP mocking elsewhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from yokai.adapters.bitbucket_dc.hosting import (
    BitbucketDataCenterHosting,
    BitbucketDataCenterSettings,
)
from yokai.core.models import Branch, RepoLocation


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed"
)


def _run(cmd, cwd):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    """Create a bare git repository to act as the remote."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _run(["git", "init", "--bare", "--initial-branch=master"], cwd=bare)

    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init", "--initial-branch=master"], cwd=seed)
    _run(["git", "config", "user.email", "test@example.com"], cwd=seed)
    _run(["git", "config", "user.name", "Test"], cwd=seed)
    (seed / "README.md").write_text("# initial\n")
    _run(["git", "add", "-A"], cwd=seed)
    _run(["git", "commit", "-m", "initial"], cwd=seed)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=seed)
    _run(["git", "push", "-u", "origin", "master"], cwd=seed)

    return bare


@pytest.fixture
def hosting_with_local_remote(remote_repo: Path) -> BitbucketDataCenterHosting:
    """A hosting instance whose 'clone url' points at our bare local repo."""
    settings = BitbucketDataCenterSettings(
        base_url="https://example.com",
        project_key="testproj",
        username="tester",
        token="ignored-locally",
    )
    hosting = BitbucketDataCenterHosting(settings)

    original_resolve = hosting.resolve_repo

    def patched_resolve(slug: str) -> RepoLocation:
        loc = original_resolve(slug)
        loc.clone_url = str(remote_repo)
        return loc

    hosting.resolve_repo = patched_resolve  # type: ignore[method-assign]
    return hosting


def _global_git_identity(repo_path: Path):
    _run(
        ["git", "config", "user.email", "ci@example.com"], cwd=repo_path
    )
    _run(["git", "config", "user.name", "CI"], cwd=repo_path)


class TestCloneAndUpdate:
    def test_clone_creates_working_tree(
        self, hosting_with_local_remote, tmp_path: Path
    ):
        repo = hosting_with_local_remote.resolve_repo("test-repo")
        workspace = tmp_path / "workspace"
        repo_path = hosting_with_local_remote.clone_or_update(repo, workspace)

        assert repo_path.exists()
        assert (repo_path / "README.md").exists()
        assert (repo_path / ".git").exists()

    def test_second_call_pulls_updates(
        self, hosting_with_local_remote, tmp_path: Path, remote_repo: Path
    ):
        repo = hosting_with_local_remote.resolve_repo("test-repo")
        workspace = tmp_path / "workspace"
        repo_path = hosting_with_local_remote.clone_or_update(repo, workspace)
        _global_git_identity(repo_path)

        seed2 = tmp_path / "seed2"
        seed2.mkdir()
        _run(["git", "clone", str(remote_repo), str(seed2 / "r")], cwd=seed2)
        seed2_repo = seed2 / "r"
        _global_git_identity(seed2_repo)
        (seed2_repo / "NEW.txt").write_text("hello\n")
        _run(["git", "add", "-A"], cwd=seed2_repo)
        _run(["git", "commit", "-m", "add file"], cwd=seed2_repo)
        _run(["git", "push", "origin", "master"], cwd=seed2_repo)

        hosting_with_local_remote.clone_or_update(repo, workspace)
        assert (repo_path / "NEW.txt").exists()


class TestBranchCommitPush:
    def test_full_create_commit_push_cycle(
        self, hosting_with_local_remote, tmp_path: Path, remote_repo: Path
    ):
        repo = hosting_with_local_remote.resolve_repo("test-repo")
        workspace = tmp_path / "workspace"
        repo_path = hosting_with_local_remote.clone_or_update(repo, workspace)
        _global_git_identity(repo_path)

        branch = Branch(name="feature/test-1", base="master")
        hosting_with_local_remote.create_branch(repo_path, branch)

        (repo_path / "added.txt").write_text("content\n")
        commit = hosting_with_local_remote.commit_changes(
            repo_path, "feat: add a file"
        )
        assert commit is not None
        assert commit.files_changed == 1
        assert commit.insertions == 1
        assert len(commit.short_sha) >= 7

        hosting_with_local_remote.push_branch(repo_path, branch.name)

        verify = tmp_path / "verify"
        verify.mkdir()
        _run(["git", "clone", str(remote_repo), str(verify / "r")], cwd=verify)
        branches = _run(["git", "branch", "-a"], cwd=verify / "r")
        assert "feature/test-1" in branches

    def test_commit_with_no_changes_returns_none(
        self, hosting_with_local_remote, tmp_path: Path
    ):
        repo = hosting_with_local_remote.resolve_repo("test-repo")
        workspace = tmp_path / "workspace"
        repo_path = hosting_with_local_remote.clone_or_update(repo, workspace)
        _global_git_identity(repo_path)
        hosting_with_local_remote.create_branch(
            repo_path, Branch(name="empty", base="master")
        )
        result = hosting_with_local_remote.commit_changes(
            repo_path, "should not happen"
        )
        assert result is None


class TestGetChangedFiles:
    def test_diff_against_base_returns_added_files(
        self, hosting_with_local_remote, tmp_path: Path
    ):
        repo = hosting_with_local_remote.resolve_repo("test-repo")
        workspace = tmp_path / "workspace"
        repo_path = hosting_with_local_remote.clone_or_update(repo, workspace)
        _global_git_identity(repo_path)

        hosting_with_local_remote.create_branch(
            repo_path, Branch(name="feature/diff-test", base="master")
        )
        (repo_path / "src").mkdir()
        (repo_path / "src" / "Foo.java").write_text("public class Foo {}\n")
        (repo_path / "src" / "Bar.java").write_text("public class Bar {}\n")
        hosting_with_local_remote.commit_changes(repo_path, "add files")

        changed = hosting_with_local_remote.get_changed_files(repo_path, "master")
        paths = sorted(f.path for f in changed)
        assert paths == ["src/Bar.java", "src/Foo.java"]
        for f in changed:
            assert f.added == 1
            assert f.removed == 0
