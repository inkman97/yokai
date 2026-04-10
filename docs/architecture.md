# Architecture

This document describes the internal structure of yokai. Read it
if you want to understand how the framework is put together, or if you
plan to contribute non-trivial changes.

## Layered structure

```
+------------------------------------------------------------+
|  CLI (cli.py)                                              |
|    yokai run / status / init                       |
+------------------------------------------------------------+
|  Factory (factory.py)                                      |
|    builds a Pipeline from FrameworkConfig                  |
+------------------------------------------------------------+
|  Pipeline orchestrator (core/pipeline.py)                  |
|    process_story, run_once, run_forever                    |
+------------------------------------------------------------+
|  Abstract interfaces (core/interfaces.py)                  |
|    IssueTracker, RepoHosting, CodingAgent, StoryRouter,    |
|    NotificationSink, ExecutionStore                        |
+------------------------------------------------------------+
|  Concrete adapters (adapters/*)                            |
|    JiraDataCenterTracker, BitbucketDataCenterHosting,      |
|    ClaudeCodeAgent                                         |
+------------------------------------------------------------+
```

The pipeline depends only on the interfaces. Adapters depend on the
interfaces but never on each other. The factory is the only place that
knows the mapping from `type` strings to concrete adapter classes.

## The Pipeline class

`Pipeline` (in `core/pipeline.py`) is the orchestrator. Its public
methods are:

- `process_story(story)` — process a single story end to end. Synchronous,
  raises on failure. Used directly in tests and in single-shot scripts.
- `run_once()` — poll the tracker once, submit any new stories to the
  internal pool, return the count submitted. Does not wait.
- `run_forever(stop_condition=None)` — main entry point. Loops forever,
  polling and submitting until interrupted (or until `stop_condition()`
  returns True, which is used by tests).

Internally, `process_story` walks through this sequence:

1. Mark the story as in-progress on the tracker.
2. Resolve the repository slug via the router.
3. Clone or update the repository under the workspace directory.
4. Create a feature branch using the configured branch pattern.
5. Build the prompt for the coding agent.
6. Run the agent in the working tree.
7. Commit changes. If there are no changes, post a notice and stop.
8. Push the branch.
9. Compute the diff stats for the description.
10. Open the pull request.
11. Post two comments back on the story (short link + detailed agent
    output).
12. Mark the story as completed in the execution store.

Hooks are emitted at every transition. Notification sinks are called on
started, succeeded, and failed states.

## Concurrency model

The orchestrator processes stories in parallel using a
`ThreadPoolExecutor`. Two concerns drive the design:

**Per-repo locking**

Two stories that both target the same repository must not run at the
same time. They share a working tree, and the second one would either
see the first one's branch state or corrupt it. Each repository slug
has its own `threading.Lock` in the `RepoLockRegistry`. A worker
acquires the lock before any git operation and holds it until the PR
is opened (or the story fails). Stories on different repositories see
different locks and proceed in parallel.

**In-flight deduplication**

The polling loop runs every `poll_interval_seconds`. Between the
moment a story is submitted to the pool and the moment the tracker
sees the "processing" label update, the polling loop might pick the
same story up again. The `InFlightRegistry` is a thread-safe set of
story keys currently being processed. The polling loop atomically
calls `try_mark` and only submits stories where it returns True. The
worker calls `unmark` in its `finally` clause, regardless of success
or failure.

These two mechanisms are independent. A story can be in-flight without
holding the repo lock yet (it is queued in the executor), and a worker
can hold the repo lock long after the story has been removed from the
in-flight set (it never is, in practice, but the design makes the
boundary explicit).

## Hook system

`HookRegistry` (in `core/hooks.py`) is a simple event dispatcher. Plugins
register callbacks for named events. The pipeline emits events at
fixed lifecycle points.

A hook callback receives a single dict payload. The keys depend on the
event. See the README for the table.

Hooks are best-effort. A hook that raises an exception is logged and
the next hook for the same event is still called. The pipeline is never
interrupted by a hook failure. This is a deliberate trade-off: it makes
plugin authors responsible for their own errors, but ensures that a
buggy third-party plugin can never break the main flow.

## Storage

`ExecutionStore` is the interface for persistent state. Two
implementations:

- `InMemoryExecutionStore` — for tests and ephemeral runs. State is lost
  when the process exits.
- `SqliteExecutionStore` — single-file SQLite database. Survives
  restarts. Thread-safe via a single lock and `check_same_thread=False`.
  Atomic mark_in_flight via INSERT ... ON CONFLICT.

The store is queried by `yokai status` and updated by the
pipeline at the end of each story (success or failure).

## Configuration

`FrameworkConfig` (in `core/config.py`) is a typed representation of
the YAML file. The loader expands `${VAR_NAME}` references against
environment variables and validates that all required keys are present.

The factory `build_pipeline(config)` is the only consumer of
`FrameworkConfig`. It looks up the right adapter builder for each
`type` field and constructs the pipeline.

## Logging and secrets

All loggers under the `yokai` namespace go through a custom
filter that redacts known secrets. Tokens are registered with
`register_secret(value)` at startup, typically inside the factory.
Once registered, any log message containing the secret has it replaced
with `***REDACTED***` before reaching any handler.

This is global and best-effort: a secret that contains substrings of
benign text could trigger false positives, and a token printed via
`print()` instead of the logger is not redacted. Always log through
`get_logger("module.name")`.

## Testing strategy

The test suite has three layers:

1. **Pure unit tests** for value-only modules (models, formatters,
   routers, branch naming, config parser). No I/O.
2. **Mocked HTTP tests** for the Jira and Bitbucket adapters using the
   `responses` library. These verify URL construction, payload shape,
   header handling, and error mapping without any network.
3. **Real git integration tests** for the Bitbucket adapter against a
   local bare git repository. These cover clone, branch, commit, push,
   and diff stat parsing end to end. They do not need any Bitbucket
   server or network access.

Pipeline-level tests use fake in-memory implementations of the
interfaces (`tests/unit/fakes.py`). These exercise the real `Pipeline`
class against fakes, including the parallelism logic with measurable
timing assertions.

The test suite has 187 tests at the time of writing and runs in about
14 seconds.
