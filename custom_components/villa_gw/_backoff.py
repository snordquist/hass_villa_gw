"""Capped exponential backoff helper.

Used by long-lived connection loops (telnet log tail, polling) so they
recover cleanly from transient network issues without hammering the gateway.

Usage:
    bo = Backoff(initial=2.0, factor=2.0, cap=300.0, jitter=0.25)
    while running:
        try:
            await do_work()
            bo.reset()              # success → next failure starts at `initial`
        except Exception as err:
            delay = bo.next_delay()
            _LOGGER.warning("Failed (%s) — retry in %.1fs", err, delay)
            await asyncio.sleep(delay)
"""

from __future__ import annotations

import random


class Backoff:
    """Capped exponential backoff with jitter.

    - ``initial``: delay (s) after the first failure
    - ``factor``: multiplier on each consecutive failure
    - ``cap``: absolute maximum delay (s)
    - ``jitter``: ±fraction of the delay added randomly (e.g. 0.25 = ±25%)
    """

    def __init__(
        self,
        initial: float = 2.0,
        factor: float = 2.0,
        cap: float = 300.0,
        jitter: float = 0.25,
    ) -> None:
        if initial <= 0 or factor < 1 or cap < initial:
            raise ValueError("invalid backoff parameters")
        self._initial = initial
        self._factor = factor
        self._cap = cap
        self._jitter = max(0.0, jitter)
        self._failures = 0

    @property
    def failure_count(self) -> int:
        return self._failures

    def reset(self) -> None:
        """Successful operation — restart at the initial delay on next failure."""
        self._failures = 0

    def next_delay(self) -> float:
        """Increment the failure counter and return the next sleep delay (seconds)."""
        delay = self._initial * (self._factor ** self._failures)
        delay = min(delay, self._cap)
        if self._jitter:
            spread = delay * self._jitter
            delay = max(0.1, delay + random.uniform(-spread, spread))
        self._failures += 1
        return delay
