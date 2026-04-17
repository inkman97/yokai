"""The Worker process.

A worker pulls jobs one at a time from a JobQueue, executes the agent
on a prepared repo checkout, and writes the JobResult into the
ResultStore. It heartbeats periodically so the coordinator can detect
when it dies.

Lifecycle of a job inside a worker:

  dequeue (PICKED_UP)
    -> prepare checkout
    -> update_status(AGENT_RUNNING)
    -> agent.run() with timeout
    -> if success: update_status(AGENT_COMPLETED) + put(JobResult)
       if failure: update_status(AGENT_FAILED) + put(failed JobResult)
    -> cleanup checkout

Heartbeats run on a separate thread, every heartbeat_interval seconds.

Graceful shutdown: stop() sets a flag. The worker finishes the
current job (if any), deregisters from the WorkerRegistry, and exits.
SIGTERM/SIGINT handlers can be installed by the entry point if desired
(yokai.cli) - the Worker class itself does not install signal handlers
so it remains test-friendly.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from yokai.core.hooks import HookRegistry
from yokai.queue.agent import AgentRunner, RepoCheckout
from yokai.queue.backoff import exponential_backoff
from yokai.queue.exceptions import LeaseExpiredError
from yokai.queue.interfaces import JobQueue, ResultStore, WorkerRegistry
from yokai.queue.models import (
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)


log = logging.getLogger("yokai.worker")


@dataclass
class WorkerSettings:
    poll_interval_seconds: float = 2.0
    lease_duration_seconds: float = 1800.0  # 30 min, matches agent timeout
    agent_timeout_seconds: float = 1800.0
    heartbeat_interval_seconds: float = 15.0
    retry_backoff_base_seconds: float = 5.0
    retry_backoff_cap_seconds: float = 300.0
    worker_id: str = field(
        default_factory=lambda: f"{socket.gethostname()}-w-{uuid.uuid4().hex[:8]}"
    )


@dataclass
class WorkerStats:
    jobs_processed: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0
    jobs_timed_out: int = 0


class Worker:
    def __init__(
            self,
            *,
            queue: JobQueue,
            results: ResultStore,
            registry: WorkerRegistry,
            agent: AgentRunner,
            checkout: RepoCheckout,
            settings: WorkerSettings | None = None,
            hooks: HookRegistry | None = None,
    ) -> None:
        self._queue = queue
        self._results = results
        self._registry = registry
        self._agent = agent
        self._checkout = checkout
        self._settings = settings or WorkerSettings()
        self._hooks = hooks or HookRegistry()
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._current_job_id: str | None = None
        self._current_job_lock = threading.Lock()
        self.stats = WorkerStats()

    def _emit(self, event: str, **payload) -> None:
        """Safely emit a hook event. Never raises."""
        try:
            self._hooks.emit(event, payload)
        except Exception:
            log.exception(f"Hook {event} failed (non-fatal)")

    @property
    def worker_id(self) -> str:
        return self._settings.worker_id

    def stop(self) -> None:
        """Request a graceful shutdown after the current job completes."""
        log.info(f"Worker {self.worker_id} received stop signal")
        self._stop_event.set()

    def run(self) -> None:
        """Main loop. Blocks until stop() is called."""
        self._register()
        self._start_heartbeat_thread()
        log.info(f"Worker {self.worker_id} started")
        try:
            while not self._stop_event.is_set():
                processed = self._process_one_job()
                if not processed and not self._stop_event.is_set():
                    self._stop_event.wait(self._settings.poll_interval_seconds)
        except KeyboardInterrupt:
            log.info(f"Worker {self.worker_id} interrupted")
        finally:
            self._stop_event.set()
            self._stop_heartbeat_thread()
            self._deregister()
            log.info(
                f"Worker {self.worker_id} exiting. "
                f"Stats: {self.stats}"
            )

    def process_one_job(self) -> bool:
        """Public single-job entry point for tests.

        Returns True if a job was processed, False if no job was
        available. Does NOT touch heartbeat thread or registry.
        """
        return self._process_one_job()

    def _process_one_job(self) -> bool:
        try:
            job = self._queue.dequeue(
                self.worker_id,
                timedelta(seconds=self._settings.lease_duration_seconds),
            )
        except Exception:
            log.exception("dequeue failed")
            return False

        if job is None:
            return False

        log.info(
            f"Picked up job {job.job_id} (story={job.story_key}, "
            f"repo={job.repo_slug}, attempt={job.attempts})"
        )
        with self._current_job_lock:
            self._current_job_id = job.job_id

        try:
            self._execute_job(job)
        finally:
            with self._current_job_lock:
                self._current_job_id = None
            self.stats.jobs_processed += 1
        return True

    def _execute_job(self, job: Job) -> None:
        start_time = time.monotonic()

        # Lazy-build the Story for hook payloads (plugins written
        # against the monolithic Pipeline expect this type)
        from yokai.queue_adapters.agent_runner import job_to_story
        story = job_to_story(job)

        self._emit("before_process", story=story)
        self._emit(
            "after_resolve_repo", story=story, repo_slug=job.repo_slug
        )

        # 1) Prepare checkout
        try:
            checkout_info = self._checkout.prepare(job)
        except Exception as e:
            log.exception(f"checkout.prepare failed for {job.job_id}")
            self._record_failure(
                job,
                error=f"Checkout preparation failed: {e}",
                traceback_str=traceback.format_exc(),
                duration=time.monotonic() - start_time,
            )
            self._emit("on_failure", story=story, error=e)
            self._safe_cleanup(job)
            return

        self._emit(
            "after_clone", story=story, repo_path=checkout_info.repo_path
        )

        # 2) Transition to AGENT_RUNNING
        try:
            self._queue.update_status(
                job.job_id, JobStatus.AGENT_RUNNING, self.worker_id
            )
        except LeaseExpiredError:
            log.warning(
                f"Lost lease on {job.job_id} before starting agent, aborting"
            )
            self._safe_cleanup(job)
            return
        except Exception:
            log.exception(
                f"Failed to mark {job.job_id} as AGENT_RUNNING"
            )
            self._safe_cleanup(job)
            return

        # 3) Run the agent
        self._emit(
            "before_agent_run",
            story=story,
            repo_path=checkout_info.repo_path,
            prompt=None,  # prompt is built inside AgentCodingRunner
        )
        try:
            execution = self._agent.run(
                job,
                checkout_info.repo_path,
                timeout_seconds=self._settings.agent_timeout_seconds,
            )
        except Exception as e:
            log.exception(f"Agent runner crashed on {job.job_id}")
            self._record_failure(
                job,
                error=f"Agent runner crashed: {e}",
                traceback_str=traceback.format_exc(),
                duration=time.monotonic() - start_time,
            )
            self._emit("on_failure", story=story, error=e)
            self._safe_cleanup(job)
            return

        # Convert AgentExecution -> AgentResult for hook-payload parity
        # with the monolithic Pipeline
        from yokai.core.models import AgentResult
        agent_result = AgentResult(
            success=execution.success,
            output=execution.output or "",
            duration_seconds=time.monotonic() - start_time,
            error=execution.error,
        )
        self._emit("after_agent_run", story=story, agent_result=agent_result)

        duration = time.monotonic() - start_time

        # 4) Record outcome
        if execution.success:
            # 4a) Commit + push the agent's changes while still on
            # the feature branch. This must happen in the Worker
            # (not in the ResultHandler) because the working tree
            # with the agent's modifications only exists here.
            commit_message = f"feat({job.story_key}): {job.payload.get('title', '')}\n\nGenerated by yokai"
            try:
                commit_result = self._checkout.commit_and_push(
                    checkout_info, commit_message
                )
            except NotImplementedError:
                # Legacy checkout without commit_and_push: fall through
                # and let the ResultHandler handle it (same-host only).
                commit_result = "legacy"
            except Exception as e:
                log.exception(f"commit_and_push failed for {job.job_id}")
                self._record_failure(
                    job,
                    error=f"Commit/push failed: {e}",
                    traceback_str=traceback.format_exc(),
                    duration=duration,
                    output=execution.output,
                )
                self._emit("on_failure", story=story, error=e)
                self._safe_cleanup(job)
                return

            if commit_result is None:
                # Agent ran successfully but made no changes
                log.warning(
                    f"Agent reported success but made no changes for {job.job_id}"
                )
                self._record_failure(
                    job,
                    error="Agent reported success but made no file changes",
                    duration=duration,
                    output=execution.output,
                )
                self._emit(
                    "on_failure", story=story,
                    error="Agent made no changes",
                )
                self._safe_cleanup(job)
                return

            if commit_result != "legacy":
                self._emit("after_commit", story=story, commit=None)
                self._emit(
                    "after_push", story=story,
                    branch_name=checkout_info.branch_name,
                )

            self._record_success(job, execution, checkout_info, duration)
        else:
            self._record_failure(
                job,
                error=execution.error or "Agent reported failure",
                traceback_str=execution.traceback,
                duration=duration,
                output=execution.output,
            )
            if execution.error and "timeout" in execution.error.lower():
                self.stats.jobs_timed_out += 1
            self._emit(
                "on_failure",
                story=story,
                error=execution.error or "Agent reported failure",
            )

        self._safe_cleanup(job)

    def _record_success(
            self,
            job: Job,
            execution,  # AgentExecution
            checkout_info,  # CheckoutInfo
            duration: float,
    ) -> None:
        result = JobResult(
            job_id=job.job_id,
            success=True,
            agent_output=execution.output,
            duration_seconds=duration,
            branch_name=checkout_info.branch_name,
        )
        try:
            self._results.put(result)
        except Exception:
            log.exception(
                f"Failed to write success result for {job.job_id}"
            )
            # Try to mark job failed since we cannot record the result
            self._safe_status_update(
                job, JobStatus.AGENT_FAILED,
                error="Result store write failed",
            )
            self.stats.jobs_failed += 1
            return

        try:
            self._queue.update_status(
                job.job_id, JobStatus.AGENT_COMPLETED, self.worker_id
            )
            log.info(
                f"Job {job.job_id} completed in {duration:.1f}s "
                f"(output: {len(execution.output)} chars)"
            )
            self.stats.jobs_succeeded += 1
        except LeaseExpiredError:
            log.warning(
                f"Lost lease on {job.job_id} after agent succeeded. "
                f"Result is stored but job state may be inconsistent."
            )
            self.stats.jobs_failed += 1
        except Exception:
            log.exception(
                f"Failed to mark {job.job_id} as AGENT_COMPLETED"
            )
            self.stats.jobs_failed += 1

    def _record_failure(
            self,
            job: Job,
            *,
            error: str,
            traceback_str: str | None = None,
            duration: float = 0.0,
            output: str = "",
    ) -> None:
        # Always try to write a JobResult so postprocessing can see it
        try:
            self._results.put(
                JobResult(
                    job_id=job.job_id,
                    success=False,
                    agent_output=output,
                    error=error,
                    traceback=traceback_str,
                    duration_seconds=duration,
                )
            )
        except Exception:
            log.exception(
                f"Failed to write failure result for {job.job_id}"
            )

        # Decide retry vs dead letter based on attempts vs max_attempts
        # We re-fetch the job to see the latest attempts count (the
        # dequeue already incremented it).
        try:
            current = self._queue.get(job.job_id)
        except Exception:
            log.exception(f"Could not refetch {job.job_id} for retry decision")
            self.stats.jobs_failed += 1
            return

        if current.attempts >= current.max_attempts:
            # Final failure: AGENT_FAILED -> DEAD_LETTERED
            self._safe_status_update(
                job, JobStatus.AGENT_FAILED, error=error
            )
            try:
                self._queue.update_status(
                    job.job_id, JobStatus.DEAD_LETTERED, error=error
                )
                log.error(
                    f"Job {job.job_id} dead-lettered after "
                    f"{current.attempts} attempts: {error[:200]}"
                )
            except Exception:
                log.exception(
                    f"Failed to dead-letter {job.job_id}"
                )
        else:
            # Retry: AGENT_FAILED -> QUEUED with backoff
            self._safe_status_update(
                job, JobStatus.AGENT_FAILED, error=error
            )
            backoff_seconds = exponential_backoff(
                current.attempts,
                base_seconds=self._settings.retry_backoff_base_seconds,
                cap_seconds=self._settings.retry_backoff_cap_seconds,
            )
            not_before = datetime.now(timezone.utc) + timedelta(
                seconds=backoff_seconds
            )
            try:
                self._queue.update_status(
                    job.job_id,
                    JobStatus.QUEUED,
                    error=error,
                    not_before=not_before,
                )
                log.warning(
                    f"Job {job.job_id} requeued for retry "
                    f"({current.attempts}/{current.max_attempts}) "
                    f"after {backoff_seconds:.1f}s backoff: {error[:200]}"
                )
            except Exception:
                log.exception(
                    f"Failed to requeue {job.job_id} for retry"
                )

        self.stats.jobs_failed += 1

    def _safe_status_update(
            self,
            job: Job,
            new_status: JobStatus,
            error: str | None = None,
    ) -> None:
        """Best-effort status update that swallows lease errors."""
        try:
            self._queue.update_status(
                job.job_id, new_status, self.worker_id, error=error
            )
        except LeaseExpiredError:
            log.warning(
                f"Lease expired during status update on {job.job_id}; "
                f"trying without lease check"
            )
            try:
                self._queue.update_status(
                    job.job_id, new_status, error=error
                )
            except Exception:
                log.exception(
                    f"Coordinator-style update_status also failed for "
                    f"{job.job_id}"
                )
        except Exception:
            log.exception(
                f"update_status({new_status}) failed for {job.job_id}"
            )

    def _safe_cleanup(self, job: Job) -> None:
        try:
            self._checkout.cleanup(job)
        except Exception:
            log.exception(f"checkout.cleanup failed for {job.job_id}")

    def _register(self) -> None:
        now = datetime.now(timezone.utc)
        info = WorkerInfo(
            worker_id=self.worker_id,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            started_at=now,
            last_heartbeat_at=now,
        )
        try:
            self._registry.register(info)
        except Exception:
            log.exception(f"Failed to register worker {self.worker_id}")

    def _deregister(self) -> None:
        try:
            self._registry.deregister(self.worker_id)
        except Exception:
            log.exception(f"Failed to deregister worker {self.worker_id}")

    def _start_heartbeat_thread(self) -> None:
        def heartbeat_loop():
            while not self._stop_event.is_set():
                with self._current_job_lock:
                    cj = self._current_job_id
                try:
                    self._registry.heartbeat(self.worker_id, cj)
                except Exception:
                    log.exception(
                        f"Heartbeat failed for {self.worker_id}"
                    )
                self._stop_event.wait(
                    self._settings.heartbeat_interval_seconds
                )

        t = threading.Thread(
            target=heartbeat_loop,
            name=f"yokai-heartbeat-{self.worker_id}",
            daemon=True,
        )
        t.start()
        self._heartbeat_thread = t

    def _stop_heartbeat_thread(self) -> None:
        if self._heartbeat_thread is None:
            return
        # _stop_event is already set
        self._heartbeat_thread.join(timeout=5.0)
        if self._heartbeat_thread.is_alive():
            log.warning(
                f"Heartbeat thread did not exit cleanly within 5s"
            )
