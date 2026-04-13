"""Unit tests for Jira Cloud adapter, with HTTP responses mocked."""

import base64

import pytest
import responses

from yokai.adapters.jira_cloud.tracker import (
    JiraCloudSettings,
    JiraCloudTracker,
    _adf_to_plain_text,
    _plain_text_to_adf,
)
from yokai.core.exceptions import IssueTrackerError


def make_tracker():
    return JiraCloudTracker(
        JiraCloudSettings(
            base_url="https://acme.atlassian.net",
            project="NOVA",
            email="alice@example.com",
            api_token="cloud-token",
        )
    )


def adf_paragraph(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


JIRA_CLOUD_SEARCH_RESPONSE = {
    "issues": [
        {
            "key": "NOVA-201",
            "fields": {
                "summary": "Improve cloud error handling",
                "description": adf_paragraph("Return 404 instead of 500"),
                "components": [{"name": "EMU-BE"}],
                "labels": ["ai-pipeline", "tech-debt"],
            },
        },
        {
            "key": "NOVA-202",
            "fields": {
                "summary": "Add filter",
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
            "https://acme.atlassian.net/rest/api/3/search",
            json=JIRA_CLOUD_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_tracker()
        stories = tracker.search_pending_stories()

        assert len(stories) == 2
        assert stories[0].key == "NOVA-201"
        assert stories[0].title == "Improve cloud error handling"
        assert stories[0].components == ["EMU-BE"]
        assert "ai-pipeline" in stories[0].labels
        assert (
                stories[0].url
                == "https://acme.atlassian.net/browse/NOVA-201"
        )

    @responses.activate
    def test_extracts_plain_text_from_adf_description(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            json=JIRA_CLOUD_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_tracker()
        stories = tracker.search_pending_stories()
        assert "Return 404 instead of 500" in stories[0].description

    @responses.activate
    def test_handles_null_description(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            json=JIRA_CLOUD_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_tracker()
        stories = tracker.search_pending_stories()
        assert stories[1].description == ""

    @responses.activate
    def test_returns_empty_list_when_no_issues(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_tracker()
        assert tracker.search_pending_stories() == []

    @responses.activate
    def test_raises_on_http_error(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            status=401,
            json={"errorMessages": ["Unauthorized"]},
        )
        tracker = make_tracker()
        with pytest.raises(IssueTrackerError, match="search failed"):
            tracker.search_pending_stories()

    @responses.activate
    def test_uses_basic_auth_with_email_and_token(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_tracker()
        tracker.search_pending_stories()
        auth_header = responses.calls[0].request.headers["Authorization"]
        assert auth_header.startswith("Basic ")
        decoded = base64.b64decode(
            auth_header.split(" ", 1)[1]
        ).decode()
        assert decoded == "alice@example.com:cloud-token"


class TestAddComment:
    @responses.activate
    def test_posts_comment_as_adf(self):
        responses.add(
            responses.POST,
            "https://acme.atlassian.net/rest/api/3/issue/NOVA-201/comment",
            json={"id": "1"},
            status=201,
        )
        tracker = make_tracker()
        tracker.add_comment("NOVA-201", "test comment")
        assert len(responses.calls) == 1
        body = responses.calls[0].request.body
        assert b'"type": "doc"' in body
        assert b"test comment" in body

    @responses.activate
    def test_raises_on_failure(self):
        responses.add(
            responses.POST,
            "https://acme.atlassian.net/rest/api/3/issue/NOVA-201/comment",
            status=403,
            json={"error": "forbidden"},
        )
        tracker = make_tracker()
        with pytest.raises(IssueTrackerError, match="Failed to add comment"):
            tracker.add_comment("NOVA-201", "test")


class TestMarkInProgress:
    @responses.activate
    def test_adds_processing_label(self):
        responses.add(
            responses.PUT,
            "https://acme.atlassian.net/rest/api/3/issue/NOVA-201",
            status=204,
        )
        tracker = make_tracker()
        tracker.mark_in_progress("NOVA-201")
        body = responses.calls[0].request.body
        assert b"ai-processing" in body


class TestGetStoryUrl:
    def test_builds_browse_url(self):
        tracker = make_tracker()
        url = tracker.get_story_url("NOVA-42")
        assert url == "https://acme.atlassian.net/browse/NOVA-42"


class TestAdfPlainTextHelpers:
    def test_simple_paragraph_round_trip(self):
        adf = _plain_text_to_adf("hello world")
        assert _adf_to_plain_text(adf).strip() == "hello world"

    def test_handles_none(self):
        assert _adf_to_plain_text(None) == ""

    def test_handles_string(self):
        assert _adf_to_plain_text("plain") == "plain"

    def test_handles_nested_content(self):
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "first "},
                        {"type": "text", "text": "second"},
                    ],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "third"}],
                },
            ],
        }
        text = _adf_to_plain_text(adf)
        assert "first second" in text
        assert "third" in text
