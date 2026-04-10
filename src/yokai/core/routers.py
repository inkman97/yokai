"""Story routers that decide which repository handles a story.

Two implementations:

- ComponentMapRouter: maps Jira component names to repo slugs via a dict.
- LabelPrefixRouter: looks for a label like "repo:my-repo" on the story.
- ChainRouter: tries multiple routers in order and returns the first match.
"""

from __future__ import annotations

from yokai.core.interfaces import StoryRouter
from yokai.core.models import Story


class ComponentMapRouter(StoryRouter):
    def __init__(self, mapping: dict[str, str]):
        self._mapping = dict(mapping)

    def resolve_repo(self, story: Story) -> str | None:
        for component in story.components:
            if component in self._mapping:
                return self._mapping[component]
        return None


class LabelPrefixRouter(StoryRouter):
    def __init__(self, prefix: str = "repo:"):
        self._prefix = prefix

    def resolve_repo(self, story: Story) -> str | None:
        for label in story.labels:
            if label.startswith(self._prefix):
                slug = label[len(self._prefix):]
                if slug:
                    return slug
        return None


class ChainRouter(StoryRouter):
    def __init__(self, routers: list[StoryRouter]):
        if not routers:
            raise ValueError("ChainRouter requires at least one router")
        self._routers = list(routers)

    def resolve_repo(self, story: Story) -> str | None:
        for router in self._routers:
            slug = router.resolve_repo(story)
            if slug is not None:
                return slug
        return None
