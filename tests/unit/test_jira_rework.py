"""Tests for Jira DC and Cloud rework methods (search, mark_rework_*)."""

import responses

from yokai.adapters.jira_dc.tracker import (
    JiraDataCenterSettings,
    JiraDataCenterTracker,
)
from yokai.adapters.jira_cloud.tracker import (
    JiraCloudSettings,
    JiraCloudTracker,
)


def make_dc_tracker():
    return JiraDataCenterTracker(
        JiraDataCenterSettings(
            base_url="https://jira.example.com",
            project="NOVA",
            account="testuser",
            token="test-token",
        )
    )


def make_cloud_tracker():
    return JiraCloudTracker(
        JiraCloudSettings(
            base_url="https://acme.atlassian.net",
            project="NOVA",
            account="alice@example.com",
            token="cloud-token",
        )
    )


REWORK_SEARCH_RESPONSE = {
    "issues": [
        {
            "key": "NOVA-200",
            "fields": {
                "summary": "Fix review comments",
                "description": "Reviewer asked for changes",
                "components": [{"name": "EMU-BE"}],
                "labels": ["ai-pipeline", "ai-rework"],
            },
        },
    ]
}


class TestJiraDcSearchRework:
    @responses.activate
    def test_returns_rework_stories(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json=REWORK_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_dc_tracker()
        stories = tracker.search_rework_stories()

        assert len(stories) == 1
        assert stories[0].key == "NOVA-200"
        assert "ai-rework" in stories[0].labels

    @responses.activate
    def test_jql_filters_by_rework_label(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_dc_tracker()
        tracker.search_rework_stories()
        url = responses.calls[0].request.url
        assert "ai-rework" in url
        assert "ai-processing" in url

    @responses.activate
    def test_returns_empty_when_no_rework(self):
        responses.add(
            responses.GET,
            "https://jira.example.com/rest/api/2/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_dc_tracker()
        assert tracker.search_rework_stories() == []


class TestJiraDcMarkRework:
    @responses.activate
    def test_mark_rework_in_progress_adds_processing_label(self):
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/NOVA-200",
            status=204,
        )
        tracker = make_dc_tracker()
        tracker.mark_rework_in_progress("NOVA-200")
        body = responses.calls[0].request.body
        assert b"ai-processing" in body

    @responses.activate
    def test_mark_rework_done_removes_rework_and_processing_adds_done(self):
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/NOVA-200",
            status=204,
        )
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/NOVA-200",
            status=204,
        )
        responses.add(
            responses.PUT,
            "https://jira.example.com/rest/api/2/issue/NOVA-200",
            status=204,
        )
        tracker = make_dc_tracker()
        tracker.mark_rework_done("NOVA-200")

        assert len(responses.calls) == 3
        bodies = [c.request.body for c in responses.calls]
        assert any(b"ai-rework" in b and b"remove" in b for b in bodies)
        assert any(b"ai-processing" in b and b"remove" in b for b in bodies)
        assert any(b"ai-done" in b and b"add" in b for b in bodies)


class TestJiraCloudSearchRework:
    @responses.activate
    def test_returns_rework_stories(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            json=REWORK_SEARCH_RESPONSE,
            status=200,
        )
        tracker = make_cloud_tracker()
        stories = tracker.search_rework_stories()

        assert len(stories) == 1
        assert stories[0].key == "NOVA-200"

    @responses.activate
    def test_jql_filters_by_rework_label(self):
        responses.add(
            responses.GET,
            "https://acme.atlassian.net/rest/api/3/search",
            json={"issues": []},
            status=200,
        )
        tracker = make_cloud_tracker()
        tracker.search_rework_stories()
        url = responses.calls[0].request.url
        assert "ai-rework" in url


class TestJiraCloudMarkRework:
    @responses.activate
    def test_mark_rework_done_removes_rework_and_processing_adds_done(self):
        responses.add(
            responses.PUT,
            "https://acme.atlassian.net/rest/api/3/issue/NOVA-200",
            status=204,
        )
        responses.add(
            responses.PUT,
            "https://acme.atlassian.net/rest/api/3/issue/NOVA-200",
            status=204,
        )
        responses.add(
            responses.PUT,
            "https://acme.atlassian.net/rest/api/3/issue/NOVA-200",
            status=204,
        )
        tracker = make_cloud_tracker()
        tracker.mark_rework_done("NOVA-200")

        assert len(responses.calls) == 3
