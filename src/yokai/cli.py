"""Command-line interface for yokai.

Subcommands:
- run    : start the orchestrator polling loop
- status : show recent story executions from the store
- init   : print a starter YAML config to stdout
"""

from __future__ import annotations

import argparse
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
  username: ${JIRA_USERNAME}
  token: ${JIRA_TOKEN}

repo_hosting:
  type: bitbucket_dc
  base_url: https://code.example.com
  project_key: myproj
  username: ${BITBUCKET_USERNAME}
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
