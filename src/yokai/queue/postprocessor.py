"""Postprocessor abstraction.

After a Worker finishes a job successfully, the Postprocessor takes
the result and performs the side effects that close the loop:
  - commit the agent's changes
  - push the branch
  - open a pull request
  - add a comment on the source story
  - any other downstream notification

Concrete implementations live in yokai.adapters (BitbucketDC + Jira
combined into a single Postprocessor, for example) - the queue
subsystem does not know about git, HTTP, or any specific provider.

The ResultHandler invokes Postprocessor.run() for each successful
result and translates the outcome into a queue state transition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from yokai.queue.models import Job, JobResult


@dataclass
class PostprocessOutcome:
    """Outcome of postprocessing a single completed job."""

    success: bool
    pr_url: str | None = None
    error: str | None = None


class Postprocessor(ABC):
    """Performs the commit/push/PR/comment side effects for a completed job."""

    @abstractmethod
    def run(self, job: Job, result: JobResult) -> PostprocessOutcome:
        """Execute postprocessing for a completed job.

        Must not raise on expected failures (network, auth, missing
        repo state) - return PostprocessOutcome(success=False, error=...).
        Exceptions are caught by the ResultHandler but treated as
        crashes rather than expected failures.
        """
