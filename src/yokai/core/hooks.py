"""Plugin hook system.

Plugins extend the framework without modifying its core by registering
callbacks for named events. The orchestrator emits events at well-known
points in the pipeline.

Event names and their payloads:
- "before_process"       {"story": Story}
- "after_resolve_repo"   {"story": Story, "repo_slug": str}
- "after_clone"          {"story": Story, "repo_path": Path}
- "before_agent_run"     {"story": Story, "repo_path": Path, "prompt": str}
- "after_agent_run"      {"story": Story, "agent_result": AgentResult}
- "after_commit"         {"story": Story, "commit": CommitInfo}
- "after_push"           {"story": Story, "branch_name": str}
- "after_pull_request"   {"story": Story, "pull_request": PullRequest}
- "on_success"           {"story": Story, "pull_request": PullRequest}
- "on_failure"           {"story": Story, "error": Exception}

Hooks are best-effort. If a hook raises, the exception is logged but
does not break the pipeline. This keeps third-party plugins from
crashing the main flow.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from yokai.core.logging_setup import get_logger

log = get_logger("hooks")


HookCallback = Callable[[dict[str, Any]], None]


class HookRegistry:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = defaultdict(list)

    def register(self, event: str, callback: HookCallback) -> None:
        self._hooks[event].append(callback)

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        for callback in self._hooks.get(event, []):
            try:
                callback(payload)
            except Exception:
                log.exception(
                    f"Hook callback for {event} raised (suppressed)"
                )

    def count(self, event: str) -> int:
        return len(self._hooks.get(event, []))

    def clear(self) -> None:
        self._hooks.clear()
