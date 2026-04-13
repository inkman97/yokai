"""Jira Cloud adapter implementing IssueTracker.

Uses the Jira Cloud REST API v3 with HTTP Basic authentication. On Jira
Cloud the credentials are an Atlassian account email plus an API token
generated from id.atlassian.com, not a username/password pair.

Mapping to FrameworkConfig fields:
- IssueTrackerConfig.username  -> Atlassian account email
- IssueTrackerConfig.token     -> API token from id.atlassian.com
- IssueTrackerConfig.base_url  -> https://your-site.atlassian.net
- IssueTrackerConfig.project   -> project key (same concept as DC)

The Cloud v3 API returns issue descriptions in Atlassian Document Format
(ADF), which is a structured JSON tree rather than plain text. This
adapter walks that tree and extracts the visible text so the rest of the
framework receives a plain string compatible with the DC behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from yokai.core.exceptions import IssueTrackerError
from yokai.core.interfaces import IssueTracker
from yokai.core.logging_setup import get_logger
from yokai.core.models import Story

log = get_logger("adapters.jira_cloud")


@dataclass
class JiraCloudSettings:
    base_url: str
    project: str
    email: str
    api_token: str
    trigger_label: str = "ai-pipeline"
    processing_label: str = "ai-processing"
    status: str = "Backlog"
    request_timeout: int = 15


class JiraCloudTracker(IssueTracker):
    def __init__(self, settings: JiraCloudSettings):
        self._settings = settings
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(settings.email, settings.api_token)
        self._session.headers.update(
            {
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
        url = f"{s.base_url}/rest/api/3/search"
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
            raise IssueTrackerError(f"Jira Cloud search failed: {e}") from e

        issues = response.json().get("issues", [])
        stories = [self._issue_to_story(issue) for issue in issues]
        log.info(f"Jira Cloud returned {len(stories)} pending stories")
        return stories

    def mark_in_progress(self, story_key: str) -> None:
        self._add_label(story_key, self._settings.processing_label)

    def mark_failed(self, story_key: str, reason: str) -> None:
        self.add_comment(story_key, f"Pipeline AI failed: {reason}")

    def add_comment(self, story_key: str, body: str) -> None:
        s = self._settings
        url = f"{s.base_url}/rest/api/3/issue/{story_key}/comment"
        # Cloud v3 expects ADF for the comment body, not plain text.
        payload = {"body": _plain_text_to_adf(body)}
        try:
            response = self._session.post(
                url, json=payload, timeout=s.request_timeout
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
        url = f"{s.base_url}/rest/api/3/issue/{story_key}"
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
        description = _adf_to_plain_text(fields.get("description"))
        return Story(
            key=issue["key"],
            title=fields.get("summary", ""),
            description=description,
            components=[c.get("name", "") for c in components_raw],
            labels=list(labels_raw),
            url=self.get_story_url(issue["key"]),
            raw=issue,
        )


def _adf_to_plain_text(node: Any) -> str:
    """Best-effort flattening of an ADF document into plain text.

    Walks the tree and concatenates the `text` fields of any text nodes,
    inserting newlines between top-level paragraphs. Sufficient for the
    framework's needs; it is not a full ADF renderer.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")

    parts: list[str] = []
    for child in node.get("content", []) or []:
        chunk = _adf_to_plain_text(child)
        if chunk:
            parts.append(chunk)

    if node_type in ("paragraph", "heading", "blockquote", "listItem"):
        return "".join(parts) + "\n"

    return "".join(parts)


def _plain_text_to_adf(text: str) -> dict[str, Any]:
    """Wrap a plain text string in the minimal ADF structure Jira Cloud
    requires for a comment body.
    """
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
