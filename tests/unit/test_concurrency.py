"""Unit tests for RepoLockRegistry and InFlightRegistry."""

import threading
import time

from yokai.core.concurrency import (
    InFlightRegistry,
    RepoLockRegistry,
)


class TestRepoLockRegistry:
    def test_same_slug_returns_same_lock(self):
        reg = RepoLockRegistry()
        lock1 = reg.get("repo-a")
        lock2 = reg.get("repo-a")
        assert lock1 is lock2

    def test_different_slugs_return_different_locks(self):
        reg = RepoLockRegistry()
        assert reg.get("repo-a") is not reg.get("repo-b")

    def test_concurrent_get_is_safe(self):
        reg = RepoLockRegistry()
        results: list[object] = []

        def worker():
            results.append(reg.get("shared"))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(id(r) for r in results)) == 1

    def test_locks_actually_serialize(self):
        reg = RepoLockRegistry()
        counter = {"value": 0}
        observed_max = {"value": 0}
        concurrent = {"value": 0}
        guard = threading.Lock()

        def worker():
            with reg.get("shared"):
                with guard:
                    concurrent["value"] += 1
                    observed_max["value"] = max(
                        observed_max["value"], concurrent["value"]
                    )
                time.sleep(0.02)
                with guard:
                    concurrent["value"] -= 1
                counter["value"] += 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert counter["value"] == 5
        assert observed_max["value"] == 1

    def test_different_repos_run_in_parallel(self):
        reg = RepoLockRegistry()
        concurrent = {"value": 0}
        observed_max = {"value": 0}
        guard = threading.Lock()

        def worker(repo):
            with reg.get(repo):
                with guard:
                    concurrent["value"] += 1
                    observed_max["value"] = max(
                        observed_max["value"], concurrent["value"]
                    )
                time.sleep(0.05)
                with guard:
                    concurrent["value"] -= 1

        threads = [
            threading.Thread(target=worker, args=(f"repo-{i}",)) for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert observed_max["value"] >= 2


class TestInFlightRegistry:
    def test_try_mark_returns_true_first_time(self):
        reg = InFlightRegistry()
        assert reg.try_mark("story-1") is True

    def test_try_mark_returns_false_on_duplicate(self):
        reg = InFlightRegistry()
        reg.try_mark("story-1")
        assert reg.try_mark("story-1") is False

    def test_unmark_allows_remarking(self):
        reg = InFlightRegistry()
        reg.try_mark("story-1")
        reg.unmark("story-1")
        assert reg.try_mark("story-1") is True

    def test_unmark_missing_is_noop(self):
        reg = InFlightRegistry()
        reg.unmark("never-added")

    def test_size_tracks_entries(self):
        reg = InFlightRegistry()
        assert reg.size() == 0
        reg.try_mark("a")
        reg.try_mark("b")
        assert reg.size() == 2
        reg.unmark("a")
        assert reg.size() == 1

    def test_concurrent_try_mark_only_one_wins(self):
        reg = InFlightRegistry()
        winners = []
        guard = threading.Lock()

        def worker():
            if reg.try_mark("contested"):
                with guard:
                    winners.append(1)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1
