"""Bridge from yokai.core.IssueTracker to yokai.queue.IssueSource.

Wraps the existing JiraDataCenterTracker / JiraCloudTracker (or any
other IssueTracker) so the queue Coordinator can use them.

Mapping:
  IssueTracker.search_pending_stories -> IssueSource.fetch_pending
                                         (Story converted to StorySnapshot)
  IssueTracker.mark_in_progress       -> IssueSource.mark_accepted
  IssueTracker.mark_failed            -> IssueSource.mark_rejected
"""

from __future__ import annotations

from yokai.core.interfaces import IssueTracker
from yokai.core.models import Story
from yokai.queue.sources import IssueSource, StorySnapshot


def story_to_snapshot(story: Story) -> StorySnapshot:
    return StorySnapshot(
        key=story.key,
        title=story.title,
        description=story.description,
        components=list(story.components),
        labels=list(story.labels),
        raw=dict(story.raw),
    )


class TrackerIssueSource(IssueSource):
    def __init__(self, tracker: IssueTracker) -> None:
        self._tracker = tracker

    def fetch_pending(self) -> list[StorySnapshot]:
        stories = self._tracker.search_pending_stories()
        return [story_to_snapshot(s) for s in stories]

    def fetch_rework(self) -> list[StorySnapshot]:
        stories = self._tracker.search_rework_stories()
        return [story_to_snapshot(s) for s in stories]

    def mark_accepted(self, story_key: str) -> None:
        self._tracker.mark_in_progress(story_key)

    def mark_rework_accepted(self, story_key: str) -> None:
        self._tracker.mark_rework_in_progress(story_key)

    def mark_rejected(self, story_key: str, reason: str) -> None:
        self._tracker.mark_failed(story_key, reason)
