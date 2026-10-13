"""Part A2 — Backoff with full jitter, and a retry budget.

Tests: tests/test_errors_retry.py
"""

from __future__ import annotations

import math
import random


class FullJitterBackoff:
    """Exponential backoff with *full* jitter (Brooker, AWS 2015).

    delay(n) = uniform(0, min(cap_s, base_s * 2**(n-1)))   for the n-th retry (n >= 1)

    If the provider sent ``retry_after``, honour it: never retry earlier than
    ``retry_after``; add a small jitter on top so clients do not all come back
    in the same instant: retry_after + uniform(0, base_s).
    """

    def __init__(self, base_s: float = 0.25, cap_s: float = 8.0, rng: random.Random | None = None) -> None:
        if not math.isfinite(base_s) or not math.isfinite(cap_s) or base_s <= 0 or cap_s <= 0:
            raise ValueError("base_s and cap_s must be positive")
        self.base_s = base_s
        self.cap_s = cap_s
        self.rng = rng or random.Random(0)

    def delay(self, retry_number: int, retry_after: float | None = None) -> float:
        """TODO(student).

        * raise ValueError if retry_number < 1
        * with retry_after:    retry_after + rng.uniform(0, base_s)
        * without retry_after: rng.uniform(0, min(cap_s, base_s * 2 ** (retry_number - 1)))
        Use ``self.rng`` (seeded) — never the global ``random`` module.
        """
        if retry_number < 1:
            raise ValueError("retry_number must be at least 1")
        if retry_after is not None:
            if not math.isfinite(retry_after) or retry_after < 0:
                raise ValueError("retry_after must be non-negative")
            return retry_after + self.rng.uniform(0.0, self.base_s)

        try:
            ceiling = min(self.cap_s, math.ldexp(self.base_s, retry_number - 1))
        except OverflowError:
            ceiling = self.cap_s
        return self.rng.uniform(0.0, ceiling)


class RetryBudget:
    """Token-bucket retry budget (as in Finagle / gRPC retry throttling).

    * The bucket starts full (``max_tokens``).
    * Every *first* attempt deposits ``ratio`` tokens (capped at ``max_tokens``).
    * Tokens also refill at ``min_per_s`` per second, so a quiet service can
      still retry occasionally.
    * A retry is allowed only if one whole token is available; it consumes it.

    With ratio=0.1 retries can never exceed ~10% of traffic, however bad the
    incident — which is exactly what stops a retry storm.
    """

    def __init__(self, ratio: float = 0.1, min_per_s: float = 0.5, max_tokens: float = 20.0) -> None:
        if (
            not all(math.isfinite(value) for value in (ratio, min_per_s, max_tokens))
            or ratio < 0
            or min_per_s < 0
            or max_tokens < 0
        ):
            raise ValueError("ratio, min_per_s, and max_tokens must be non-negative")
        self.ratio = ratio
        self.min_per_s = min_per_s
        self.max_tokens = max_tokens
        self.tokens = max_tokens
        self._last: float | None = None  # virtual time of the previous call

    def record_request(self, now: float) -> None:
        """Refill by elapsed virtual time, then deposit the request's tokens."""
        self._refill(now)
        self.tokens = min(self.max_tokens, self.tokens + self.ratio)

    def try_acquire_retry(self, now: float) -> bool:
        """Refill by elapsed virtual time and spend one token if available."""
        self._refill(now)
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def _refill(self, now: float) -> None:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if self._last is None:
            self._last = now
            return
        elapsed = max(0.0, now - self._last)
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.min_per_s)
        self._last = max(self._last, now)
