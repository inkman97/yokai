# Writing an adapter

This guide shows how to add support for a new issue tracker, repo
hosting provider, or coding agent. The same pattern applies to all
three: implement an interface, register a builder with the factory.

## The general pattern

1. Pick the right interface from `yokai.core.interfaces`.
2. Write a class that implements every abstract method.
3. Define a settings dataclass for the configuration the adapter needs.
4. Register a builder function with the factory.

The factory builder is a small function that takes a `FrameworkConfig`
and returns an instance of your class. Once registered, your adapter
can be selected from YAML by setting the matching `type` field.

## Example: a GitHub Issues tracker

Suppose you want to use GitHub Issues instead of Jira Data Center as
the issue source. The interface to implement is `IssueTracker`.

### Step 1: implement the interface

Create a module, for example `myorg/github_tracker.py`:

```python
from dataclasses import dataclass

import requests

from yokai import (
    IssueTracker,
    IssueTrackerError,
    Story,
    get_logger,
)

log = get_logger("adapters.github")


@dataclass
class GitHubTrackerSettings:
    base_url: str
    repo: str
    token: str
    trigger_label: str = "ai-pipeline"
    processing_label: str = "ai-processing"


class GitHubIssuesTracker(IssueTracker):
    def __init__(self, settings: GitHubTrackerSettings):
        self._s = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/vnd.github+json",
            }
        )

    def search_pending_stories(self) -> list[Story]:
        url = f"{self._s.base_url}/repos/{self._s.repo}/issues"
        params = {
            "labels": self._s.trigger_label,
            "state": "open",
        }
        try:
            r = self._session.get(url, params=params, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            raise IssueTrackerError(f"GitHub search failed: {e}") from e

        result = []
        for issue in r.json():
            labels = [lbl["name"] for lbl in issue.get("labels", [])]
            if self._s.processing_label in labels:
                continue
            result.append(
                Story(
                    key=str(issue["number"]),
                    title=issue["title"],
                    description=issue.get("body") or "",
                    labels=labels,
                    url=issue["html_url"],
                    raw=issue,
                )
            )
        return result

    def mark_in_progress(self, story_key: str) -> None:
        self._add_label(story_key, self._s.processing_label)

    def mark_failed(self, story_key: str, reason: str) -> None:
        self.add_comment(story_key, f"Pipeline failed: {reason}")

    def add_comment(self, story_key: str, body: str) -> None:
        url = (
            f"{self._s.base_url}/repos/{self._s.repo}"
            f"/issues/{story_key}/comments"
        )
        try:
            r = self._session.post(url, json={"body": body}, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            raise IssueTrackerError(
                f"Failed to comment on issue {story_key}: {e}"
            ) from e

    def get_story_url(self, story_key: str) -> str:
        return f"https://github.com/{self._s.repo}/issues/{story_key}"

    def _add_label(self, story_key: str, label: str) -> None:
        url = (
            f"{self._s.base_url}/repos/{self._s.repo}"
            f"/issues/{story_key}/labels"
        )
        try:
            r = self._session.post(
                url, json={"labels": [label]}, timeout=15
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise IssueTrackerError(
                f"Failed to label issue {story_key}: {e}"
            ) from e
```

### Step 2: register the builder with the factory

In a startup module that runs before `build_pipeline`, register your
adapter:

```python
from yokai.factory import register_tracker
from myorg.github_tracker import GitHubIssuesTracker, GitHubTrackerSettings


def build_github_tracker(config):
    s = config.issue_tracker
    return GitHubIssuesTracker(
        GitHubTrackerSettings(
            base_url=s.base_url,
            repo=s.project,        # repo name in owner/name form
            token=s.token,
            trigger_label=s.trigger_label,
            processing_label=s.processing_label,
        )
    )


register_tracker("github_issues", build_github_tracker)
```

### Step 3: select it from YAML

```yaml
issue_tracker:
  type: github_issues
  base_url: https://api.github.com
  project: my-org/my-repo
  trigger_label: ai-pipeline
  processing_label: ai-processing
  status: open
  username: ${GITHUB_USERNAME}
  token: ${GITHUB_TOKEN}
```

The other sections of the config remain unchanged.

## Notes for adapter authors

- **Errors**: wrap third-party exceptions in the framework's
  exception types (`IssueTrackerError`, `RepoHostingError`,
  `AgentExecutionError`). The pipeline catches `SpecPipelineError`
  for graceful failure handling.
- **Logging**: use `get_logger("adapters.your_name")`. The framework
  installs a redaction filter that hides registered tokens from log
  output.
- **Tokens**: the factory automatically calls `register_secret()` on
  the issue tracker and repo hosting tokens loaded from config. If
  your adapter holds additional secrets, register them yourself in
  the builder.
- **Testing**: use the `responses` library to mock HTTP. For git
  operations, use a local bare repo as the remote (see
  `tests/integration/test_bitbucket_git.py` for the pattern).
- **No global state**: adapters should be safe to instantiate
  multiple times with different settings. Do not use module-level
  mutable state.

## Adapters that span multiple interfaces

A coding agent that also creates pull requests on a hosted platform
(for instance, an MCP-based agent that internally talks to Bitbucket)
can implement only `CodingAgent` and skip `RepoHosting`. In that case,
provide a no-op `RepoHosting` adapter and let the agent handle the
git work. The pipeline does not enforce a strict ordering inside the
agent step.

## Contributing your adapter back

If your adapter is general enough to be useful to others, consider
contributing it to the main repository. Open an issue first to discuss
the API and the dependency cost of any new third-party libraries.
