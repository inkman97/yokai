"""In-memory execution store.

Used for testing and for short-lived runs where persistence is not
needed. The state is lost when the process exits.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from yokai.core.interfaces import ExecutionStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryExecutionStore(ExecutionStore):
    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._guard = threading.Lock()

    def is_in_flight(self, story_key: str) -> bool:
        with self._guard:
            record = self._records.get(story_key)
            return record is not None and record.get("status") == "in_flight"

    def mark_in_flight(self, story_key: str) -> bool:
        with self._guard:
            existing = self._records.get(story_key)
            if existing and existing.get("status") == "in_flight":
                return False
            self._records[story_key] = {
                "story_key": story_key,
                "status": "in_flight",
                "started_at": _now_iso(),
                "completed_at": None,
                "pr_url": None,
                "error": None,
            }
            return True

    def mark_completed(self, story_key: str, pr_url: str) -> None:
        with self._guard:
            record = self._records.get(story_key) or {"story_key": story_key}
            record["status"] = "completed"
            record["completed_at"] = _now_iso()
            record["pr_url"] = pr_url
            self._records[story_key] = record

    def mark_failed(self, story_key: str, error: str) -> None:
        with self._guard:
            record = self._records.get(story_key) or {"story_key": story_key}
            record["status"] = "failed"
            record["completed_at"] = _now_iso()
            record["error"] = error
            self._records[story_key] = record

    def list_recent(self, limit: int = 50) -> list[dict]:
        with self._guard:
            items = list(self._records.values())
            items.sort(
                key=lambda r: r.get("started_at") or "",
                reverse=True,
            )
            return items[:limit]
