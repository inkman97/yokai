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

ACTIVITIES_URL = (
    "https://code.example.com/rest/api/1.0/projects/MYPROJ"
    "/repos/my-repo/pull-requests/10/activities"
)

TASKS_URL = (
    "https://code.example.com/rest/api/1.0/projects/MYPROJ"
    "/repos/my-repo/pull-requests/10/tasks"
)


def _stub_empty_tasks():
    responses.add(responses.GET, TASKS_URL, json={"values": []}, status=200)


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
                    {"id": 10, "title": "PR 1", "fromRef": {"displayId": "branch-a"}, "toRef": {"displayId": "master"}, "links": {"self": [{"href": ""}]}},
                    {"id": 11, "title": "PR 2", "fromRef": {"displayId": "branch-b"}, "toRef": {"displayId": "master"}, "links": {"self": [{"href": ""}]}},
                ],
            },
            status=200,
        )
        hosting = make_hosting()
        assert len(hosting.find_pull_requests(REPO, "")) == 2

    @responses.activate
    def test_returns_empty_on_http_error(self):
        responses.add(responses.GET, "https://code.example.com/rest/api/1.0/projects/MYPROJ/repos/my-repo/pull-requests", status=500)
        hosting = make_hosting()
        assert hosting.find_pull_requests(REPO, "feature/x") == []


class TestGetPrComments:
    @responses.activate
    def test_returns_open_comments_with_anchor(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "Francesco"}, "text": "Move validation to service", "severity": "NORMAL", "state": "OPEN", "createdDate": "1713600000000"}, "commentAnchor": {"path": "src/Controller.java", "line": 72}}], "isLastPage": True}, status=200)
        _stub_empty_tasks()
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 1
        assert comments[0].author == "Francesco"
        assert comments[0].file_path == "src/Controller.java"
        assert comments[0].line == 72
        assert comments[0].state == "OPEN"

    @responses.activate
    def test_skips_resolved_comments(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "Human"}, "text": "Already fixed", "state": "RESOLVED"}}, {"action": "COMMENTED", "comment": {"id": 101, "author": {"displayName": "Human"}, "text": "Still open", "state": "OPEN"}}], "isLastPage": True}, status=200)
        _stub_empty_tasks()
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 1
        assert comments[0].text == "Still open"

    @responses.activate
    def test_skips_non_comment_activities(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "APPROVED"}, {"action": "MERGED"}, {"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "Human"}, "text": "Real comment"}}], "isLastPage": True}, status=200)
        _stub_empty_tasks()
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 1
        assert comments[0].text == "Real comment"

    @responses.activate
    def test_skips_yokai_bot_comments(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "yokai-bot"}, "text": "Generated by yokai"}}, {"action": "COMMENTED", "comment": {"id": 101, "author": {"displayName": "Human"}, "text": "Please fix"}}], "isLastPage": True}, status=200)
        _stub_empty_tasks()
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 1
        assert comments[0].author == "Human"

    @responses.activate
    def test_paginates_through_activities(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "Page1"}, "text": "Comment 1"}}], "isLastPage": False, "nextPageStart": 100}, status=200)
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 101, "author": {"displayName": "Page2"}, "text": "Comment 2"}}], "isLastPage": True}, status=200)
        _stub_empty_tasks()
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 2

    @responses.activate
    def test_includes_open_tasks(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [], "isLastPage": True}, status=200)
        responses.add(responses.GET, TASKS_URL, json={"values": [{"id": 500, "text": "Add unit tests", "state": "OPEN", "author": {"displayName": "Reviewer"}, "anchor": {"path": "src/Service.java", "line": 10}}, {"id": 501, "text": "Already done", "state": "RESOLVED", "author": {"displayName": "Reviewer"}}]}, status=200)
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 1
        assert comments[0].text == "[TASK] Add unit tests"
        assert comments[0].severity == "BLOCKER"
        assert comments[0].file_path == "src/Service.java"

    @responses.activate
    def test_combines_comments_and_tasks(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "Human"}, "text": "Fix this", "state": "OPEN"}}], "isLastPage": True}, status=200)
        responses.add(responses.GET, TASKS_URL, json={"values": [{"id": 500, "text": "Add tests", "state": "OPEN", "author": {"displayName": "Reviewer"}}]}, status=200)
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 2
        texts = [c.text for c in comments]
        assert "Fix this" in texts
        assert "[TASK] Add tests" in texts

    @responses.activate
    def test_tasks_failure_is_non_fatal(self):
        responses.add(responses.GET, ACTIVITIES_URL, json={"values": [{"action": "COMMENTED", "comment": {"id": 100, "author": {"displayName": "Human"}, "text": "A comment"}}], "isLastPage": True}, status=200)
        responses.add(responses.GET, TASKS_URL, status=500)
        hosting = make_hosting()
        comments = hosting.get_pr_comments(REPO, "10")
        assert len(comments) == 1

    @responses.activate
    def test_returns_empty_on_activities_http_error(self):
        responses.add(responses.GET, ACTIVITIES_URL, status=500)
        _stub_empty_tasks()
        hosting = make_hosting()
        assert hosting.get_pr_comments(REPO, "10") == []
