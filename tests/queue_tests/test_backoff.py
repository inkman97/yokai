"""Tests for retry backoff utilities."""

import random

from yokai.queue.backoff import exponential_backoff, no_backoff


class TestExponentialBackoff:
    def test_attempt_one_close_to_base(self):
        # No jitter: should be exactly base
        delay = exponential_backoff(1, base_seconds=5.0, jitter_ratio=0)
        assert delay == 5.0

    def test_attempt_two_doubles_base(self):
        delay = exponential_backoff(2, base_seconds=5.0, jitter_ratio=0)
        assert delay == 10.0

    def test_attempt_three_quadruples(self):
        delay = exponential_backoff(3, base_seconds=5.0, jitter_ratio=0)
        assert delay == 20.0

    def test_capped_at_cap_seconds(self):
        # 5 * 2^9 = 2560, capped at 100
        delay = exponential_backoff(
            10, base_seconds=5.0, cap_seconds=100.0, jitter_ratio=0
        )
        assert delay == 100.0

    def test_monotonic_increase_until_cap(self):
        prev = 0
        for attempt in range(1, 7):
            delay = exponential_backoff(
                attempt, base_seconds=5.0, cap_seconds=300.0, jitter_ratio=0
            )
            assert delay >= prev
            prev = delay

    def test_jitter_within_range(self):
        random.seed(42)
        for _ in range(100):
            delay = exponential_backoff(
                3, base_seconds=10.0, cap_seconds=1000.0, jitter_ratio=0.2
            )
            # base 10, attempt 3 -> 40 +/- 20%
            assert 32.0 <= delay <= 48.0

    def test_attempt_zero_treated_as_one(self):
        delay = exponential_backoff(0, base_seconds=5.0, jitter_ratio=0)
        assert delay == 5.0

    def test_negative_attempt_treated_as_one(self):
        delay = exponential_backoff(-3, base_seconds=5.0, jitter_ratio=0)
        assert delay == 5.0

    def test_never_returns_negative(self):
        # Even with extreme jitter, result must be >= 0
        random.seed(0)
        for _ in range(50):
            delay = exponential_backoff(
                1, base_seconds=5.0, jitter_ratio=0.99
            )
            assert delay >= 0


class TestNoBackoff:
    def test_always_zero(self):
        for attempt in range(1, 20):
            assert no_backoff(attempt) == 0.0
