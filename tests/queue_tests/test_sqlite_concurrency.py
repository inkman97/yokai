"""Concurrency tests for SQLite backend with multiple instances.

Each test creates N SqliteBackend instances pointing at the same DB
file and runs workers in threads. This simulates the cross-process
case (real workers in separate yokai processes) without needing
multiprocessing.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pytest

from yokai.queue import (
    DuplicateJobError,
    Job,
    JobStatus,
)
from yokai.queue.backends.sqlite import SqliteBackend


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "queue.db"


def test_no_duplicate_dequeue_across_two_instances(db_path):
    """Two backend instances simulate two worker processes. Enqueue
    50 jobs, race them. Each job must go to exactly one worker."""
    NUM_JOBS = 50

    coordinator = SqliteBackend(db_path)
    for i in range(NUM_JOBS):
        coordinator.enqueue(Job.new(f"S-{i}", "r", {}))

    def worker_loop(worker_id: str) -> list[str]:
        # Each worker gets its own backend instance (simulating its
        # own process)
        backend = SqliteBackend(db_path)
        picks = []
        while True:
            job = backend.dequeue(worker_id, timedelta(seconds=60))
            if job is None:
                break
            picks.append(job.job_id)
        return picks

    all_picks: list[str] = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(worker_loop, f"w-{i}") for i in range(10)]
        for f in as_completed(futures):
            all_picks.extend(f.result())

    assert len(all_picks) == NUM_JOBS
    assert len(set(all_picks)) == NUM_JOBS  # no duplicates


def test_concurrent_enqueue_same_story_only_one_succeeds(db_path):
    NUM_THREADS = 30

    def try_enqueue(_: int):
        backend = SqliteBackend(db_path)
        try:
            backend.enqueue(Job.new("SAME-KEY", "r", {}))
            return "ok"
        except DuplicateJobError:
            return "dup"

    with ThreadPoolExecutor(max_workers=10) as ex:
        outcomes = list(ex.map(try_enqueue, range(NUM_THREADS)))

    assert outcomes.count("ok") == 1
    assert outcomes.count("dup") == NUM_THREADS - 1


def test_coordinator_lock_only_one_winner_across_instances(db_path):
    """100 attempts via 20 threads, each with its own backend. Only
    one acquires."""
    won = []

    def try_acquire(coord_id: str):
        backend = SqliteBackend(db_path)
        if backend.acquire(coord_id, timedelta(seconds=60)):
            won.append(coord_id)

    with ThreadPoolExecutor(max_workers=20) as ex:
        list(ex.map(try_acquire, [f"c-{i}" for i in range(100)]))

    assert len(won) == 1


def test_high_volume_dequeue_does_not_lose_or_duplicate_jobs(db_path):
    """500 jobs, 20 workers across separate backend instances. Verify
    counts."""
    NUM_JOBS = 500
    NUM_WORKERS = 20

    coordinator = SqliteBackend(db_path)
    for i in range(NUM_JOBS):
        coordinator.enqueue(Job.new(f"S-{i}", "r", {}))

    def worker_loop(worker_id: str) -> list[str]:
        backend = SqliteBackend(db_path)
        picks = []
        while True:
            job = backend.dequeue(worker_id, timedelta(seconds=60))
            if job is None:
                break
            picks.append(job.job_id)
        return picks

    all_picks: list[str] = []
    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as ex:
        futures = [
            ex.submit(worker_loop, f"w-{i}") for i in range(NUM_WORKERS)
        ]
        for f in as_completed(futures):
            all_picks.extend(f.result())

    assert len(all_picks) == NUM_JOBS
    assert len(set(all_picks)) == NUM_JOBS

    final_stats = coordinator.stats()
    assert final_stats[JobStatus.PICKED_UP] == NUM_JOBS
    assert final_stats[JobStatus.QUEUED] == 0
