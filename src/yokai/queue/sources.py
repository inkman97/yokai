"""Coordinator-side abstractions.

The Coordinator depends on these protocols. Concrete implementations
live elsewhere:
  - JiraDataCenterTracker (in yokai.adapters.jira_dc) implements IssueSource
  - LinearTracker (future) would also implement IssueSource

Keeping them as ABCs in the queue subsystem means the coordinator can
be unit-tested with fakes, without pulling in HTTP libraries or real
adapters.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StorySnapshot:
    """A minimal view of a story sufficient for the coordinator.

    The full Story object lives in yokai.core.models. This snapshot
    is what the coordinator needs to decide whether to enqueue a job.
    """

    key: str
    title: str
    description: str
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class IssueSource(ABC):
    """A source of stories to be processed (Jira, Linear, GitHub, ...)."""

    @abstractmethod
    def fetch_pending(self) -> list[StorySnapshot]:
        """Return stories that match the trigger criteria.

        Implementations are responsible for filtering by status, label,
        and any other configured criteria. They must NOT mark the
        stories as in-progress here - that is the coordinator's job
        once the story is successfully enqueued.
        """

    @abstractmethod
    def mark_accepted(self, story_key: str) -> None:
        """Signal to the source that a story has been accepted into
        the pipeline. Typically adds an `ai-processing` label so the
        next poll does not return it again.

        This is called only after enqueue succeeds. If enqueue raises
        DuplicateJobError, this is NOT called (the previous accept
        already happened or another instance won the race)."""

    @abstractmethod
    def mark_rejected(self, story_key: str, reason: str) -> None:
        """Signal that a story was found but cannot be processed
        (e.g. no repo could be resolved). Typically posts a comment."""


class StoryRouter(ABC):
    """Decides which repository should handle a given story."""

    @abstractmethod
    def resolve_repo(self, story: StorySnapshot) -> str | None:
        """Return the repo slug for this story, or None if unroutable."""


class ComponentMapRouter(StoryRouter):
    """Routes stories by Jira component name -> repo slug mapping."""

    def __init__(self, mapping: dict[str, str]):
        self._mapping = dict(mapping)

    def resolve_repo(self, story: StorySnapshot) -> str | None:
        for component in story.components:
            if component in self._mapping:
                return self._mapping[component]
        return None


class LabelPrefixRouter(StoryRouter):
    """Routes by labels of the form `repo:slug-name`."""

    def __init__(self, prefix: str = "repo:"):
        self._prefix = prefix

    def resolve_repo(self, story: StorySnapshot) -> str | None:
        for label in story.labels:
            if label.startswith(self._prefix):
                slug = label[len(self._prefix):]
                if slug:
                    return slug
        return None


class ChainRouter(StoryRouter):
    """Tries each router in order and returns the first non-None result."""

    def __init__(self, routers: list[StoryRouter]):
        if not routers:
            raise ValueError("ChainRouter requires at least one router")
        self._routers = list(routers)

    def resolve_repo(self, story: StorySnapshot) -> str | None:
        for router in self._routers:
            slug = router.resolve_repo(story)
            if slug is not None:
                return slug
        return None
