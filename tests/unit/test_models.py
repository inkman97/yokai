"""Unit tests for domain models."""

from yokai.core.models import (
    FileChange,
    Story,
    StoryStatus,
)


class TestStory:
    def test_has_label_returns_true_when_label_present(self):
        story = Story(
            key="NOVA-1",
            title="Test",
            description="",
            labels=["ai-pipeline", "backend"],
        )
        assert story.has_label("ai-pipeline") is True

    def test_has_label_returns_false_when_label_absent(self):
        story = Story(key="NOVA-1", title="Test", description="")
        assert story.has_label("ai-pipeline") is False

    def test_has_component_returns_true_when_present(self):
        story = Story(
            key="NOVA-1",
            title="Test",
            description="",
            components=["EMU-BE"],
        )
        assert story.has_component("EMU-BE") is True

    def test_has_component_returns_false_when_absent(self):
        story = Story(key="NOVA-1", title="Test", description="")
        assert story.has_component("EMU-BE") is False

    def test_default_collections_are_independent_per_instance(self):
        a = Story(key="A", title="", description="")
        b = Story(key="B", title="", description="")
        a.labels.append("x")
        assert b.labels == []


class TestFileChange:
    def test_is_test_detects_test_directory(self):
        f = FileChange(
            path="src/main/test/java/Foo.java", added=10, removed=2
        )
        assert f.is_test is True

    def test_is_test_detects_tests_directory(self):
        f = FileChange(path="src/tests/test_foo.py", added=5, removed=0)
        assert f.is_test is True

    def test_is_test_returns_false_for_source_files(self):
        f = FileChange(path="src/main/java/Foo.java", added=20, removed=3)
        assert f.is_test is False


class TestStoryStatus:
    def test_status_values_are_lowercase_strings(self):
        assert StoryStatus.PENDING.value == "pending"
        assert StoryStatus.IN_PROGRESS.value == "in_progress"
        assert StoryStatus.COMPLETED.value == "completed"
        assert StoryStatus.FAILED.value == "failed"
