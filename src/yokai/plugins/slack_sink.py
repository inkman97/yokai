"""Slack webhook notification sink.

Posts messages to a Slack incoming webhook URL. Failures are logged
but do not propagate, since notifications are best-effort and must not
break the pipeline.
"""

from __future__ import annotations

import requests

from yokai.core.interfaces import NotificationSink
from yokai.core.logging_setup import get_logger
from yokai.core.models import PullRequest, Story

log = get_logger("notifications.slack")


class SlackWebhookSink(NotificationSink):
    def __init__(self, webhook_url: str, timeout: int = 10):
        self._url = webhook_url
        self._timeout = timeout

    def notify_started(self, story: Story, repo_slug: str) -> None:
        self._post(
            f":rocket: Pipeline started for *{story.key}* - {story.title}\n"
            f"Repository: `{repo_slug}`"
        )

    def notify_succeeded(self, story: Story, pr: PullRequest) -> None:
        self._post(
            f":white_check_mark: *{story.key}* ready for review\n"
            f"{story.title}\n"
            f"Pull request: {pr.url}"
        )

    def notify_failed(self, story: Story, error: str) -> None:
        self._post(
            f":x: Pipeline failed for *{story.key}*\n"
            f"{story.title}\n"
            f"Error: {error}"
        )

    def _post(self, text: str) -> None:
        try:
            requests.post(
                self._url, json={"text": text}, timeout=self._timeout
            )
        except requests.RequestException as e:
            log.warning(f"Slack notification failed: {e}")
