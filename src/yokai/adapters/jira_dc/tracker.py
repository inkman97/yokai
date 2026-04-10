"""Jira Data Center adapter implementing IssueTracker.

Uses the Jira REST API v2 with Bearer token authentication (Personal
Access Token). Compatible with on-premise installations behind reverse
proxies and SSO, provided the PAT is valid for the configured user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from yokai.core.exceptions import IssueTrackerError
from yokai.core.interfaces import IssueTracker
from yokai.core.logging_setup import get_logger
from yokai.core.models import Story

log = get_logger("adapters.jira_dc")


@dataclass
class JiraDataCenterSettings:
    base_url: str
    project: str
    username: str
    token: str
    trigger_label: str = "ai-pipeline"
    processing_label: str = "ai-processing"
    status: str = "Backlog"
    request_timeout: int = 15


class JiraDataCenterTracker(IssueTracker):
    def __init__(self, settings: JiraDataCenterSettings):
        self._settings = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def search_pending_stories(self) -> list[Story]:
        s = self._settings
        jql = (
            f'project = {s.project} '
            f'AND status = "{s.status}" '
            f'AND labels = "{s.trigger_label}" '
            f'AND labels != "{s.processing_label}"'
        )
        url = f"{s.base_url}/rest/api/2/search"
        params = {
            "jql": jql,
            "fields": "summary,description,components,labels",
            "maxResults": 50,
        }
        try:
            response = self._session.get(
                url, params=params, timeout=s.request_timeout
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise IssueTrackerError(f"Jira search failed: {e}") from e

        issues = response.json().get("issues", [])
        stories = [self._issue_to_story(issue) for issue in issues]
        log.info(f"Jira returned {len(stories)} pending stories")
        return stories

    def mark_in_progress(self, story_key: str) -> None:
        self._add_label(story_key, self._settings.processing_label)

    def mark_failed(self, story_key: str, reason: str) -> None:
        self.add_comment(story_key, f"Pipeline AI failed: {reason}")

    def add_comment(self, story_key: str, body: str) -> None:
        s = self._settings
        url = f"{s.base_url}/rest/api/2/issue/{story_key}/comment"
        try:
            response = self._session.post(
                url, json={"body": body}, timeout=s.request_timeout
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise IssueTrackerError(
                f"Failed to add comment to {story_key}: {e}"
            ) from e

    def get_story_url(self, story_key: str) -> str:
        return f"{self._settings.base_url}/browse/{story_key}"

    def _add_label(self, story_key: str, label: str) -> None:
        s = self._settings
        url = f"{s.base_url}/rest/api/2/issue/{story_key}"
        payload = {"update": {"labels": [{"add": label}]}}
        try:
            response = self._session.put(
                url, json=payload, timeout=s.request_timeout
            )
            response.raise_for_status()
        except requests.RequestException as e:
            raise IssueTrackerError(
                f"Failed to add label {label} to {story_key}: {e}"
            ) from e

    def _issue_to_story(self, issue: dict[str, Any]) -> Story:
        fields = issue.get("fields", {}) or {}
        components_raw = fields.get("components") or []
        labels_raw = fields.get("labels") or []
        return Story(
            key=issue["key"],
            title=fields.get("summary", ""),
            description=fields.get("description", "") or "",
            components=[c.get("name", "") for c in components_raw],
            labels=list(labels_raw),
            url=self.get_story_url(issue["key"]),
            raw=issue,
        )
