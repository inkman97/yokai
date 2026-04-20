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
**yokai started as the first open-source framework targeted at
on-premise enterprise environments** (Jira Data Center and Bitbucket
Data Center behind firewalls and SSO, where cloud connectors do not
reach), and now also ships adapters for Atlassian Cloud
(Jira Cloud and Bitbucket Cloud) so the same framework can drive
hybrid setups.

It is designed to be runnable from a developer laptop, with no
infrastructure requirements beyond Python 3.10+, git, and the chosen
coding agent CLI.

## Status

Early alpha. The core orchestrator, the Jira Data Center, Bitbucket
Data Center, Jira Cloud, and Bitbucket Cloud adapters, and the
Claude Code adapter are working and tested. Since 0.2.0 an optional
async mode is available with SQLite and Redis backends. The API is
unstable and may change.

## Features

- Provider-agnostic core: swap any of the issue tracker, repo hosting,
  coding agent, router, or storage by implementing a small interface.
- Built-in adapters for Jira Data Center, Bitbucket Data Center,
  Jira Cloud, Bitbucket Cloud, and Claude Code CLI.
- Two deployment modes: simple monolithic (`yokai run`) and scaled
  async (coordinator + worker(s) + result-handler) sharing a
  persistent queue.
- Pluggable queue backends (in-memory, SQLite, Redis) for the async
  mode, so a single laptop setup and a multi-host production cluster
  use the same code.
- Parallel processing with per-repository locking: stories on different
  repos run concurrently, stories on the same repo serialize.
- In-flight deduplication: a story is never picked up twice while it
  is being processed, even if the issue tracker label update is
  delayed.
- Automatic retry with exponential backoff and dead-letter queue for
  jobs that exceed `max_attempts`.
- Plugin system with lifecycle hooks: register callbacks for events
  like `after_agent_run` or `on_failure` without forking the framework.
- Persistent execution state via SQLite, surviving process restarts.
- Notification sinks (logger, Slack webhook, custom).
- Token redaction in all log output, including credentials embedded in
  Bitbucket Cloud clone URLs.
- Idempotent commands and safe failure recovery.
- **PR review rework loop** (async mode): when a reviewer leaves
  comments on a yokai-generated PR, add the `ai-rework` label on
  Jira and yokai will read the review comments, fix the code, and
  push to the same branch — the PR updates automatically.
- **Markdown to Jira wiki markup converter**: agent output (written
  in Markdown by Claude) is automatically converted to Jira Data
  Center wiki markup so comments render with proper headers, tables,
  bold, and code blocks.

## Deployment modes

yokai can run in two modes, chosen via config:

### Monolithic mode: `yokai run`

One process polls the tracker, runs the agent, opens PRs. This is the
simplest setup and has been the default since version 0.1. It is still
the recommended mode for single-developer laptop use.

### Async mode: `yokai coordinator` + `yokai worker` + `yokai result-handler`

Three roles, three processes, a persistent queue in between. Added in
0.2.0. Use this when you want:

- **Resilience**: jobs survive process crashes (the queue persists).
- **Scale**: run multiple workers in parallel, on the same host or
  on different hosts.
- **Separation of concerns**: polling, agent execution, and PR
  creation can be monitored and restarted independently.

```
+---------------+    enqueue    +-----------------+
| Coordinator   | ------------> |  Job Queue      |
| (polls Jira)  |               |  (SQLite/Redis) |
+---------------+               |                 |
                                |                 |
+---------------+    dequeue    |                 |
| Worker(s)     | <------------ |                 |
| (run agent)   |               |                 |
+---------------+    write      |                 |
       |          ------------> |  Result Store   |
       v                        |                 |
+---------------+    read       |                 |
| ResultHandler | <------------ |                 |
| (commit + PR) |               +-----------------+
+---------------+
```

Backends for the queue:

- **SQLite** (default): single file, no external services, good for
  single-host deployments.
- **Redis**: multi-host, production-grade. Install with the `[redis]`
  extra.
