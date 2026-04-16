"""The ResultHandler.

Polls the ResultStore for results whose corresponding job is in state
AGENT_COMPLETED and runs the Postprocessor on each. Translates the
postprocessor outcome into the final job state:

  AGENT_COMPLETED + success -> POSTPROCESSING -> DONE
  AGENT_COMPLETED + failure -> FAILED   (no retry; the agent already
                                         did its work, retry would
                                         duplicate side effects)

The result handler runs in its own loop, like the coordinator and
worker. It can run as a thread inside the coordinator process or as
a standalone process. The default deployment runs it inside the
coordinator (one less process to manage).

Multiple result handlers can run safely - each call to
pending_for_postprocessing returns the same set of jobs, but the
update_status to POSTPROCESSING acts as a claim. The first to claim
wins; the others see InvalidStateTransition and skip.
"""

from __future__ import annotations

import logging
import threading
import traceback
from dataclasses import dataclass

from yokai.queue.exceptions import InvalidStateTransition
from yokai.queue.interfaces import JobQueue, ResultStore
from yokai.queue.models import JobStatus
from yokai.queue.postprocessor import Postprocessor


log = logging.getLogger("yokai.result_handler")


@dataclass
class ResultHandlerSettings:
    poll_interval_seconds: float = 5.0
    batch_size: int = 10


@dataclass
class HandlerCycleStats:
    fetched: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0  # claimed by another handler
    errors: int = 0


class ResultHandler:
    def __init__(
        self,
        *,
        queue: JobQueue,
        results: ResultStore,
        postprocessor: Postprocessor,
        settings: ResultHandlerSettings | None = None,
    ) -> None:
        self._queue = queue
        self._results = results
        self._postprocessor = postprocessor
        self._settings = settings or ResultHandlerSettings()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("ResultHandler starting")
        try:
            while not self._stop_event.is_set():
                self.run_once()
                self._stop_event.wait(self._settings.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("ResultHandler interrupted")
        finally:
            log.info("ResultHandler exiting")

    def run_once(self) -> HandlerCycleStats:
        stats = HandlerCycleStats()
        try:
            results = self._results.pending_for_postprocessing(
                limit=self._settings.batch_size
            )
        except Exception:
            log.exception("pending_for_postprocessing failed")
            stats.errors += 1
            return stats

        stats.fetched = len(results)
        for result in results:
            self._handle_one(result, stats)

        if stats.fetched > 0:
            log.info(
                f"ResultHandler cycle: fetched={stats.fetched} "
                f"succeeded={stats.succeeded} failed={stats.failed} "
                f"skipped={stats.skipped} errors={stats.errors}"
            )
        return stats

    def _handle_one(self, result, stats: HandlerCycleStats) -> None:
        # 1) Fetch the corresponding Job
        try:
            job = self._queue.get(result.job_id)
        except Exception:
            log.exception(f"get({result.job_id}) failed")
            stats.errors += 1
            return

        if job.status != JobStatus.AGENT_COMPLETED:
            # Either already postprocessed by another handler, or a
            # race with reclaim. Skip.
            stats.skipped += 1
            return

        # 2) Claim by transitioning to POSTPROCESSING. If two handlers
        # race, the first wins and the second sees InvalidStateTransition.
        try:
            self._queue.update_status(
                job.job_id, JobStatus.POSTPROCESSING
            )
        except InvalidStateTransition:
            stats.skipped += 1
            return
        except Exception:
            log.exception(
                f"Failed to claim {job.job_id} for postprocessing"
            )
            stats.errors += 1
            return

        log.info(f"Postprocessing job {job.job_id} (story={job.story_key})")

        # 3) Run the postprocessor
        try:
            outcome = self._postprocessor.run(job, result)
        except Exception as e:
            log.exception(f"Postprocessor crashed on {job.job_id}")
            self._mark_failed(
                job.job_id,
                f"Postprocessor crashed: {e}",
                stats,
            )
            return

        # 4) Translate outcome to final state
        stats.processed += 1
        if outcome.success:
            try:
                self._queue.update_status(job.job_id, JobStatus.DONE)
                log.info(
                    f"Job {job.job_id} done. PR: {outcome.pr_url}"
                )
                stats.succeeded += 1
            except Exception:
                log.exception(
                    f"Failed to mark {job.job_id} as DONE"
                )
                stats.errors += 1
        else:
            self._mark_failed(
                job.job_id,
                outcome.error or "Postprocessing failed without details",
                stats,
            )

    def _mark_failed(
        self, job_id: str, error: str, stats: HandlerCycleStats
    ) -> None:
        try:
            self._queue.update_status(
                job_id, JobStatus.FAILED, error=error
            )
            log.error(f"Job {job_id} FAILED in postprocessing: {error[:200]}")
            stats.failed += 1
        except Exception:
            log.exception(f"Failed to mark {job_id} as FAILED")
            stats.errors += 1
