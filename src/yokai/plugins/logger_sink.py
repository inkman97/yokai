"""Notification sink that logs events via the framework logger.

Simple default implementation. Useful for local runs or as a fallback.
"""

from __future__ import annotations

from yokai.core.interfaces import NotificationSink
from yokai.core.logging_setup import get_logger
from yokai.core.models import PullRequest, Story

log = get_logger("notifications.logger")


class LoggerNotificationSink(NotificationSink):
    def notify_started(self, story: Story, repo_slug: str) -> None:
        log.info(f"STARTED {story.key} on repo {repo_slug}")

    def notify_succeeded(self, story: Story, pr: PullRequest) -> None:
        log.info(f"SUCCEEDED {story.key} -> PR {pr.url}")

    def notify_failed(self, story: Story, error: str) -> None:
        log.warning(f"FAILED {story.key}: {error}")
