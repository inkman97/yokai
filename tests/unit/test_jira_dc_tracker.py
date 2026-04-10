"""Unit tests for Jira Data Center adapter, with HTTP responses mocked."""

import pytest
import responses

from yokai.adapters.jira_dc.tracker import (
    JiraDataCenterSettings,
    JiraDataCenterTracker,
)
from yokai.core.exceptions import IssueTrackerError


def make_tracker():
    return JiraDataCenterTracker(
        JiraDataCenterSettings(
            base_url="https://jira.example.com",
            project="NOVA",
            username="testuser",
            token="test-token",
        )
    )


JIRA_SEARCH_RESPONSE = {
    "issues": [
        {
            "key": "NOVA-101",
            "fields": {
                "summary": "Improve error handling",
                "description": "Return 404 instead of 500",
                "components": [{"name": "EMU-BE"}],
                "labels": ["ai-pipeline", "tech-debt"],
            },
        },
        {
            "key": "NOVA-102",
            "fields": {
                "summary": "Add date filter",
                "description": None,
                "components": [{"name": "EMU-FE"}],
                "labels": ["ai-pipeline"],
            },
        },
    ]
}


class TestSearchPendingStories:
    @responses.activate
    def test_returns_parsed_stories(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json=JIRA_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_tracker()
        stories = tracker.search_pending_stories()

        assert len(stories) == 2
        assert stories[0].key == "NOVA-101"
        assert stories[0].title == "Improve error handling"
        assert stories[0].components == ["EMU-BE"]
        assert "ai-pipeline" in stories[0].labels
        assert stories[0].url == "https://jira.example.com/browse/NOVA-101"

    @responses.activate
    def test_handles_null_description(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json=JIRA_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_tracker()
        stories = tracker.search_pending_stories()
        assert stories[1].description == ""

    @responses.activate
    def test_returns_empty_list_when_no_issues(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_tracker()
        assert tracker.search_pending_stories() == []

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            status=401,
            json={"errorMessages": ["Unauthorized"]},
        )
        tracker = make_tracker()
        with pytest.raises(IssueTrackerError, match="search failed"):
            tracker.search_pending_stories()

    @responses.activate
    def test_includes_bearer_token_in_headers(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_tracker()
        tracker.search_pending_stories()
        assert (
            responses.calls[0].request.headers["Authorization"]
            == "Bearer test-token"
        )

    @responses.activate
    def test_jql_filters_by_project_status_label(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_tracker()
        tracker.search_pending_stories()
        url = responses.calls[0].request.url
        assert "project+%3D+NOVA" in url or "project%20%3D%20NOVA" in url
        assert "ai-pipeline" in url
        assert "ai-processing" in url


class TestAddComment:
    @responses.activate
    def test_posts_comment_body(self):
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/NOVA-101/comment",
            json={"id": "1"},
            status=201,
        )
        tracker = make_tracker()
        tracker.add_comment("NOVA-101", "test comment")
        assert len(responses.calls) == 1

    @responses.activate
    def test_raises_on_failure(self):
        responses.add(
            responses.POST,
            "https://jira.example.com/rest/api/2/issue/NOVA-101/comment",
            status=403,
            json={"error": "forbidden"},
        )
        tracker = make_tracker()
        with pytest.raises(IssueTrackerError, match="Failed to add comment"):
            tracker.add_comment("NOVA-101", "test")


class TestMarkInProgress:
    @responses.activate
    def test_adds_processing_label(self):
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/NOVA-101",
            status=204,
        )
        tracker = make_tracker()
        tracker.mark_in_progress("NOVA-101")
        body = responses.calls[0].request.body
        assert b"ai-processing" in body


class TestGetStoryUrl:
    def test_builds_browse_url(self):
        tracker = make_tracker()
        url = tracker.get_story_url("NOVA-42")
        assert url == "https://jira.example.com/browse/NOVA-42"
