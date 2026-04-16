"""Concrete backend implementations for the queue subsystem."""

from yokai.queue.backends.memory import InMemoryBackend
from yokai.queue.backends.sqlite import SqliteBackend

__all__ = ["InMemoryBackend", "SqliteBackend"]

# Redis backend is optional - only available if `redis` lib is installed.
try:
    from yokai.queue.backends.redis import RedisBackend  # noqa: F401
    __all__.append("RedisBackend")
except ImportError:
    pass
