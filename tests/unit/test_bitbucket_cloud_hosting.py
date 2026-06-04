"""Unit tests for Bitbucket Cloud adapter.

Git operations against a real local repo are tested in integration tests.
Here we test:
- resolve_repo URL construction (workspace-based, with embedded auth)
- open_pull_request via mocked HTTP using the v2 schema
- find_pull_requests via mocked HTTP
- get_pr_comments via mocked HTTP
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
        loc = hosting.resolve_repo("TEST-cloud-svc")
        assert loc.slug == "TEST-cloud-svc"


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


class TestFindPullRequests:
    @responses.activate
    def test_returns_matching_prs(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            json={
                "values": [
                    {
                        "id": 10,
                        "title": "AI PR",
                        "description": "auto",
                        "source": {"branch": {"name": "feature/TEST-1-ai"}},
                        "destination": {"branch": {"name": "main"}},
                        "links": {"html": {"href": "https://bb.example/pr/10"}},
                    },
                    {
                        "id": 11,
                        "title": "Other PR",
                        "description": "",
                        "source": {"branch": {"name": "feature/manual"}},
                        "destination": {"branch": {"name": "main"}},
                        "links": {"html": {"href": "https://bb.example/pr/11"}},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        prs = hosting.find_pull_requests(loc, "feature/TEST-1-ai")

        assert len(prs) == 1
        assert prs[0].id == "10"
        assert prs[0].source_branch == "feature/TEST-1-ai"
        assert prs[0].target_branch == "main"

    @responses.activate
    def test_returns_all_when_branch_is_empty(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            json={
                "values": [
                    {
                        "id": 10,
                        "title": "PR 1",
                        "source": {"branch": {"name": "branch-a"}},
                        "destination": {"branch": {"name": "main"}},
                        "links": {"html": {"href": ""}},
                    },
                    {
                        "id": 11,
                        "title": "PR 2",
                        "source": {"branch": {"name": "branch-b"}},
                        "destination": {"branch": {"name": "main"}},
                        "links": {"html": {"href": ""}},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        prs = hosting.find_pull_requests(loc, "")

        assert len(prs) == 2

    @responses.activate
    def test_returns_empty_on_http_error(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests",
            status=500,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        prs = hosting.find_pull_requests(loc, "feature/x")

        assert prs == []


class TestGetPrComments:
    @responses.activate
    def test_returns_comments_with_inline_info(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            json={
                "values": [
                    {
                        "id": 100,
                        "user": {"display_name": "Francesco"},
                        "content": {"raw": "Fix this method"},
                        "inline": {"path": "src/Main.java", "to": 42},
                        "created_on": "2026-04-20T10:00:00Z",
                    },
                    {
                        "id": 101,
                        "user": {"display_name": "Reviewer"},
                        "content": {"raw": "Looks good otherwise"},
                        "created_on": "2026-04-20T11:00:00Z",
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert len(comments) == 2
        assert comments[0].author == "Francesco"
        assert comments[0].text == "Fix this method"
        assert comments[0].file_path == "src/Main.java"
        assert comments[0].line == 42
        assert comments[1].file_path is None
        assert comments[1].line is None

    @responses.activate
    def test_skips_yokai_bot_comments(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            json={
                "values": [
                    {
                        "id": 100,
                        "user": {"display_name": "yokai-bot"},
                        "content": {"raw": "Generated by yokai"},
                    },
                    {
                        "id": 101,
                        "user": {"display_name": "Human Reviewer"},
                        "content": {"raw": "Please fix this"},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert len(comments) == 1
        assert comments[0].author == "Human Reviewer"

    @responses.activate
    def test_skips_empty_comments(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            json={
                "values": [
                    {
                        "id": 100,
                        "user": {"display_name": "Someone"},
                        "content": {"raw": ""},
                    },
                    {
                        "id": 101,
                        "user": {"display_name": "Someone"},
                        "content": {"raw": "Real comment"},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert len(comments) == 1
        assert comments[0].text == "Real comment"

    @responses.activate
    def test_handles_pagination(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            json={
                "values": [
                    {
                        "id": 100,
                        "user": {"display_name": "Page1"},
                        "content": {"raw": "Comment page 1"},
                    },
                ],
                "next": "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments?page=2",
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments?page=2",
            json={
                "values": [
                    {
                        "id": 101,
                        "user": {"display_name": "Page2"},
                        "content": {"raw": "Comment page 2"},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert len(comments) == 2
        assert comments[0].author == "Page1"
        assert comments[1].author == "Page2"

    @responses.activate
    def test_returns_empty_on_http_error(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            status=500,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert comments == []

    @responses.activate
    def test_skips_resolved_comments(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            json={
                "values": [
                    {
                        "id": 100,
                        "user": {"display_name": "Reviewer"},
                        "content": {"raw": "Already addressed"},
                        "resolution": {"type": "RESOLVED"},
                    },
                    {
                        "id": 101,
                        "user": {"display_name": "Reviewer"},
                        "content": {"raw": "Still needs work"},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert len(comments) == 1
        assert comments[0].text == "Still needs work"

    @responses.activate
    def test_skips_deleted_comments(self):
        responses.add(
            responses.GET,
            "https://api.bitbucket.org/2.0/repositories/acme-team/my-repo/pullrequests/10/comments",
            json={
                "values": [
                    {
                        "id": 100,
                        "user": {"display_name": "Reviewer"},
                        "content": {"raw": "Deleted comment"},
                        "deleted": True,
                    },
                    {
                        "id": 101,
                        "user": {"display_name": "Reviewer"},
                        "content": {"raw": "Active comment"},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        comments = hosting.get_pr_comments(loc, "10")

        assert len(comments) == 1
        assert comments[0].text == "Active comment"


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
