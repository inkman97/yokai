"""Retry backoff strategies.

A backoff function takes the attempt count (1-based) and returns the
delay in seconds before the next retry. The Worker uses this when
requeueing a failed job to set the not_before timestamp.

Default policy: exponential with jitter, capped at 5 minutes.
  attempt=1 -> ~5s
  attempt=2 -> ~10s
  attempt=3 -> ~20s
  attempt=4 -> ~40s
  attempt=5 -> ~80s
  attempt=6 -> ~160s
  attempt=7+ -> capped at 300s
"""

from __future__ import annotations

import random


def exponential_backoff(
    attempt: int,
    base_seconds: float = 5.0,
    cap_seconds: float = 300.0,
    jitter_ratio: float = 0.2,
) -> float:
    """Return delay in seconds for the given attempt number (1-based).

    delay = min(cap, base * 2^(attempt-1)) * (1 +/- jitter)
    """
    if attempt < 1:
        attempt = 1
    raw = base_seconds * (2 ** (attempt - 1))
    capped = min(raw, cap_seconds)
    if jitter_ratio > 0:
        factor = 1.0 + random.uniform(-jitter_ratio, jitter_ratio)
        capped = capped * factor
    return max(0.0, capped)


def no_backoff(attempt: int) -> float:
    """Constant zero delay - use for tests or when retries should
    happen immediately."""
    return 0.0
