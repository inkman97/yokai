"""Unit tests for branch naming."""

from yokai.core.branch_naming import render_branch_name, slugify


class TestSlugify:
    def test_lowercases(self):
        assert slugify("Hello World") == "hello-world"

    def test_collapses_special_chars(self):
        assert slugify("[EMU] Fix: error handling (404/500)") == "emu-fix-error-handling-404-500"

    def test_strips_trailing_hyphens(self):
        assert slugify("hello!!!") == "hello"

    def test_truncates_to_max_length(self):
        long = "word " * 50
        result = slugify(long, max_length=20)
        assert len(result) <= 20

    def test_empty_falls_back(self):
        assert slugify("") == "story"
        assert slugify("   ") == "story"
        assert slugify("!!!") == "story"


class TestRenderBranchName:
    def test_default_pattern(self):
        result = render_branch_name(
            "feature/{issue_key}-ai-{timestamp}",
            issue_key="NOVA-101",
            timestamp=1234567890,
        )
        assert result == "feature/NOVA-101-ai-1234567890"

    def test_lowercase_placeholder(self):
        result = render_branch_name(
            "ai/{issue_key_lc}-{timestamp}",
            issue_key="NOVA-101",
            timestamp=42,
        )
        assert result == "ai/nova-101-42"

    def test_slug_placeholder(self):
        result = render_branch_name(
            "feature/{issue_key}-{slug}",
            issue_key="NOVA-1",
            title="Improve Error Handling",
            timestamp=0,
        )
        assert result == "feature/NOVA-1-improve-error-handling"

    def test_timestamp_defaults_to_now_when_none(self):
        result = render_branch_name(
            "x-{timestamp}", issue_key="N-1", timestamp=None
        )
        assert result.startswith("x-")
        ts_part = result.split("-", 1)[1]
        assert ts_part.isdigit()

    def test_mixed_placeholders(self):
        result = render_branch_name(
            "{issue_key}/{slug}-{timestamp}",
            issue_key="NOVA-42",
            title="Do stuff",
            timestamp=1000,
        )
        assert result == "NOVA-42/do-stuff-1000"
