"""Compatibility adapters between yokai.core interfaces and
yokai.queue interfaces.

Use these to plug existing IssueTracker / RepoHosting / CodingAgent
adapters (Jira DC/Cloud, Bitbucket DC/Cloud, Claude Code) into the
async Coordinator/Worker/ResultHandler subsystem.
"""

from yokai.queue_adapters.agent_runner import (
    AgentCodingRunner,
    job_to_story,
)
from yokai.queue_adapters.hosting_checkout import HostingRepoCheckout
from yokai.queue_adapters.postprocessor import HostingTrackerPostprocessor
from yokai.queue_adapters.tracker_source import (
    TrackerIssueSource,
    story_to_snapshot,
)

__all__ = [
    "TrackerIssueSource",
    "HostingRepoCheckout",
    "AgentCodingRunner",
    "HostingTrackerPostprocessor",
    "job_to_story",
    "story_to_snapshot",
]
