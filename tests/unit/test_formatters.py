"""Unit tests for PR description and Jira comment formatters."""

from datetime import datetime

from yokai.core.formatters import (
    build_jira_detailed_comment,
    build_jira_short_comment,
    build_pr_description,
)
from yokai.core.models import (
    AgentResult,
    CommitInfo,
    FileChange,
    Story,
)


def make_story():
    return Story(
        key="NOVA-101",
        title="Improve error handling",
        description="...",
        components=["EMU-BE"],
    )


def make_commit():
    return CommitInfo(
        sha="abcdef0123456789",
        short_sha="abcdef0",
        message="feat(NOVA-101): improve error handling",
        files_changed=4,
        insertions=80,
        deletions=10,
    )


def make_files():
    return [
        FileChange(path="src/main/java/Foo.java", added=20, removed=5),
        FileChange(path="src/main/java/Bar.java", added=10, removed=2),
        FileChange(path="src/test/java/FooTest.java", added=30, removed=0),
        FileChange(path="src/test/java/BarTest.java", added=20, removed=3),
    ]


class TestBuildPrDescription:
    def test_includes_story_key_and_title(self):
        result = build_pr_description(
            story=make_story(),
            story_url="https://jira.example.com/browse/NOVA-101",
            branch_name="feature/NOVA-101-ai",
            target_branch="master",
            commit=make_commit(),
            changed_files=make_files(),
            timestamp=datetime(2026, 4, 10, 14, 30),
        )
        assert "NOVA-101" in result
        assert "Improve error handling" in result

    def test_includes_branch_info(self):
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="feature/NOVA-101-ai",
            target_branch="master",
            commit=make_commit(),
            changed_files=make_files(),
        )
        assert "feature/NOVA-101-ai" in result
        assert "master" in result

    def test_includes_commit_short_sha(self):
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="b",
            target_branch="master",
            commit=make_commit(),
            changed_files=make_files(),
        )
        assert "abcdef0" in result

    def test_separates_source_and_test_files(self):
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="b",
            target_branch="master",
            commit=make_commit(),
            changed_files=make_files(),
        )
        assert "Source (2)" in result
        assert "Tests (2)" in result

    def test_totals_are_correct(self):
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="b",
            target_branch="master",
            commit=make_commit(),
            changed_files=make_files(),
        )
        assert "+80 -10" in result

    def test_no_test_section_when_no_tests(self):
        files = [FileChange(path="src/main/Foo.java", added=10, removed=0)]
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="b",
            target_branch="master",
            commit=make_commit(),
            changed_files=files,
        )
        assert "Tests" not in result
        assert "Source" in result

    def test_no_source_section_when_only_tests(self):
        files = [FileChange(path="src/test/FooTest.java", added=10, removed=0)]
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="b",
            target_branch="master",
            commit=make_commit(),
            changed_files=files,
        )
        assert "Source" not in result
        assert "Tests" in result

    def test_review_warning_present(self):
        result = build_pr_description(
            story=make_story(),
            story_url="x",
            branch_name="b",
            target_branch="master",
            commit=make_commit(),
            changed_files=make_files(),
        )
        assert "human review" in result.lower()


class TestBuildJiraShortComment:
    def test_includes_pr_url(self):
        result = build_jira_short_comment(
            pr_url="https://bitbucket.example.com/pr/1",
            branch_name="feature/x",
            changed_files=make_files(),
        )
        assert "https://bitbucket.example.com/pr/1" in result

    def test_includes_branch_in_jira_code_format(self):
        result = build_jira_short_comment(
            pr_url="x",
            branch_name="feature/x",
            changed_files=make_files(),
        )
        assert "{{feature/x}}" in result

    def test_includes_file_counts(self):
        result = build_jira_short_comment(
            pr_url="x",
            branch_name="b",
            changed_files=make_files(),
        )
        assert "4" in result
        assert "2 source" in result
        assert "2 tests" in result

    def test_includes_diff_size(self):
        result = build_jira_short_comment(
            pr_url="x",
            branch_name="b",
            changed_files=make_files(),
        )
        assert "+80" in result
        assert "-10" in result


class TestBuildJiraDetailedComment:
    def test_wraps_output_in_panel_and_noformat(self):
        result = build_jira_detailed_comment(
            AgentResult(
                success=True,
                output="All tests pass",
                duration_seconds=120.0,
            )
        )
        assert "{panel" in result
        assert "{noformat}" in result
        assert "All tests pass" in result

    def test_has_h3_header(self):
        result = build_jira_detailed_comment(
            AgentResult(success=True, output="x", duration_seconds=1.0)
        )
        assert result.startswith("h3.")

    def test_strips_leading_trailing_whitespace_from_output(self):
        result = build_jira_detailed_comment(
            AgentResult(
                success=True,
                output="\n\n  Real content  \n\n",
                duration_seconds=1.0,
            )
        )
        assert "Real content" in result
