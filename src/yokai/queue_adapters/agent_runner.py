"""Bridge from yokai.core.CodingAgent to yokai.queue.AgentRunner.

Wraps the existing ClaudeCodeAgent (or any CodingAgent) so the queue
Worker can use it. Reconstructs a minimal Story from the Job payload,
builds the prompt with the same prompt builder used by the legacy
Pipeline, and translates AgentResult into AgentExecution.

The Worker enforces its own timeout via lease semantics; the
CodingAgent has its own subprocess timeout. Both should be set to the
same value via WorkerSettings.agent_timeout_seconds and the agent's
own settings (e.g. ClaudeCodeSettings.timeout_seconds).
"""

from __future__ import annotations

import traceback
from pathlib import Path

from yokai.core.exceptions import (
    AgentExecutionError,
    AgentTimeoutError,
)
from yokai.core.interfaces import CodingAgent
from yokai.core.models import PRComment, Story
from yokai.core.prompts import PromptBuilder, default_prompt_builder, rework_prompt_builder
from yokai.queue.agent import AgentExecution, AgentRunner
from yokai.queue.models import Job


def job_to_story(job: Job) -> Story:
    """Reconstruct a minimal Story from a Job payload.

    The payload was put there by the Coordinator from a StorySnapshot.
    We rebuild a Story with the same fields so the existing
    PromptBuilder can be reused unchanged.
    """
    p = job.payload or {}
    return Story(
        key=job.story_key,
        title=p.get("title", ""),
        description=p.get("description", ""),
        components=list(p.get("components", [])),
        labels=list(p.get("labels", [])),
        url=p.get("url"),
        raw=dict(p.get("raw", {})),
    )


def _deserialize_pr_comments(raw_comments: list[dict]) -> list[PRComment]:
    """Reconstruct PRComment objects from serialized dicts in the payload."""
    return [
        PRComment(
            id=c.get("id", ""),
            author=c.get("author", ""),
            text=c.get("text", ""),
            file_path=c.get("file_path"),
            line=c.get("line"),
            severity=c.get("severity", ""),
            state=c.get("state", ""),
            created_at=c.get("created_at", ""),
        )
        for c in raw_comments
    ]


class AgentCodingRunner(AgentRunner):
    def __init__(
        self,
        agent: CodingAgent,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self._agent = agent
        self._prompt_builder = prompt_builder or default_prompt_builder

    def run(
        self,
        job: Job,
        repo_path: Path,
        timeout_seconds: float,
    ) -> AgentExecution:
        story = job_to_story(job)
        try:
            if job.payload.get("job_type") == "rework":
                raw_comments = job.payload.get("pr_comments", [])
                pr_comments = _deserialize_pr_comments(raw_comments)
                prompt = rework_prompt_builder(story, pr_comments)
            else:
                prompt = self._prompt_builder(story)
        except Exception as e:
            return AgentExecution(
                success=False,
                error=f"Prompt builder failed: {e}",
                traceback=traceback.format_exc(),
            )

        try:
            result = self._agent.run(repo_path, prompt)
        except AgentTimeoutError as e:
            return AgentExecution(
                success=False,
                error=f"Agent timed out: {e}",
                traceback=traceback.format_exc(),
            )
        except AgentExecutionError as e:
            return AgentExecution(
                success=False,
                error=f"Agent execution error: {e}",
                traceback=traceback.format_exc(),
            )
        except Exception as e:
            return AgentExecution(
                success=False,
                error=f"Unexpected agent error: {e}",
                traceback=traceback.format_exc(),
            )

        return AgentExecution(
            success=result.success,
            output=result.output,
            error=result.error,
        )
