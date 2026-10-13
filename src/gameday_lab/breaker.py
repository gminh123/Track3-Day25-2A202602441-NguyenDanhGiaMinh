"""Part B — Sliding-window circuit breaker that is safe under concurrency.

Differences from the textbook "open after 5 consecutive failures" breaker:

* trips on the failure *rate* over the last ``window_size`` results, and only
  once ``min_calls`` results are in (no tripping on 2 errors at 3am);
* treats *slow* successes as unhealthy (``slow_call_s``) — catches degradation
  before timeouts pile up;
* HALF_OPEN lets at most ``half_open_max_probes`` requests in flight; everyone
  else keeps failing fast (no thundering herd on a recovering provider);
* every admitted request gets a ``Permit``; results that come back after the
  state has changed (stale ``generation``) are ignored.

Tests: tests/test_breaker.py
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class Permit:
    breaker: str
    generation: int  # state generation at the time the permit was issued
    probe: bool  # issued in HALF_OPEN


@dataclass
class Transition:
    t: float
    frm: BreakerState
    to: BreakerState
    reason: str


@dataclass
class SlidingWindowBreaker:
    name: str
    window_size: int = 20
    min_calls: int = 10
    failure_rate_threshold: float = 0.5
    slow_call_s: float = 5.0
    slow_rate_threshold: float = 0.6
    open_duration_s: float = 10.0
    half_open_max_probes: int = 2
    half_open_successes: int = 2

    state: BreakerState = BreakerState.CLOSED
    generation: int = 0
    opened_at: float = 0.0
    probes_in_flight: int = 0
    probe_successes: int = 0
    transitions: list[Transition] = field(default_factory=list)
    _window: deque[tuple[bool, bool]] = field(default_factory=deque)  # (failed, slow)

    def __post_init__(self) -> None:
        if self.window_size < 1 or self.min_calls < 1:
            raise ValueError("window_size and min_calls must be positive")
        if not 0.0 <= self.failure_rate_threshold <= 1.0:
            raise ValueError("failure_rate_threshold must be between 0 and 1")
        if not 0.0 <= self.slow_rate_threshold <= 1.0:
            raise ValueError("slow_rate_threshold must be between 0 and 1")
        if self.slow_call_s < 0 or self.open_duration_s < 0:
            raise ValueError("breaker durations must be non-negative")
        if self.half_open_max_probes < 1 or self.half_open_successes < 1:
            raise ValueError("half-open probe counts must be positive")

    # ------------------------------------------------------------ provided
    def _move(self, now: float, to: BreakerState, reason: str) -> None:
        """Change state. PROVIDED — use it for every transition.

        Logs the transition, bumps ``generation`` (so older permits become
        stale), resets probe counters, records ``opened_at`` when opening and
        clears the window when closing.
        """
        self.transitions.append(Transition(now, self.state, to, reason))
        self.state = to
        self.generation += 1
        self.probes_in_flight = 0
        self.probe_successes = 0
        if to is BreakerState.OPEN:
            self.opened_at = now
        if to is BreakerState.CLOSED:
            self._window.clear()

    # ------------------------------------------------------------ state/results
    def _record(self, now: float, failed: bool, slow: bool) -> None:
        """Add one CLOSED-state result to the sliding window and maybe trip.

        1. append (failed, slow); keep only the last ``window_size`` entries
        2. if fewer than ``min_calls`` entries: return
        3. failure rate >= failure_rate_threshold      -> _move(OPEN, "failure_rate")
           elif slow rate (slow and not failed) >= slow_rate_threshold
                                                        -> _move(OPEN, "slow_call_rate")
        """
        self._window.append((failed, slow))
        while len(self._window) > self.window_size:
            self._window.popleft()
        if len(self._window) < self.min_calls:
            return

        total = len(self._window)
        failure_rate = sum(result_failed for result_failed, _ in self._window) / total
        slow_rate = sum(result_slow and not result_failed for result_failed, result_slow in self._window) / total
        if failure_rate >= self.failure_rate_threshold:
            self._move(now, BreakerState.OPEN, "failure_rate")
        elif slow_rate >= self.slow_rate_threshold:
            self._move(now, BreakerState.OPEN, "slow_call_rate")

    def allow(self, now: float) -> Permit | None:
        """Return a Permit if a request may be sent now, else None.

        * OPEN: if less than ``open_duration_s`` since ``opened_at`` -> None;
                otherwise _move(HALF_OPEN, "open_timeout_elapsed") and continue below
        * HALF_OPEN: if probes_in_flight >= half_open_max_probes -> None;
                     else probes_in_flight += 1 and return Permit(name, generation, probe=True)
        * CLOSED: return Permit(name, generation, probe=False)
        """
        if self.state is BreakerState.OPEN:
            if now - self.opened_at < self.open_duration_s:
                return None
            self._move(now, BreakerState.HALF_OPEN, "open_timeout_elapsed")

        if self.state is BreakerState.HALF_OPEN:
            if self.probes_in_flight >= self.half_open_max_probes:
                return None
            self.probes_in_flight += 1
            return Permit(self.name, self.generation, probe=True)

        return Permit(self.name, self.generation, probe=False)

    def on_success(self, permit: Permit, now: float, latency_s: float) -> None:
        """
        * stale permit (permit.generation != self.generation) -> ignore
        * slow = latency_s >= slow_call_s
        * HALF_OPEN: free the probe slot; slow -> _move(OPEN, "probe_slow");
                     else probe_successes += 1 and close ("probes_succeeded") once
                     it reaches half_open_successes
        * CLOSED: _record(now, failed=False, slow=slow)
        """
        if permit.breaker != self.name or permit.generation != self.generation:
            return

        slow = latency_s >= self.slow_call_s
        if self.state is BreakerState.HALF_OPEN:
            if not permit.probe:
                return
            self.probes_in_flight = max(0, self.probes_in_flight - 1)
            if slow:
                self._move(now, BreakerState.OPEN, "probe_slow")
                return
            self.probe_successes += 1
            if self.probe_successes >= self.half_open_successes:
                self._move(now, BreakerState.CLOSED, "probes_succeeded")
            return

        if self.state is BreakerState.CLOSED and not permit.probe:
            self._record(now, failed=False, slow=slow)

    def on_failure(self, permit: Permit, now: float) -> None:
        """Only called for errors that count toward the breaker.

        Stale results are ignored; HALF_OPEN probes reopen the breaker and
        CLOSED results are added to the sliding window.
        """
        if permit.breaker != self.name or permit.generation != self.generation:
            return
        if self.state is BreakerState.HALF_OPEN:
            if permit.probe:
                self._move(now, BreakerState.OPEN, "probe_failed")
            return
        if self.state is BreakerState.CLOSED and not permit.probe:
            self._record(now, failed=True, slow=False)

    def release(self, permit: Permit, now: float) -> None:
        """The attempt ended without telling us anything about provider health
        (client error, rate limit, cancelled). Free the probe slot, record nothing.

        Ignore stale results; free an active HALF_OPEN probe slot without
        recording a health result.
        """
        if permit.breaker != self.name or permit.generation != self.generation:
            return
        if self.state is BreakerState.HALF_OPEN and permit.probe:
            self.probes_in_flight = max(0, self.probes_in_flight - 1)
