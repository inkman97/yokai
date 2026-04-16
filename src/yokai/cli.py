"""Command-line interface for yokai.

Subcommands:
- run            : start the legacy monolithic orchestrator (single process)
- coordinator    : start the async coordinator (polls tracker, enqueues jobs)
- worker         : start an async worker (dequeues jobs, runs agent)
- result-handler : start the result handler (postprocesses agent results into PRs)
- queue-status   : show the current queue state
- queue-retry    : requeue a dead-lettered or failed job by id
- status         : show recent story executions from the legacy store
- init           : print a starter YAML config to stdout
"""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from yokai import __version__
from yokai.core.config import load_config
from yokai.core.exceptions import (
    ConfigurationError,
    SpecPipelineError,
)
from yokai.core.logging_setup import configure_logging, get_logger
from yokai.factory import build_pipeline
from yokai.storage.sqlite_store import SqliteExecutionStore

log = get_logger("cli")


STARTER_CONFIG = """\
# yokai configuration
# Replace the placeholders below with your real values, or set them
# via environment variables and reference them as ${VAR_NAME}.

issue_tracker:
  type: jira_dc
  base_url: https://jira.example.com
  project: PROJ
  trigger_label: ai-pipeline
  processing_label: ai-processing
  status: Backlog
  account: ${JIRA_USERNAME}
  token: ${JIRA_TOKEN}

repo_hosting:
  type: bitbucket_dc
  base_url: https://code.example.com
  namespace: myproj
  account: ${BITBUCKET_USERNAME}
  token: ${BITBUCKET_TOKEN}
  default_branch: master
  branch_pattern: "feature/{issue_key}-ai-{timestamp}"

agent:
  type: claude_code
  command: claude
  flags:
    - --print
    - --dangerously-skip-permissions
  timeout_seconds: 1800

routing:
  type: component_map
  components:
    BACKEND: my-backend-repo
    FRONTEND: my-frontend-repo
  label_prefix: "repo:"

orchestrator:
  poll_interval_seconds: 30
  max_parallel_stories: 4
  workspace_dir: ~/yokai-workspace

storage:
  type: sqlite
  path: ~/.yokai/state.db

# Optional: enables async coordinator/worker mode.
# Omit this block to use only the legacy `yokai run` monolithic mode.
queue:
  backend: sqlite       # sqlite | memory | redis
  db_path: ~/.yokai/queue.db
  # For backend: redis, set redis_url instead of db_path:
  # redis_url: redis://localhost:6379/0
  # redis_url: redis://:password@redis.example.com:6379/0
  coordinator:
    poll_interval_seconds: 30
    lease_duration_seconds: 90
    reclaim_interval_seconds: 60
    max_attempts_per_job: 3
  worker:
    poll_interval_seconds: 2
    agent_timeout_seconds: 1800
    lease_duration_seconds: 1800
    heartbeat_interval_seconds: 15
    retry_backoff_base_seconds: 5
    retry_backoff_cap_seconds: 300
  result_handler:
    poll_interval_seconds: 5
    batch_size: 10

plugins: []
"""


