"""Unit tests for Bitbucket Data Center adapter.

Git operations against a real local repo are tested in integration tests.
Here we test:
- resolve_repo URL construction (case sensitivity logic)
- open_pull_request via mocked HTTP
- _parse_show_stat as a pure function
"""

import pytest
import responses

from yokai.adapters.bitbucket_dc.hosting import (
    BitbucketDataCenterHosting,
    BitbucketDataCenterSettings,
)
from yokai.core.exceptions import RepoHostingError
from yokai.core.models import RepoLocation


def make_hosting():
    return BitbucketDataCenterHosting(
        BitbucketDataCenterSettings(
            base_url="https://code.example.com",
            project_key="myproj",
            username="testuser",
            token="bb-token",
        )
    )


class TestResolveRepo:
    def test_clone_url_uses_lowercase_project_key(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert "/scm/myproj/my-repo.git" in loc.clone_url

    def test_web_url_uses_uppercase_project_key(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert "/projects/MYPROJ/repos/my-repo" in loc.web_url

    def test_returns_default_branch_from_settings(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("my-repo")
        assert loc.default_branch == "master"

    def test_slug_preserved(self):
        hosting = make_hosting()
        loc = hosting.resolve_repo("nova-masterdata-editor-commons")
        assert loc.slug == "nova-masterdata-editor-commons"


class TestOpenPullRequest:
    @responses.activate
    def test_creates_pr_and_returns_object(self):
        responses.add(
            responses.POST,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            json={
                "id": 42,
                "links": {
                    "self": [
                        {"href": "https://code.example.com/pr/42"}
                    ]
                },
            },
            status=201,
        )
        hosting = make_hosting()
        repo = RepoLocation(
            slug="my-repo",
            project_key="myproj",
            default_branch="master",
        )
        pr = hosting.open_pull_request(
            repo=repo,
            source_branch="feature/test",
            target_branch="master",
            title="My PR",
            description="A description",
        )
        assert pr.id == "42"
        assert pr.url == "https://code.example.com/pr/42"
        assert pr.title == "My PR"

    @responses.activate
    def test_uses_uppercase_project_in_api_path(self):
        responses.add(
            responses.POST,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            json={"id": 1, "links": {"self": [{"href": "x"}]}},
            status=201,
        )
        hosting = make_hosting()
        repo = RepoLocation(
            slug="my-repo", project_key="myproj", default_branch="master"
        )
        hosting.open_pull_request(
            repo=repo,
            source_branch="b",
            target_branch="master",
            title="t",
            description="d",
        )
        url = responses.calls[0].request.url
        assert "/projects/MYPROJ/" in url

    @responses.activate
    def test_payload_contains_source_and_target(self):
        responses.add(
            responses.POST,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            json={"id": 1, "links": {"self": [{"href": "x"}]}},
            status=201,
        )
        hosting = make_hosting()
        repo = RepoLocation(
            slug="my-repo", project_key="myproj", default_branch="master"
        )
        hosting.open_pull_request(
            repo=repo,
            source_branch="feature/x",
            target_branch="master",
            title="t",
            description="d",
        )
        body = responses.calls[0].request.body
        assert b"refs/heads/feature/x" in body
        assert b"refs/heads/master" in body

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(
            responses.POST,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            status=403,
            json={"errors": [{"message": "forbidden"}]},
        )
        hosting = make_hosting()
        repo = RepoLocation(
            slug="my-repo", project_key="myproj", default_branch="master"
        )
        with pytest.raises(RepoHostingError, match="Failed to create pull request"):
            hosting.open_pull_request(
                repo=repo,
                source_branch="b",
                target_branch="master",
                title="t",
                description="d",
            )


class TestParseShowStat:
    def test_parses_typical_output(self):
        stat = " 8 files changed, 150 insertions(+), 12 deletions(-)\n"
        files, ins, dels = BitbucketDataCenterHosting._parse_show_stat(stat)
        assert files == 8
        assert ins == 150
        assert dels == 12

    def test_parses_single_file_no_deletions(self):
        stat = " 1 file changed, 5 insertions(+)\n"
        files, ins, dels = BitbucketDataCenterHosting._parse_show_stat(stat)
        assert files == 1
        assert ins == 5
        assert dels == 0

    def test_parses_only_deletions(self):
        stat = " 2 files changed, 8 deletions(-)\n"
        files, ins, dels = BitbucketDataCenterHosting._parse_show_stat(stat)
        assert files == 2
        assert ins == 0
        assert dels == 8

    def test_returns_zeros_on_empty_input(self):
        files, ins, dels = BitbucketDataCenterHosting._parse_show_stat("")
        assert (files, ins, dels) == (0, 0, 0)
