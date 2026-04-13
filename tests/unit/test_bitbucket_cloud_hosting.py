"""Unit tests for Bitbucket Cloud adapter.

Git operations against a real local repo are tested in integration tests.
Here we test:
- resolve_repo URL construction (workspace-based, with embedded auth)
- open_pull_request via mocked HTTP using the v2 schema
- _parse_show_stat as a pure function
- _redact_args helper to ensure credentials never leak into logs
"""

import base64

import pytest
import responses

from yokai.adapters.bitbucket_cloud.hosting import (
    BitbucketCloudHosting,
    BitbucketCloudSettings,
    _redact_args,
)
from yokai.core.exceptions import RepoHostingError


def make_hosting():
    return BitbucketCloudHosting(
        BitbucketCloudSettings(
            base_url="https://api.bitbucket.org/2.0",
            workspace="acme-team",
            account="alice",
            token="cloud-app-password",
        )
    )


class TestResolveRepo:
    def test_clone_url_includes_workspace_and_slug(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert "bitbucket.org/acme-team/my-repo.git" in loc.clone_url

    def test_clone_url_embeds_credentials(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert "alice:cloud-app-password@bitbucket.org" in loc.clone_url

    def test_web_url_points_to_bitbucket_org(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert loc.web_url == "https://bitbucket.org/acme-team/my-repo"

    def test_default_branch_is_main_for_cloud(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert loc.default_branch == "main"

    def test_namespace_field_holds_workspace(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert loc.namespace == "acme-team"

    def test_slug_preserved(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("nova-cloud-svc")
        assert loc.slug == "nova-cloud-svc"


class TestOpenPullRequest:
    @responses.activate
    def test_posts_v2_schema_with_source_destination(self):
        responses.add(
            responses.POST,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            json={
                "id": 42,
                "links": {
                    "html": {
                        "href": "https://bitbucket.org/acme-team/my-repo/pull-requests/42"
                    }
                },
            },
            status=201,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        pr = hosting.open_pull_request(
            repo=loc,
            source_branch="feature/x",
            target_branch="main",
            title="Add x",
            description="implements x",
        )

        assert pr.id == "42"
        assert pr.url == "https://bitbucket.org/acme-team/my-repo/pull-requests/42"
        assert pr.title == "Add x"
        assert pr.source_branch == "feature/x"
        assert pr.target_branch == "main"

        body = responses.calls[0].request.body
        assert b'"source"' in body
        assert b'"destination"' in body
        assert b'"branch"' in body
        assert b"feature/x" in body
        assert b"main" in body
        assert b"fromRef" not in body
        assert b"toRef" not in body

    @responses.activate
    def test_uses_basic_auth_with_account_and_token(self):
        responses.add(
            responses.POST,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            json={"id": 1, "links": {"html": {"href": ""}}},
            status=201,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        hosting.open_pull_request(
            repo=loc,
            source_branch="x",
            target_branch="main",
            title="t",
            description="d",
        )

        auth_header = responses.calls[0].request.headers["Authorization"]
        assert auth_header.startswith("Basic ")
        decoded = base64.b64decode(
            auth_header.split(" ", 1)[1]
        ).decode()
        assert decoded == "alice:cloud-app-password"

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(
            responses.POST,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            status=400,
            json={"error": {"message": "bad"}},
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        with pytest.raises(RepoHostingError, match="Failed to create pull request"):
            hosting.open_pull_request(
                repo=loc,
                source_branch="x",
                target_branch="main",
                title="t",
                description="d",
            )

    @responses.activate
    def test_close_source_branch_is_false_by_default(self):
        responses.add(
            responses.POST,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            json={"id": 1, "links": {"html": {"href": ""}}},
            status=201,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        hosting.open_pull_request(
            repo=loc,
            source_branch="x",
            target_branch="main",
            title="t",
            description="d",
        )
        body = responses.calls[0].request.body
        assert b'"close_source_branch": false' in body


class TestParseShowStat:
    def test_parses_typical_stat_line(self):
        stat = " 3 files changed, 42 insertions(+), 7 deletions(-)\n"
        files, ins, dels = BitbucketCloudHosting._parse_show_stat(stat)
        assert files == 3
        assert ins == 42
        assert dels == 7

    def test_handles_singular_file_changed(self):
        stat = " 1 file changed, 1 insertion(+)\n"
        files, ins, dels = BitbucketCloudHosting._parse_show_stat(stat)
        assert files == 1
        assert ins == 1
        assert dels == 0

    def test_returns_zeros_for_empty(self):
        files, ins, dels = BitbucketCloudHosting._parse_show_stat("")
        assert (files, ins, dels) == (0, 0, 0)


class TestRedactArgs:
    def test_redacts_userinfo_in_clone_url(self):
        args = ["clone", "https://alice:secret@bitbucket.org/acme/repo.git"]
        redacted = _redact_args(args)
        assert "secret" not in " ".join(redacted)
        assert "alice" not in " ".join(redacted)
        assert "***:***@bitbucket.org/acme/repo.git" in " ".join(redacted)

    def test_leaves_non_credential_args_untouched(self):
        args = ["status", "--porcelain"]
        assert _redact_args(args) == args

    def test_leaves_non_bitbucket_url_untouched(self):
        args = ["clone", "https://github.com/foo/bar.git"]
        assert _redact_args(args) == args

    def test_handles_url_without_userinfo(self):
        args = ["clone", "https://bitbucket.org/acme/repo.git"]
        redacted = _redact_args(args)
        assert redacted == args
