"""Centralized logging configuration with secret redaction.

The framework handles tokens for issue trackers and repo hosting providers.
This module installs a logging filter that redacts known secrets from any
log record before it is emitted, regardless of where the secret appears.
"""

from __future__ import annotations

import logging
import sys
from typing import Iterable


_SECRETS_TO_REDACT: set[str] = set()


def register_secret(value: str) -> None:
    """Add a secret to the global redaction set.

    Call this once per token at startup. Empty values are ignored so it is
    safe to register optional secrets unconditionally.
    """
    if value:
        _SECRETS_TO_REDACT.add(value)


def clear_secrets() -> None:
    _SECRETS_TO_REDACT.clear()


class _RedactionFilter(logging.Filter):
    REDACTED = "***REDACTED***"

    def filter(self, record: logging.LogRecord) -> bool:
        if not _SECRETS_TO_REDACT:
            return True
        message = record.getMessage()
        for secret in _SECRETS_TO_REDACT:
            if secret in message:
                message = message.replace(secret, self.REDACTED)
        record.msg = message
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Install the framework logging configuration.

    Idempotent: calling it multiple times does not duplicate handlers.
    """
    root = logging.getLogger("yokai")
    if getattr(root, "_yokai_configured", False):
        root.setLevel(level)
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.addFilter(_RedactionFilter())

    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    root._yokai_configured = True  # type: ignore[attr-defined]


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the framework namespace."""
    return logging.getLogger(f"yokai.{name}")