- **In-memory**: tests and experiments only.

See [`docs/async_mode.md`](docs/async_mode.md) for the full operational
guide.

## Quickstart

### 1. Install

```bash
pip install yokai-cli
# or with Redis support:
pip install yokai-cli[redis]
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

Then start yokai. Pick one mode:

**Monolithic (simplest)**:

```bash
yokai run --config config.yaml
```

**Async on one host (more resilient)**:

```bash
# in three separate terminals
yokai coordinator    --config config.yaml
yokai worker         --config config.yaml
yokai result-handler --config config.yaml
```

Either way, it polls Jira, clones the target repo, runs Claude Code,
opens a pull request, and posts two comments back on the Jira story.

### 5. Inspect history

```bash
yokai status --config config.yaml        # legacy SQLite execution store
yokai queue-status --config config.yaml  # async queue state (jobs, workers, dead-letters)
```

## PR review rework loop

When a reviewer leaves comments on a yokai-generated pull request,
yokai can automatically fix the code based on the review feedback.

### How it works

1. yokai processes a story and opens a PR (the normal flow).
2. A reviewer comments on the PR in Bitbucket.
3. Someone adds the label `ai-rework` to the Jira story.
4. The coordinator picks it up, finds the open PR on Bitbucket by
   matching the story key in the branch name, and reads all review
   comments.
5. The worker checks out the **existing branch** (no new branch),
   runs Claude Code with a rework-specific prompt that includes
   every review comment with file path and line number, then commits
   and pushes to the same branch.
6. The PR updates automatically with the new commit.
7. yokai removes `ai-rework` and restores `ai-done` on Jira.

The rework loop can be repeated: if the reviewer leaves more comments,
add `ai-rework` again.

### Label flow

```
ai-pipeline → ai-processing → ai-done
                                  ↓  (reviewer adds ai-rework)
                            ai-rework → ai-processing → ai-done
                                                            ↓  (repeat if needed)
                                                      ai-rework → ...
```

### Configuration

The rework label is configurable (default `ai-rework`):

```yaml
issue_tracker:
  rework_label: ai-rework