def cmd_run(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = load_config(args.config)
        pipeline = build_pipeline(config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    except SpecPipelineError as e:
        print(f"Initialization error: {e}", file=sys.stderr)
        return 3

    pipeline.run_forever()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = load_config(args.config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    if config.storage.type != "sqlite":
        print(
            "Status is only available with storage.type = sqlite",
            file=sys.stderr,
        )
        return 2
    if not config.storage.path:
        print("storage.path is not configured", file=sys.stderr)
        return 2

    store = SqliteExecutionStore(config.storage.path)
    try:
        records = store.list_recent(limit=args.limit)
    finally:
        store.close()

    if not records:
        print("No story executions recorded yet.")
        return 0

    print(f"{'Story':<20} {'Status':<12} {'Started at':<28} {'PR':<50}")
    print("-" * 110)
    for r in records:
        story = r.get("story_key") or "?"
        status = r.get("status") or "?"
        started = r.get("started_at") or ""
        pr = r.get("pr_url") or r.get("error") or ""
        print(f"{story:<20} {status:<12} {started:<28} {pr:<50}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    if args.output:
        path = Path(args.output)
        if path.exists() and not args.force:
            print(
                f"Refusing to overwrite {path} (use --force)", file=sys.stderr
            )
            return 1
        path.write_text(STARTER_CONFIG)
        print(f"Wrote starter config to {path}")
    else:
        sys.stdout.write(STARTER_CONFIG)
    return 0

def _load_async_config(args):
    """Load config and verify the queue section is present."""
    from yokai.async_factory import build_coordinator  # noqa: F401

    config = load_config(args.config)
    if config.queue is None:
        raise ConfigurationError(
            "queue section missing in config; required for async commands. "
            "Run `yokai init` to see an example."
        )
    return config


def _install_signal_handlers(component) -> None:
    """SIGTERM/SIGINT -> component.stop()."""
    def handler(signum, frame):
        log.info(f"Received signal {signum}, requesting graceful shutdown")
        component.stop()

    try:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    except ValueError:
        # Not in main thread (some test runners). Ignore.
        pass


def cmd_coordinator(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = _load_async_config(args)
        from yokai.async_factory import build_coordinator
        coordinator = build_coordinator(config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    _install_signal_handlers(coordinator)
    log.info(f"Starting coordinator {coordinator.coordinator_id}")
    coordinator.run()
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = _load_async_config(args)
        from yokai.async_factory import build_worker
        worker = build_worker(config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    _install_signal_handlers(worker)
    log.info(f"Starting worker {worker.worker_id}")
    worker.run()
    return 0


def cmd_result_handler(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = _load_async_config(args)
        from yokai.async_factory import build_result_handler
        handler = build_result_handler(config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    _install_signal_handlers(handler)
    log.info("Starting result handler")
    handler.run()
    return 0


def cmd_queue_status(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = _load_async_config(args)
        from yokai.async_factory import build_backend_only
        backend = build_backend_only(config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    from yokai.queue.models import JobStatus
    from datetime import timedelta

    print("=" * 60)
    print("Job counts by status")
    print("=" * 60)
    stats = backend.stats()
    total = sum(stats.values())
    for status in JobStatus:
        n = stats[status]
        if n > 0 or status in (
            JobStatus.QUEUED,
            JobStatus.PICKED_UP,
            JobStatus.AGENT_RUNNING,
            JobStatus.AGENT_COMPLETED,
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
        ):
            print(f"  {status.value:<20} {n}")
    print(f"  {'TOTAL':<20} {total}")
    print()

    # Live workers
    print("=" * 60)
    print("Workers (last heartbeat within 60s)")
    print("=" * 60)
    alive = backend.list_alive(timedelta(seconds=60))
    if not alive:
        print("  (none)")
    else:
        for w in alive:
            cj = w.current_job_id or "(idle)"
            print(f"  {w.worker_id:<40} job={cj}")
    print()

    # Recent failures and dead-letters
    print("=" * 60)
    print(f"Most recent {args.limit} dead-lettered jobs")
    print("=" * 60)
    dead = backend.list_by_status(JobStatus.DEAD_LETTERED, limit=args.limit)
    if not dead:
        print("  (none)")
    else:
        for j in dead:
            err = (j.last_error or "")[:80]
            print(f"  {j.story_key:<15} {j.job_id[:8]} attempts={j.attempts} {err}")
    return 0


def cmd_queue_retry(args: argparse.Namespace) -> int:
    configure_logging(args.log_level)
    try:
        config = _load_async_config(args)
        from yokai.async_factory import build_backend_only
        backend = build_backend_only(config)
    except ConfigurationError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    from yokai.queue.exceptions import (
        InvalidStateTransition,
        JobNotFound,
    )
    from yokai.queue.models import JobStatus

    try:
        job = backend.get(args.job_id)
    except JobNotFound:
        print(f"Job not found: {args.job_id}", file=sys.stderr)
        return 1

    print(f"Job {job.job_id}")
    print(f"  story_key: {job.story_key}")
    print(f"  status:    {job.status.value}")
    print(f"  attempts:  {job.attempts}/{job.max_attempts}")
    print(f"  last_error: {(job.last_error or '')[:200]}")
    print()

    if job.status not in (
        JobStatus.DEAD_LETTERED,
        JobStatus.FAILED,
    ):
        print(
            f"Refusing to retry: job is in status {job.status.value}, "
            f"only DEAD_LETTERED or FAILED can be retried.",
            file=sys.stderr,
        )
        return 1

    # Reset attempts so the worker has the full retry budget again
    print("Requeueing...")
    # Direct DB-level reset; the state machine does not allow
    # DEAD_LETTERED/FAILED -> QUEUED, so we use a lower-level approach:
    # we delete and re-enqueue with a fresh job_id and status.
    # Simpler: since both are terminal, re-enqueue under a new job.
    from yokai.queue.models import Job

    new_job = Job.new(
        story_key=job.story_key,
        repo_slug=job.repo_slug,
        payload=job.payload,
        max_attempts=job.max_attempts,
    )
    try:
        backend.enqueue(new_job)
    except Exception as e:
        print(f"Re-enqueue failed: {e}", file=sys.stderr)
        return 1

    print(f"Re-enqueued as new job: {new_job.job_id}")
    return 0



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yokai",
        description=(
            "Spec-driven development pipeline: from issue tracker stories "
            "to pull requests via coding agents."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"yokai {__version__}"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the polling orchestrator")
    p_run.add_argument("--config", "-c", required=True, help="Path to YAML config")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Show recent story executions")
    p_status.add_argument("--config", "-c", required=True, help="Path to YAML config")
    p_status.add_argument("--limit", type=int, default=20)
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser("init", help="Print a starter YAML config")
    p_init.add_argument(
        "--output", "-o", help="Write to file instead of stdout"
    )
    p_init.add_argument(
        "--force", action="store_true", help="Overwrite existing file"
    )
    p_init.set_defaults(func=cmd_init)

    p_coord = sub.add_parser(
        "coordinator",
        help="Start the async coordinator (polls tracker, enqueues jobs)",
    )
    p_coord.add_argument(
        "--config", "-c", required=True, help="Path to YAML config"
    )
    p_coord.set_defaults(func=cmd_coordinator)

    p_worker = sub.add_parser(
        "worker",
        help="Start an async worker (dequeues jobs, runs agent)",
    )
    p_worker.add_argument(
        "--config", "-c", required=True, help="Path to YAML config"
    )
    p_worker.set_defaults(func=cmd_worker)

    p_rh = sub.add_parser(
        "result-handler",
        help="Start the result handler (postprocesses agent results into PRs)",
    )
    p_rh.add_argument(
        "--config", "-c", required=True, help="Path to YAML config"
    )
    p_rh.set_defaults(func=cmd_result_handler)

    p_qs = sub.add_parser(
        "queue-status",
        help="Show the current queue state (counts, workers, dead-letters)",
    )
    p_qs.add_argument(
        "--config", "-c", required=True, help="Path to YAML config"
    )
    p_qs.add_argument("--limit", type=int, default=20)
    p_qs.set_defaults(func=cmd_queue_status)

    p_qr = sub.add_parser(
        "queue-retry",
        help="Re-enqueue a dead-lettered or failed job by id",
    )
    p_qr.add_argument(
        "--config", "-c", required=True, help="Path to YAML config"
    )
    p_qr.add_argument("job_id", help="The job id to retry")
    p_qr.set_defaults(func=cmd_queue_retry)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
