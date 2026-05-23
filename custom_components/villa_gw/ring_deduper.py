"""Cross-source ring-event deduplication.

The coordinator may receive ring signals from multiple parallel paths:
  - `sip`:  Cloud SIP-INVITE forwarded by `de.ilifestyle-cloud.com`
  - `log`:  Telnet log-tail picking up `call_btn_trigger`
  - `poll`: AT+B APPLICATION state-machine transition into RINGING

Each press should fire ONE `EVENT_DOORBELL_RINGING` regardless of how
many paths observe it. `RingDeduper` is the pure decider — caller passes
in a monotonic clock value so tests stay deterministic and the
production caller uses `hass.loop.time()`.
"""

from __future__ import annotations


class RingDeduper:
    """Suppresses duplicate ring signals within `window_s` seconds.

    The window is measured from the LAST fired signal, not the last
    observed one — so a chain of suppressed duplicates does not extend
    the dedup window indefinitely.
    """

    def __init__(self, window_s: float = 3.0) -> None:
        self._window_s = window_s
        self._last_fired_at: float | None = None
        self._last_source: str | None = None

    @property
    def last_fired_at(self) -> float | None:
        return self._last_fired_at

    @property
    def last_source(self) -> str | None:
        return self._last_source

    def should_fire(self, source: str, *, now: float) -> bool:
        """Return True iff this signal should propagate to HA's bus.

        `now` must be monotonic (e.g. `hass.loop.time()`). Calling with a
        smaller `now` than a previous call is undefined behaviour — the
        caller is expected to use the same clock throughout.
        """
        if self._last_fired_at is None or (now - self._last_fired_at) >= self._window_s:
            self._last_fired_at = now
            self._last_source = source
            return True
        return False
