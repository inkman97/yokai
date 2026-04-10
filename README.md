# yokai

> In Japanese folklore, a yokai is a spirit that operates in the
> background of the human world, often working at night, sometimes
> mischievous and sometimes helpful. This framework is the helpful
> kind: it watches your backlog while you sleep and leaves pull
> requests waiting for you in the morning.

A Python framework for **spec-driven development pipelines**: turn issue
tracker stories into pull requests automatically, using a coding agent
of your choice.

```
+-------------+      +----------+      +---------------+      +---------------+
|   Jira      | ---> | Router   | ---> |  Claude Code  | ---> |  Bitbucket    |
|  (story)    |      |          |      |  (agent)      |      |  (pull req)   |
+-------------+      +----------+      +---------------+      +---------------+
```

yokai polls your issue tracker for stories tagged with a configurable
trigger label, routes each story to its target repository, runs a coding
agent inside the local working tree, then commits, pushes, and opens a
pull request. It posts the result back as comments on the original
story so the human reviewer has full context.

## Why this exists

Several commercial offerings cover the same workflow, but they all
target cloud SaaS deployments (Jira Cloud, Bitbucket Cloud, GitHub).
**yokai is the first open-source framework targeted at
on-premise enterprise environments**: Jira Data Center and Bitbucket
Data Center behind firewalls and SSO, where cloud connectors do not
work.

It is designed to be runnable from a developer laptop, with no
infrastructure requirements beyond Python 3.10+, git, and the chosen
coding agent CLI.

## Status

Early alpha. The core orchestrator, the Jira Data Center and Bitbucket
Data Center adapters, and the Claude Code adapter are working and
tested. The API is unstable and may change.

## Features

- Provider-agnostic core: swap any of the issue tracker, repo hosting,
  coding agent, router, or storage by implementing a small interface.
- Built-in adapters for Jira Data Center, Bitbucket Data Center, and
  Claude Code CLI.
- Parallel processing with per-repository locking: stories on different
  repos run concurrently, stories on the same repo serialize.
- In-flight deduplication: a story is never picked up twice while it
  is being processed, even if the issue tracker label update is
  delayed.
- Plugin system with lifecycle hooks: register callbacks for events
  like `after_agent_run` or `on_failure` without forking the framework.
- Persistent execution state via SQLite, surviving process restarts.
- Notification sinks (logger, Slack webhook, custom).
- Token redaction in all log output.
- Idempotent commands and safe failure recovery.

## Quickstart

### 1. Install

```bash
pip install yokai
```

You also need:
- Python 3.10 or later
- git
- The CLI of your chosen coding agent (e.g. Claude Code:
  `npm install -g @anthropic-ai/claude-code`)

### 2. Generate a starter config

```bash
yokai init --output config.yaml
```

Edit `config.yaml` and fill in your Jira and Bitbucket details.
Tokens should be passed via environment variables and referenced as
`${VAR_NAME}` in the file.

### 3. Set credentials

```bash
export JIRA_USERNAME=your.username
export JIRA_TOKEN=your-jira-personal-access-token
export BITBUCKET_USERNAME=your.username
export BITBUCKET_TOKEN=your-bitbucket-http-access-token
```

The Bitbucket token must have **repository write** permission. Read-only
tokens will fail at the push step.

### 4. Tag a story and run

In Jira, add the label `ai-pipeline` to a story in the Backlog status.
Make sure the story has a component that matches one of the entries
in your `routing.components` map, or add a label like `repo:my-repo`.

Then run the orchestrator:

```bash
yokai run --config config.yaml
```

It will poll Jira every 30 seconds. When it sees the labelled story, it
clones the target repo, runs Claude Code, opens a pull request, and
posts two comments back on the Jira story (a short link comment and a
detailed agent output comment).

### 5. Inspect history

```bash
yokai status --config config.yaml
```

Shows the most recent story executions stored in the SQLite state
database, with their status and pull request URL.

## Architecture

The core of the framework is a small set of abstract interfaces:

| Interface | Responsibility | Built-in implementation |
|---|---|---|
| `IssueTracker` | search, comment, label stories | `JiraDataCenterTracker` |
| `RepoHosting` | clone, branch, commit, push, open PR | `BitbucketDataCenterHosting` |
| `CodingAgent` | run an AI agent in a working tree | `ClaudeCodeAgent` |
| `StoryRouter` | resolve a story to a repository | `ComponentMapRouter`, `LabelPrefixRouter`, `ChainRouter` |
| `NotificationSink` | post events to humans | `LoggerNotificationSink`, `SlackWebhookSink` |
| `ExecutionStore` | persist execution state | `InMemoryExecutionStore`, `SqliteExecutionStore` |

The `Pipeline` class depends only on these interfaces. Concrete adapters
are constructed by the `factory.build_pipeline(config)` function from a
`FrameworkConfig` loaded from YAML.

To add support for a different system (GitHub Issues, GitLab, Linear,
Aider, OpenCode, etc.), implement the relevant interface and register
the new builder. See `docs/writing_an_adapter.md`.

### Concurrency

The orchestrator uses a `ThreadPoolExecutor` to process multiple stories
in parallel up to `max_parallel_stories`. To prevent two stories from
trampling each other's working tree on the same repo, each repository
has its own lock. Two stories on different repositories run truly in
parallel; two stories on the same repo serialize through the lock.

A separate in-flight registry tracks stories that have been submitted
to the pool but have not yet had their tracker label updated, so the
polling loop never submits the same story twice.

### Hooks

The pipeline emits 9 lifecycle events. Plugins register callbacks for
the events they care about. A failing callback never breaks the
pipeline, only logs the exception.

| Event | When it fires | Payload keys |
|---|---|---|
| `before_process` | Story acquired by worker | `story` |
| `after_resolve_repo` | Repository resolved | `story`, `repo_slug` |
| `after_clone` | Working tree ready | `story`, `repo_path` |
| `before_agent_run` | About to invoke agent | `story`, `repo_path`, `prompt` |
| `after_agent_run` | Agent finished | `story`, `agent_result` |
| `after_commit` | Local commit created | `story`, `commit` |
| `after_push` | Branch pushed | `story`, `branch_name` |
| `after_pull_request` | Pull request opened | `story`, `pull_request` |
| `on_success` | Full flow succeeded | `story`, `pull_request` |
| `on_failure` | Any error in the flow | `story`, `error` |

See `examples/example_plugin.py` for a working plugin.

## Configuration reference

The full configuration is a single YAML file. See
`examples/enterprise_data_center.yaml` for an annotated example.

Sections:

- **`issue_tracker`** — connection and filtering for the issue source
- **`repo_hosting`** — connection and branch policy for the repo host
- **`agent`** — coding agent command and timeouts
- **`routing`** — how to resolve stories to repositories
- **`orchestrator`** — polling and parallelism settings
- **`storage`** — execution state persistence (memory or sqlite)
- **`plugins`** — list of dotted import paths to plugin install
  functions

Environment variable references like `${VAR_NAME}` are expanded at load
time. Missing variables raise a clear configuration error.

## Development

Clone the repo and install in editable mode with dev extras:

```bash
git clone https://github.com/your-org/yokai
cd yokai
pip install -e .[dev]
```

Run the test suite:

```bash
pytest
```

The test suite has unit tests with HTTP mocking for the Jira and
Bitbucket adapters, parallelism tests using fake in-memory adapters,
and an integration test that exercises real git operations against a
local bare repository (no network needed).

## Contributing

This project is maintained as a side effort. Contributions are welcome,
especially:

- Additional issue tracker adapters (Jira Cloud, Linear, GitHub Issues)
- Additional repo hosting adapters (GitHub, GitLab, Bitbucket Cloud)
- Additional coding agent adapters (Aider, OpenCode, Cursor CLI)
- Bug reports from real on-premise enterprise deployments
- Improvements to documentation

Please open an issue first if you plan a substantial change.

## License

MIT