```

## Architecture

The core of the framework is a small set of abstract interfaces:

| Interface | Responsibility | Built-in implementation |
|---|---|---|
| `IssueTracker` | search, comment, label stories | `JiraDataCenterTracker`, `JiraCloudTracker` |
| `RepoHosting` | clone, branch, commit, push, open PR | `BitbucketDataCenterHosting`, `BitbucketCloudHosting` |
| `CodingAgent` | run an AI agent in a working tree | `ClaudeCodeAgent` |
| `StoryRouter` | resolve a story to a repository | `ComponentMapRouter`, `LabelPrefixRouter`, `ChainRouter` |
| `NotificationSink` | post events to humans | `LoggerNotificationSink`, `SlackWebhookSink` |
| `ExecutionStore` | persist story execution state (legacy mode) | `InMemoryExecutionStore`, `SqliteExecutionStore` |

The monolithic `Pipeline` depends only on these interfaces. Concrete
adapters are constructed by `factory.build_pipeline(config)` from a
`FrameworkConfig` loaded from YAML.

The async mode adds four more interfaces in `yokai.queue`:

| Interface | Responsibility | Built-in implementation |
|---|---|---|
| `JobQueue` | enqueue, dequeue (with lease), update status | `InMemoryBackend`, `SqliteBackend`, `RedisBackend` |
| `ResultStore` | store and retrieve agent results | same as above |
| `WorkerRegistry` | track live workers via heartbeats | same as above |
| `CoordinatorLock` | leader-election lock for coordinator HA | same as above |

These are wrapped around the existing adapters by the
`yokai.queue_adapters` bridge layer, so async mode automatically
supports every combination (Jira DC/Cloud x Bitbucket DC/Cloud) that
legacy mode supports.

To add support for a different system (GitHub Issues, GitLab, Linear,
Aider, OpenCode, etc.), implement the relevant interface and register
the new builder. See `docs/writing_an_adapter.md`.

### Concurrency

**Monolithic mode** uses a `ThreadPoolExecutor` to process multiple
stories in parallel up to `max_parallel_stories`. To prevent two stories
from trampling each other's working tree on the same repo, each
repository has its own lock. Two stories on different repositories run
truly in parallel; two stories on the same repo serialize through the
lock.

A separate in-flight registry tracks stories that have been submitted
to the pool but have not yet had their tracker label updated, so the
polling loop never submits the same story twice.

**Async mode** achieves parallelism by running multiple worker
processes. The queue backend handles the mutual exclusion atomically:
a job is dequeued exactly once, and the coordinator re-queues it only
if the worker's lease expires. Dedup of in-flight stories is done at
the queue level via per-story keys.

### Hooks

The monolithic pipeline emits 9 lifecycle events. Plugins register
callbacks for the events they care about. A failing callback never
breaks the pipeline, only logs the exception.

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

See `examples/example_plugin.py` for a working plugin. Since 0.2.0,
hooks are emitted in both monolithic and async modes. Plugins written
against the legacy `Pipeline` (using `pipeline._hooks.register(...)`)
work unchanged in async mode thanks to a compatibility shim in
`async_factory` - they receive a small object with `._hooks` just
like a real Pipeline.

## Configuration reference

The full configuration is a single YAML file. See
`examples/enterprise_data_center.yaml` for an annotated example of the
legacy monolithic mode.

Sections:

- **`issue_tracker`** - connection and filtering for the issue source
- **`repo_hosting`** - connection and branch policy for the repo host
- **`agent`** - coding agent command and timeouts
- **`routing`** - how to resolve stories to repositories
- **`orchestrator`** - polling and parallelism settings (monolithic mode)
- **`storage`** - execution state persistence for monolithic mode
  (memory or sqlite)
- **`queue`** - optional. Enables async mode. Fields: `backend`
  (sqlite/memory/redis), `db_path`, `redis_url`, and sub-sections for
  `coordinator`, `worker`, `result_handler`. Omit this section to
  keep only the monolithic `yokai run` mode.
- **`plugins`** - list of dotted import paths to plugin install
  functions

Environment variable references like `${VAR_NAME}` are expanded at load
time. Missing variables raise a clear configuration error.

## CLI reference

| Command | Mode | What it does |
|---|---|---|
| `yokai init` | - | Write a starter YAML to stdout or a file |
| `yokai run` | monolithic | Run the single-process polling orchestrator |
| `yokai status` | monolithic | List recent executions from the SQLite store |
| `yokai coordinator` | async | Poll the tracker and enqueue jobs |
| `yokai worker` | async | Dequeue and run the coding agent |
| `yokai result-handler` | async | Commit, push, open PR, comment |
| `yokai queue-status` | async | Show queue counts, live workers, dead-letters |
| `yokai queue-retry <job-id>` | async | Re-enqueue a dead-lettered or failed job |

## Development

Clone the repo and install in editable mode with dev extras:

```bash
git clone https://github.com/inkman97/yokai
cd yokai
pip install -e ".[dev,redis]"
```

Run the test suite:

```bash
pytest
```

The test suite (~680 tests) has unit tests with HTTP mocking for the
Jira and Bitbucket adapters, parallelism tests using fake in-memory
adapters, an integration test that exercises real git operations
against a local bare repository (no network needed), a full
contract test suite for the three queue backends (in-memory, SQLite,
Redis via `fakeredis`), and end-to-end tests for the PR review
rework loop.

## Contributing

This project is maintained as a side effort. Contributions are welcome,
especially:

- Additional issue tracker adapters (Linear, GitHub Issues)
- Additional repo hosting adapters (GitHub, GitLab)
- Additional coding agent adapters (Aider, OpenCode, Cursor CLI)
- Additional queue backends (RabbitMQ, PostgreSQL)
- Bug reports from real on-premise enterprise deployments
- Improvements to documentation

Please open an issue first if you plan a substantial change.

## License

MIT
