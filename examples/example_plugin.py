"""Example plugin demonstrating how to extend yokai.

A plugin is a single callable that takes a Pipeline as its only
argument. Inside, it can register hook callbacks, attach notification
sinks, or wrap any of the pipeline's components.

To enable this plugin, add to your YAML config:

    plugins:
      - examples.example_plugin:install

The framework will import the module and call install(pipeline) at
startup, after the pipeline is built and before run_forever begins.
"""

from __future__ import annotations

from yokai import Pipeline, get_logger

log = get_logger("plugin.example")


def install(pipeline: Pipeline) -> None:
    hooks = pipeline._hooks

    def on_agent_done(payload: dict) -> None:
        story = payload["story"]
        result = payload["agent_result"]
        log.info(
            f"[example-plugin] {story.key} agent done in "
            f"{result.duration_seconds:.1f}s"
        )

    def on_pr_opened(payload: dict) -> None:
        story = payload["story"]
        pr = payload["pull_request"]
        log.info(f"[example-plugin] PR opened for {story.key}: {pr.url}")

    hooks.register("after_agent_run", on_agent_done)
    hooks.register("after_pull_request", on_pr_opened)
    log.info("example_plugin installed")
