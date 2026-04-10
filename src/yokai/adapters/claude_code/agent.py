"""Claude Code CLI adapter implementing CodingAgent.

Runs the Claude Code command-line tool in non-interactive mode against
a working tree. The prompt is passed via stdin to avoid command-line
escaping issues, especially on Windows where multi-line arguments and
special characters break shell parsing.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from yokai.core.exceptions import (
    AgentExecutionError,
    AgentTimeoutError,
)
from yokai.core.interfaces import CodingAgent
from yokai.core.logging_setup import get_logger
from yokai.core.models import AgentResult

log = get_logger("adapters.claude_code")


@dataclass
class ClaudeCodeSettings:
    command: str = "claude"
    flags: list[str] = field(
        default_factory=lambda: ["--print", "--dangerously-skip-permissions"]
    )
    timeout_seconds: int = 1800


class ClaudeCodeAgent(CodingAgent):
    def __init__(self, settings: ClaudeCodeSettings):
        self._settings = settings

    def run(self, repo_path: Path, prompt: str) -> AgentResult:
        executable = shutil.which(self._settings.command)
        if not executable:
            raise AgentExecutionError(
                f"Claude Code executable not found in PATH: {self._settings.command}"
            )

        log.info(f"Running Claude Code at {executable}")
        log.info(f"Prompt length: {len(prompt)} chars")

        cmd = [executable] + list(self._settings.flags)
        started = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                cwd=repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._settings.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as e:
            raise AgentTimeoutError(
                f"Claude Code timed out after {self._settings.timeout_seconds}s"
            ) from e

        duration = time.monotonic() - started

        if result.returncode != 0:
            raise AgentExecutionError(
                f"Claude Code exited with code {result.returncode}: "
                f"{result.stderr.strip()}"
            )

        return AgentResult(
            success=True,
            output=result.stdout,
            duration_seconds=duration,
            error=None,
        )
