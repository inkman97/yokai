"""Concurrency primitives used by the orchestrator.

Two independent mechanisms:

- RepoLockRegistry: one Lock per repository slug. Two stories pointing at
  the same repo serialize through the same lock; stories on different
  repos proceed in parallel.

- InFlightRegistry: a set of story keys currently being processed.
  Protects against the race where the polling loop sees a story again
  before the issue tracker has observed the "processing" label write.

Both are thread-safe and are shared across all worker threads.
"""

from __future__ import annotations

import threading


class RepoLockRegistry:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, repo_slug: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(repo_slug)
            if lock is None:
                lock = threading.Lock()
                self._locks[repo_slug] = lock
            return lock


class InFlightRegistry:
    def __init__(self) -> None:
        self._keys: set[str] = set()
        self._guard = threading.Lock()

    def try_mark(self, story_key: str) -> bool:
        with self._guard:
            if story_key in self._keys:
                return False
            self._keys.add(story_key)
            return True

    def unmark(self, story_key: str) -> None:
        with self._guard:
            self._keys.discard(story_key)

    def is_in_flight(self, story_key: str) -> bool:
        with self._guard:
            return story_key in self._keys

    def size(self) -> int:
        with self._guard:
            return len(self._keys)
