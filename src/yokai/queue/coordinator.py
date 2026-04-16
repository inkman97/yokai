"""The Coordinator process.

Polls an IssueSource at a fixed interval, validates each story
(routes it to a repo), and enqueues a Job into the JobQueue. Uses
the CoordinatorLock for leader election so multiple coordinator
processes can be launched safely - only one will actually poll.

The coordinator is a long-running loop. It can be invoked via:

    coordinator = Coordinator(...)
    coordinator.run()  # blocks until stop() is called

For tests, `run_once()` does a single polling cycle and returns,
making the loop deterministic.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from yokai.queue.exceptions import DuplicateJobError
from yokai.queue.interfaces import CoordinatorLock, JobQueue
from yokai.queue.models import Job
from yokai.queue.sources import IssueSource, StoryRouter, StorySnapshot


log = logging.getLogger("yokai.coordinator")


@dataclass
class CoordinatorSettings:
    poll_interval_seconds: float = 30.0
    lease_duration_seconds: float = 90.0
    lease_renew_interval_seconds: float = 30.0
    reclaim_interval_seconds: float = 60.0
    max_attempts_per_job: int = 3
    coordinator_id: str = field(
        default_factory=lambda: f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    )


@dataclass
class CycleStats:
    """Outcome of a single polling cycle, useful for tests and logs."""

    fetched: int = 0
    enqueued: int = 0
    duplicates: int = 0
    unroutable: int = 0
    errors: int = 0
    reclaimed: int = 0


class Coordinator:
    def __init__(
        self,
        *,
        source: IssueSource,
        router: StoryRouter,
        queue: JobQueue,
        lock: CoordinatorLock,
        settings: CoordinatorSettings | None = None,
    ) -> None:
        self._source = source
        self._router = router
        self._queue = queue
        self._lock = lock
        self._settings = settings or CoordinatorSettings()
        self._stop_event = threading.Event()
        self._last_reclaim_at: float = 0.0
        self._holds_lock: bool = False

    @property
    def coordinator_id(self) -> str:
        return self._settings.coordinator_id

    def stop(self) -> None:
        """Signal the run loop to exit at the next iteration."""
        self._stop_event.set()

    def run(self) -> None:
        """Main loop. Blocks until stop() is called or KeyboardInterrupt.

        Each iteration: try to acquire/renew the leader lock, then if
        we are the leader, poll once. Sleep for poll_interval and repeat.
        """
        log.info(f"Coordinator {self.coordinator_id} starting")
        try:
            while not self._stop_event.is_set():
                if self._acquire_or_renew_lock():
                    self.run_once()
                else:
                    log.debug(
                        f"Not the leader (current owner: "
                        f"{self._lock.current_owner()}), skipping poll"
                    )
                self._stop_event.wait(self._settings.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info("Coordinator interrupted")
        finally:
            if self._holds_lock:
                self._lock.release(self.coordinator_id)
                log.info(f"Coordinator {self.coordinator_id} released lock")

    def run_once(self) -> CycleStats:
        """Execute a single polling + enqueueing cycle.

        Returns CycleStats so tests can assert on outcomes.
        Does NOT acquire the leader lock - call sites that need leader
        semantics should call _acquire_or_renew_lock first.
        """
        stats = CycleStats()

        # Periodic reclaim of orphaned jobs (worker died with lease)
        now = time.monotonic()
        if (
            now - self._last_reclaim_at
            >= self._settings.reclaim_interval_seconds
        ):
            try:
                reclaimed = self._queue.reclaim_expired_leases()
                stats.reclaimed = len(reclaimed)
                if reclaimed:
                    log.info(
                        f"Reclaimed {len(reclaimed)} jobs from dead workers"
                    )
            except Exception as e:
                log.exception(f"Reclaim failed: {e}")
                stats.errors += 1
            self._last_reclaim_at = now

        # Fetch and enqueue new stories
        try:
            stories = self._source.fetch_pending()
        except Exception as e:
            log.exception(f"fetch_pending failed: {e}")
            stats.errors += 1
            return stats

        stats.fetched = len(stories)
        for story in stories:
            self._process_story(story, stats)

        if stats.fetched > 0 or stats.reclaimed > 0:
            log.info(
                f"Cycle stats: fetched={stats.fetched} "
                f"enqueued={stats.enqueued} duplicates={stats.duplicates} "
                f"unroutable={stats.unroutable} errors={stats.errors} "
                f"reclaimed={stats.reclaimed}"
            )
        return stats

    def _process_story(
        self, story: StorySnapshot, stats: CycleStats
    ) -> None:
        repo_slug = self._router.resolve_repo(story)
        if repo_slug is None:
            log.warning(
                f"Story {story.key} unroutable: no matching component "
                f"or label. Components={story.components}, labels={story.labels}"
            )
            stats.unroutable += 1
            try:
                self._source.mark_rejected(
                    story.key,
                    "No repository could be resolved from components or labels",
                )
            except Exception:
                log.exception(
                    f"Failed to mark {story.key} as rejected"
                )
            return

        payload = {
            "title": story.title,
            "description": story.description,
            "components": story.components,
            "labels": story.labels,
        }
        job = Job.new(
            story_key=story.key,
            repo_slug=repo_slug,
            payload=payload,
            max_attempts=self._settings.max_attempts_per_job,
        )

        try:
            self._queue.enqueue(job)
        except DuplicateJobError:
            log.debug(f"Story {story.key} already in flight, skipping")
            stats.duplicates += 1
            return
        except Exception as e:
            log.exception(f"enqueue failed for {story.key}: {e}")
            stats.errors += 1
            return

        # Only mark accepted after successful enqueue
        try:
            self._source.mark_accepted(story.key)
        except Exception:
            # The job is already in the queue. Failing to mark on the
            # source is non-fatal but logged - the next poll might
            # re-fetch the same story (handled by DuplicateJobError).
            log.exception(
                f"Failed to mark {story.key} as accepted on source"
            )

        log.info(
            f"Enqueued {story.key} -> {repo_slug} (job_id={job.job_id})"
        )
        stats.enqueued += 1

    def _acquire_or_renew_lock(self) -> bool:
        """Returns True if we hold the leader lock at the end of the call."""
        lease = timedelta(seconds=self._settings.lease_duration_seconds)
        if self._holds_lock:
            renewed = self._lock.renew(self.coordinator_id, lease)
            if renewed:
                return True
            # Lost the lock - try to reacquire
            log.warning(
                f"Lost coordinator lock (current owner: "
                f"{self._lock.current_owner()}), trying to reacquire"
            )
            self._holds_lock = False

        acquired = self._lock.acquire(self.coordinator_id, lease)
        if acquired:
            if not self._holds_lock:
                log.info(
                    f"Acquired coordinator lock as {self.coordinator_id}"
                )
            self._holds_lock = True
            return True
        return False
