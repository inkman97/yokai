"""Tests for the StoryRouter implementations."""

import pytest

from yokai.queue.sources import (
    ChainRouter,
    ComponentMapRouter,
    LabelPrefixRouter,
    StorySnapshot,
)


def make_story(
    components=None, labels=None
) -> StorySnapshot:
    return StorySnapshot(
        key="STORY-1",
        title="Test",
        description="",
        components=components or [],
        labels=labels or [],
    )


class TestComponentMapRouter:
    def test_returns_repo_when_component_matches(self):
        router = ComponentMapRouter({"EMU-BE": "TEST-be", "EMU-FE": "TEST-fe"})
        assert router.resolve_repo(make_story(components=["EMU-BE"])) == "TEST-be"

    def test_returns_none_when_no_component_matches(self):
        router = ComponentMapRouter({"EMU-BE": "TEST-be"})
        assert router.resolve_repo(make_story(components=["OTHER"])) is None

    def test_returns_none_when_no_components(self):
        router = ComponentMapRouter({"EMU-BE": "TEST-be"})
        assert router.resolve_repo(make_story()) is None

    def test_first_matching_component_wins(self):
        router = ComponentMapRouter({"A": "repo-a", "B": "repo-b"})
        assert router.resolve_repo(make_story(components=["A", "B"])) == "repo-a"

    def test_constructor_copies_mapping(self):
        original = {"X": "repo-x"}
        router = ComponentMapRouter(original)
        original["X"] = "MUTATED"
        assert router.resolve_repo(make_story(components=["X"])) == "repo-x"


class TestLabelPrefixRouter:
    def test_returns_repo_from_matching_label(self):
        router = LabelPrefixRouter("repo:")
        assert (
            router.resolve_repo(
                make_story(labels=["ai-pipeline", "repo:my-repo"])
            )
            == "my-repo"
        )

    def test_returns_none_when_no_matching_label(self):
        router = LabelPrefixRouter("repo:")
        assert router.resolve_repo(make_story(labels=["ai-pipeline"])) is None

    def test_returns_none_when_label_value_empty(self):
        router = LabelPrefixRouter("repo:")
        assert router.resolve_repo(make_story(labels=["repo:"])) is None

    def test_first_matching_label_wins(self):
        router = LabelPrefixRouter("repo:")
        assert (
            router.resolve_repo(
                make_story(labels=["repo:first", "repo:second"])
            )
            == "first"
        )

    def test_custom_prefix(self):
        router = LabelPrefixRouter("target=")
        assert (
            router.resolve_repo(make_story(labels=["target=my-repo"]))
            == "my-repo"
        )


class TestChainRouter:
    def test_first_router_wins(self):
        chain = ChainRouter(
            [
                ComponentMapRouter({"A": "from-component"}),
                LabelPrefixRouter("repo:"),
            ]
        )
        story = make_story(
            components=["A"], labels=["repo:from-label"]
        )
        assert chain.resolve_repo(story) == "from-component"

    def test_falls_back_to_second_router(self):
        chain = ChainRouter(
            [
                ComponentMapRouter({"NOTHING": "x"}),
                LabelPrefixRouter("repo:"),
            ]
        )
        story = make_story(labels=["repo:from-label"])
        assert chain.resolve_repo(story) == "from-label"

    def test_returns_none_when_no_router_matches(self):
        chain = ChainRouter(
            [ComponentMapRouter({}), LabelPrefixRouter("repo:")]
        )
        assert chain.resolve_repo(make_story()) is None

    def test_empty_chain_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            ChainRouter([])
