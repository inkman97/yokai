"""Tests for execution stores (in-memory and SQLite).

The same test class runs against both implementations via parametrize.
"""

import threading

import pytest

from yokai.storage.memory_store import InMemoryExecutionStore
from yokai.storage.sqlite_store import SqliteExecutionStore


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryExecutionStore()
    else:
        s = SqliteExecutionStore(tmp_path / "store.db")
        yield s
        s.close()


class TestExecutionStore:
    def test_mark_in_flight_first_time_returns_true(self, store):
        assert store.mark_in_flight("N-1") is True

    def test_mark_in_flight_duplicate_returns_false(self, store):
        store.mark_in_flight("N-1")
        assert store.mark_in_flight("N-1") is False

    def test_is_in_flight_true_after_marking(self, store):
        store.mark_in_flight("N-1")
        assert store.is_in_flight("N-1") is True

    def test_is_in_flight_false_after_completion(self, store):
        store.mark_in_flight("N-1")
        store.mark_completed("N-1", "https://pr/1")
        assert store.is_in_flight("N-1") is False

    def test_completed_record_has_pr_url(self, store):
        store.mark_in_flight("N-1")
        store.mark_completed("N-1", "https://pr/1")
        records = store.list_recent()
        assert len(records) == 1
        assert records[0]["status"] == "completed"
        assert records[0]["pr_url"] == "https://pr/1"

    def test_failed_record_has_error(self, store):
        store.mark_in_flight("N-1")
        store.mark_failed("N-1", "boom")
        records = store.list_recent()
        assert records[0]["status"] == "failed"
        assert records[0]["error"] == "boom"

    def test_mark_in_flight_after_failed_succeeds(self, store):
        store.mark_in_flight("N-1")
        store.mark_failed("N-1", "boom")
        assert store.mark_in_flight("N-1") is True

    def test_list_recent_respects_limit(self, store):
        for i in range(10):
            store.mark_in_flight(f"N-{i}")
        records = store.list_recent(limit=3)
        assert len(records) == 3

    def test_list_recent_empty_when_nothing(self, store):
        assert store.list_recent() == []


class TestSqliteStorePersistence:
    def test_state_survives_reopen(self, tmp_path):
        path = tmp_path / "store.db"
        s1 = SqliteExecutionStore(path)
        s1.mark_in_flight("N-1")
        s1.mark_completed("N-1", "https://pr/1")
        s1.close()

        s2 = SqliteExecutionStore(path)
        records = s2.list_recent()
        assert len(records) == 1
        assert records[0]["story_key"] == "N-1"
        assert records[0]["status"] == "completed"
        s2.close()

    def test_concurrent_mark_in_flight_only_one_wins(self, tmp_path):
        store = SqliteExecutionStore(tmp_path / "store.db")
        winners = []
        guard = threading.Lock()

        def worker():
            if store.mark_in_flight("contested"):
                with guard:
                    winners.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1
        store.close()
