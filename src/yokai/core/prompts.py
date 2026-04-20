"""Prompt builder for the coding agent.

Kept as a separate module so users can override it without touching the
orchestrator. A prompt builder is a simple callable that takes a Story
and returns a string.
"""

from __future__ import annotations

from typing import Callable

from yokai.core.models import PRComment, Story


PromptBuilder = Callable[[Story], str]


def default_prompt_builder(story: Story) -> str:
    return f"""You are a senior software engineer.
Your task is to implement the following user story in the repository
you are running in.

## Story
{story.key}: {story.title}

## Description and acceptance criteria
{story.description}

## Operating instructions
1. Explore the repository structure to understand existing patterns.
2. Identify the files to modify or create.
3. Implement the story respecting the existing style and conventions.
4. Add tests (unit and end-to-end where appropriate).
5. Do not modify build or CI configuration unless strictly necessary.
6. When finished, write a structured summary using the exact format below.

## Required summary format

When you are done, output your summary in this exact structure:

### Summary
One or two sentences describing the overall change.

### Production code changes
For EVERY file you modified or created (not test files), list:
- File name and whether it is NEW or MODIFIED
- What you changed and why

### Test changes
For EVERY test file you modified or created, list:
- File name and whether it is NEW or MODIFIED
- What tests you added or changed

### Impact
Describe the before/after behavior change from the user's perspective.

Important: list ALL files you touched, not just the most important ones.
Do not omit files from the summary.

Proceed now.
"""


def rework_prompt_builder(
    story: Story, pr_comments: list[PRComment]
) -> str:
    """Build a prompt for rework: fix PR review comments."""
    comments_text = _format_pr_comments(pr_comments)

    return f"""You are a senior software engineer.
A pull request was opened for the following story, and the reviewer
left comments that need to be addressed. Your task is to fix the
issues described in the review comments.

## Story
{story.key}: {story.title}

## Description
{story.description}

## Review comments to address
{comments_text}

## Operating instructions
1. Read each review comment carefully.
2. For each comment, identify the file and the issue described.
3. Fix the code to address the reviewer's feedback.
4. Update or add tests if the reviewer requested them.
5. Do not modify build or CI configuration unless strictly necessary.
6. When finished, write a structured summary using the exact format below.

## Required summary format

When you are done, output your summary in this exact structure:

### Summary
One or two sentences describing the rework.

### Review comments addressed
For EACH review comment, explain what you did to address it.

### Files changed
For EVERY file you modified, list:
- File name
- What you changed

Important: address ALL review comments, not just some of them.

Proceed now.
"""


def _format_pr_comments(comments: list[PRComment]) -> str:
    """Format PR comments into a readable block for the prompt."""
    if not comments:
        return "(No review comments found)"

    parts: list[str] = []
    for i, c in enumerate(comments, 1):
        location = ""
        if c.file_path:
            location = f" in `{c.file_path}`"
            if c.line:
                location += f" (line {c.line})"
        parts.append(
            f"{i}. [{c.author}]{location}:\n   {c.text}"
        )
    return "\n\n".join(parts)
