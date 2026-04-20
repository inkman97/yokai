"""Tests for Bitbucket DC rework methods (find_pull_requests, get_pr_comments)."""

import responses

from yokai.adapters.bitbucket_dc.hosting import (
    BitbucketDataCenterHosting,
    BitbucketDataCenterSettings,
)
from yokai.core.models import RepoLocation


def make_hosting():
    return BitbucketDataCenterHosting(
        BitbucketDataCenterSettings(
            base_url="https://code.example.com",
            namespace="myproj",
            account="testuser",
            token="bb-token",
        )
    )


REPO = RepoLocation(slug="my-repo", namespace="myproj", default_branch="master")


class TestFindPullRequests:
    @responses.activate
    def test_returns_matching_prs(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            json={
                "values": [
                    {
                        "id": 10,
                        "title": "[AI] NOVA-101",
                        "description": "auto",
                        "fromRef": {"displayId": "feature/NOVA-101-ai"},
                        "toRef": {"displayId": "master"},
                        "links": {"self": [{"href": "https://code.example.com/pr/10"}]},
                    },
                    {
                        "id": 11,
                        "title": "Manual PR",
                        "description": "",
                        "fromRef": {"displayId": "feature/manual"},
                        "toRef": {"displayId": "master"},
                        "links": {"self": [{"href": "https://code.example.com/pr/11"}]},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        prs = hosting.find_pull_requests(REPO, "feature/NOVA-101-ai")

        assert len(prs) == 1
        assert prs[0].id == "10"
        assert prs[0].source_branch == "feature/NOVA-101-ai"

    @responses.activate
    def test_returns_all_when_branch_is_empty(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            json={
                "values": [
                    {
                        "id": 10,
                        "title": "PR 1",
                        "fromRef": {"displayId": "branch-a"},
                        "toRef": {"displayId": "master"},
                        "links": {"self": [{"href": ""}]},
                    },
                    {
                        "id": 11,
                        "title": "PR 2",
                        "fromRef": {"displayId": "branch-b"},
                        "toRef": {"displayId": "master"},
                        "links": {"self": [{"href": ""}]},
                    },
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        prs = hosting.find_pull_requests(REPO, "")

        assert len(prs) == 2

    @responses.activate
    def test_returns_empty_on_http_error(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests",
            status=500,
        )
        hosting = make_hosting()
        prs = hosting.find_pull_requests(REPO, "feature/x")

        assert prs == []


class TestGetPrComments:
    @responses.activate
    def test_returns_comments_with_anchor(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests/10/activities",
            json={
                "values": [
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 100,
                            "author": {"displayName": "Francesco"},
                            "text": "Move validation to service",
                            "severity": "NORMAL",
                            "createdDate": "1713600000000",
                        },
                        "commentAnchor": {
                            "path": "src/Controller.java",
                            "line": 72,
                        },
                    },
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 101,
                            "author": {"displayName": "Reviewer"},
                            "text": "General comment",
                            "severity": "NORMAL",
                        },
                    },
                ],
                "isLastPage": True,
            },
            status=200,
        )
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")

        assert len(comments) == 2
        assert comments[0].author == "Francesco"
        assert comments[0].file_path == "src/Controller.java"
        assert comments[0].line == 72
        assert comments[1].file_path is None

    @responses.activate
    def test_skips_non_comment_activities(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests/10/activities",
            json={
                "values": [
                    {"action": "APPROVED"},
                    {"action": "MERGED"},
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 100,
                            "author": {"displayName": "Human"},
                            "text": "Real comment",
                        },
                    },
                ],
                "isLastPage": True,
            },
            status=200,
        )
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")

        assert len(comments) == 1
        assert comments[0].text == "Real comment"

    @responses.activate
    def test_skips_yokai_bot_comments(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests/10/activities",
            json={
                "values": [
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 100,
                            "author": {"displayName": "yokai-bot"},
                            "text": "Generated by yokai",
                        },
                    },
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 101,
                            "author": {"displayName": "Human"},
                            "text": "Please fix",
                        },
                    },
                ],
                "isLastPage": True,
            },
            status=200,
        )
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")

        assert len(comments) == 1
        assert comments[0].author == "Human"

    @responses.activate
    def test_paginates_through_activities(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests/10/activities",
            json={
                "values": [
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 100,
                            "author": {"displayName": "Page1"},
                            "text": "Comment 1",
                        },
                    },
                ],
                "isLastPage": False,
                "nextPageStart": 100,
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests/10/activities",
            json={
                "values": [
                    {
                        "action": "COMMENTED",
                        "comment": {
                            "id": 101,
                            "author": {"displayName": "Page2"},
                            "text": "Comment 2",
                        },
                    },
                ],
                "isLastPage": True,
            },
            status=200,
        )
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")

        assert len(comments) == 2
        assert comments[0].author == "Page1"
        assert comments[1].author == "Page2"

    @responses.activate
    def test_returns_empty_on_http_error(self):
        responses.add(
            responses.GET,
            "https://code.example.com/rest/api/1.0/projects/MYPROJ"
            "/repos/my-repo/pull-requests/10/activities",
            status=500,
        )
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")

        assert comments == []
