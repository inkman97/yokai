"""Prompt builder for the coding agent.

Kept as a separate module so users can override it without touching the
orchestrator. A prompt builder is a simple callable that takes a Story
and returns a string.
"""

from __future__ import annotations

from typing import Callable

from yokai.core.models import Story


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
6. When finished, summarize what you did.

Proceed now.
"""
