"""Unit tests for story routers."""

import pytest

from yokai.core.models import Story
from yokai.core.routers import (
    ChainRouter,
    ComponentMapRouter,
    LabelPrefixRouter,
)


class TestComponentMapRouter:
    def test_returns_repo_when_component_matches(self):
        router = ComponentMapRouter({"EMU-BE": "nova-be", "EMU-FE": "nova-fe"})
        story = Story(key="N-1", title="", description="", components=["EMU-BE"])
        assert router.resolve_repo(story) == "nova-be"

    def test_returns_none_when_no_component_matches(self):
        router = ComponentMapRouter({"EMU-BE": "nova-be"})
        story = Story(key="N-1", title="", description="", components=["OTHER"])
        assert router.resolve_repo(story) is None

    def test_returns_none_when_no_components(self):
        router = ComponentMapRouter({"EMU-BE": "nova-be"})
        story = Story(key="N-1", title="", description="")
        assert router.resolve_repo(story) is None

    def test_returns_first_match_when_multiple_components(self):
        router = ComponentMapRouter({"A": "repo-a", "B": "repo-b"})
        story = Story(key="N-1", title="", description="", components=["A", "B"])
        assert router.resolve_repo(story) == "repo-a"

    def test_constructor_copies_mapping(self):
        original = {"X": "repo-x"}
        router = ComponentMapRouter(original)
        original["X"] = "different"
        story = Story(key="N-1", title="", description="", components=["X"])
        assert router.resolve_repo(story) == "repo-x"


class TestLabelPrefixRouter:
    def test_returns_repo_from_label(self):
        router = LabelPrefixRouter("repo:")
        story = Story(
            key="N-1", title="", description="", labels=["ai-pipeline", "repo:my-repo"]
        )
        assert router.resolve_repo(story) == "my-repo"

    def test_returns_none_when_no_matching_label(self):
        router = LabelPrefixRouter("repo:")
        story = Story(key="N-1", title="", description="", labels=["ai-pipeline"])
        assert router.resolve_repo(story) is None

    def test_returns_none_when_label_has_no_value(self):
        router = LabelPrefixRouter("repo:")
        story = Story(key="N-1", title="", description="", labels=["repo:"])
        assert router.resolve_repo(story) is None

    def test_returns_first_matching_label(self):
        router = LabelPrefixRouter("repo:")
        story = Story(
            key="N-1",
            title="",
            description="",
            labels=["repo:first", "repo:second"],
        )
        assert router.resolve_repo(story) == "first"

    def test_custom_prefix(self):
        router = LabelPrefixRouter("target=")
        story = Story(
            key="N-1", title="", description="", labels=["target=my-repo"]
        )
        assert router.resolve_repo(story) == "my-repo"


class TestChainRouter:
    def test_first_router_wins(self):
        chain = ChainRouter(
            [
                ComponentMapRouter({"A": "from-component"}),
                LabelPrefixRouter("repo:"),
            ]
        )
        story = Story(
            key="N-1",
            title="",
            description="",
            components=["A"],
            labels=["repo:from-label"],
        )
        assert chain.resolve_repo(story) == "from-component"

    def test_falls_back_to_second_router(self):
        chain = ChainRouter(
            [
                ComponentMapRouter({"NOTHING": "x"}),
                LabelPrefixRouter("repo:"),
            ]
        )
        story = Story(
            key="N-1", title="", description="", labels=["repo:from-label"]
        )
        assert chain.resolve_repo(story) == "from-label"

    def test_returns_none_when_no_router_matches(self):
        chain = ChainRouter(
            [ComponentMapRouter({}), LabelPrefixRouter("repo:")]
        )
        story = Story(key="N-1", title="", description="")
        assert chain.resolve_repo(story) is None

    def test_empty_chain_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            ChainRouter([])
