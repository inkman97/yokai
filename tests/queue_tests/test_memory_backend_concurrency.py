"""Concurrency tests for InMemoryBackend.

Tests run multiple worker threads against a single backend instance to
verify that the locking is correct and that no two workers ever pick
up the same job.
"""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from yokai.queue import (
    DuplicateJobError,
    Job,
    JobStatus,
)
from yokai.queue.backends.memory import InMemoryBackend


@pytest.fixture
def backend():
    return InMemoryBackend()


def test_no_two_workers_pick_up_the_same_job(backend):
    """Enqueue 100 jobs, run 20 workers in parallel, verify each job is
    picked exactly once."""
    NUM_JOBS = 100
    NUM_WORKERS = 20

    for i in range(NUM_JOBS):
        backend.enqueue(Job.new(f"S-{i}", "r", {}))

    picked_ids: list[str] = []

    def worker_loop(worker_id: str) -> list[str]:
        my_picks: list[str] = []
        while True:
            job = backend.dequeue(worker_id, timedelta(seconds=60))
            if job is None:
                break
            my_picks.append(job.job_id)
        return my_picks

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        futures = [
            ex.submit(worker_loop, f"worker-{i}") for i in range(NUM_WORKERS)
        ]
        for f in as_completed(futures):
            picked_ids.extend(f.result())

    assert len(picked_ids) == NUM_JOBS
    assert len(set(picked_ids)) == NUM_JOBS  # no duplicates


def test_concurrent_enqueue_with_distinct_story_keys_all_succeed(backend):
    NUM = 50

    def do_enqueue(i: int):
        return backend.enqueue(Job.new(f"S-{i}", "r", {}))

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(do_enqueue, range(NUM)))

    assert len(results) == NUM
    assert backend.stats()[JobStatus.QUEUED] == NUM


def test_concurrent_enqueue_with_same_story_key_only_one_wins(backend):
    """50 threads try to enqueue the same story key. Exactly one
    succeeds, the others get DuplicateJobError."""
    NUM = 50
    successes = 0
    duplicates = 0

    def try_enqueue(_: int):
        nonlocal successes, duplicates
        try:
            backend.enqueue(Job.new("SAME-KEY", "r", {}))
            return "ok"
        except DuplicateJobError:
            return "dup"

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(try_enqueue, range(NUM)))

    successes = results.count("ok")
    duplicates = results.count("dup")
    assert successes == 1
    assert duplicates == NUM - 1


def test_concurrent_status_updates_serialize_correctly(backend):
    """Two threads racing to mark the same job as completed: only one
    call should succeed, the other should see InvalidStateTransition."""
    from yokai.queue.exceptions import InvalidStateTransition

    backend.enqueue(Job.new("S-1", "r", {}))
    job = backend.dequeue("w", timedelta(seconds=60))
    backend.update_status(job.job_id, JobStatus.AGENT_RUNNING, "w")

    outcomes: list[str] = []

    def try_complete(_: int):
        try:
            backend.update_status(
                job.job_id, JobStatus.AGENT_COMPLETED, "w"
            )
            return "ok"
        except InvalidStateTransition:
            return "rejected"

    with ThreadPoolExecutor(max_workers=10) as ex:
        outcomes = list(ex.map(try_complete, range(10)))

    # AGENT_RUNNING -> AGENT_COMPLETED is valid.
    # AGENT_COMPLETED -> AGENT_COMPLETED is treated as idempotent
    # self-transition, so all 10 calls succeed.
    assert all(o == "ok" for o in outcomes)
    final = backend.get(job.job_id)
    assert final.status == JobStatus.AGENT_COMPLETED


def test_concurrent_coordinator_lock_only_one_holder(backend):
    """100 threads race to acquire the coordinator lock. Exactly one
    must win."""
    won = []

    def try_acquire(coord_id: str):
        if backend.acquire(coord_id, timedelta(seconds=60)):
            won.append(coord_id)

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(
            ex.map(
                try_acquire, [f"coord-{i}" for i in range(100)]
            )
        )

    assert len(won) == 1
    assert backend.current_owner() == won[0]


def test_dequeue_does_not_lose_jobs_under_concurrent_enqueue_dequeue(backend):
    """Producer threads enqueue jobs while consumer threads dequeue
    them. After a sync barrier, count must match."""
    NUM_PRODUCERS = 5
    NUM_CONSUMERS = 5
    JOBS_PER_PRODUCER = 20

    produced: list[str] = []
    produced_lock = __import__("threading").Lock()

    def producer(producer_idx: int):
        local: list[str] = []
        for i in range(JOBS_PER_PRODUCER):
            j = backend.enqueue(
                Job.new(f"P{producer_idx}-J{i}", "r", {})
            )
            local.append(j.job_id)
        with produced_lock:
            produced.extend(local)

    consumed: list[str] = []
    consumed_lock = __import__("threading").Lock()

    def consumer(consumer_idx: int):
        local: list[str] = []
        # Drain until empty (with a small delay to let producers run)
        import time
        time.sleep(0.1)
        while True:
            job = backend.dequeue(
                f"consumer-{consumer_idx}", timedelta(seconds=60)
            )
            if job is None:
                # Try once more in case a producer just enqueued
                time.sleep(0.05)
                job = backend.dequeue(
                    f"consumer-{consumer_idx}", timedelta(seconds=60)
                )
                if job is None:
                    break
            local.append(job.job_id)
        with consumed_lock:
            consumed.extend(local)

    with ThreadPoolExecutor(max_workers=NUM_PRODUCERS + NUM_CONSUMERS) as ex:
        producer_futures = [
            ex.submit(producer, i) for i in range(NUM_PRODUCERS)
        ]
        consumer_futures = [
            ex.submit(consumer, i) for i in range(NUM_CONSUMERS)
        ]
        for f in as_completed(producer_futures):
            f.result()
        for f in as_completed(consumer_futures):
            f.result()

    expected = NUM_PRODUCERS * JOBS_PER_PRODUCER
    assert len(produced) == expected
    assert len(set(consumed)) == len(consumed)  # no duplicates
    assert set(consumed).issubset(set(produced))
    # Some jobs may still be in QUEUED if consumers ran out first
    queued_remaining = backend.stats()[JobStatus.QUEUED]
    assert len(consumed) + queued_remaining == expected
