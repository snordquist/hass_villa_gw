"""Polling loop for the Villa GW coordinator (robust event path).

Polls `AT+B APPLICATION` on avlink:10086 every ``poll_interval_ms``,
detects call/state transitions and fires HA events. Mixed into
`VillaGwCoordinator`; relies on shared coordinator state/helpers
(`client`, `mqtt_bridge`, `gateway_online`, `cloud_online`, `_fire`,
`async_set_updated_data`, …).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ._backoff import Backoff
from .api import VillaGwConnectionError
from .const import (
    BACKOFF_FACTOR,
    BACKOFF_INITIAL_S,
    BACKOFF_JITTER,
    BACKOFF_MAX_S,
    CLOUD_STATUS_INTERVAL_S,
    EVENT_CALL_ENDED,
    EVENT_CALL_INCOMING,
    EVENT_DOORBELL_RINGING,
    EVENT_LIVE_VIEW_ENDED,
    EVENT_STATE_CHANGED,
)

_LOGGER = logging.getLogger(__name__)


class VillaGwPollMixin:
    """avlink poll loop + state-transition classifier."""

    async def _poll_loop(self, interval: float) -> None:
        """Poll `AT+B APPLICATION` every `interval`s, detect state transitions.

        Robustness:
        - On transient failures we keep polling at `interval` (the cadence is
          already slow enough to be polite). After ``POLL_BACKOFF_THRESHOLD``
          consecutive failures we switch into capped exponential backoff so we
          don't hammer an unreachable / rebooting GW.
        - `gateway_online` flag mirrors the success/failure state for entities.

        Auto-clears for stuck `doorbell_active` / `live_view_active` flags
        live in `_tick_auto_clear()` on a separate 1s timer — that keeps
        them ticking even when this loop is sleeping in a long exp-backoff.
        """
        prev_state: int | None = None
        prev_call: list[int] | None = None
        bo = Backoff(
            initial=max(BACKOFF_INITIAL_S, interval),
            factor=BACKOFF_FACTOR,
            cap=BACKOFF_MAX_S,
            jitter=BACKOFF_JITTER,
        )
        POLL_BACKOFF_THRESHOLD = 3
        failures = 0
        while True:
            try:
                app = await self.client.application()
                # success → flip online + reset backoff
                was_offline = not self.gateway_online
                if was_offline or failures:
                    _LOGGER.info(
                        "Villa GW reachable again at %s (after %d failures)",
                        self.client.host, failures,
                    )
                self.gateway_online = True
                failures = 0
                bo.reset()
                if was_offline and self.mqtt_bridge:
                    try:
                        await self.mqtt_bridge.publish_availability(True)
                    except Exception:  # noqa: BLE001
                        pass
                self.app_state = app
                state = int(app.get("state", 0))
                call = list(app.get("call", [0]) or [0])

                if prev_state is not None and (state != prev_state or call != prev_call):
                    await self._on_state_transition(prev_state, prev_call, state, call)
                prev_state, prev_call = state, call

                # Authoritative GW↔Cloud link status (throttled; self-healing
                # across restarts). The log-tail `cloud_connect` event updates
                # this instantly between polls — this slower read is the
                # backstop that fixes the stale-`off`-after-restart bug.
                nowm = self.hass.loop.time()
                if nowm - self._last_cloud_check >= CLOUD_STATUS_INTERVAL_S:
                    self._last_cloud_check = nowm
                    try:
                        online = await self.client.cloud_link_online()
                        if online != self.cloud_online:
                            _LOGGER.info(
                                "Villa GW cloud link → %s (GW /api/sip)",
                                "online" if online else "offline",
                            )
                        self.cloud_online = online
                    except VillaGwConnectionError as err:
                        # A web hiccup must neither flip the sensor nor fail
                        # the poll (the avlink read already succeeded) — keep
                        # the last known value.
                        _LOGGER.debug(
                            "cloud-link status poll failed, keeping %s: %s",
                            self.cloud_online, err,
                        )

                self.async_set_updated_data(self.data or {})

                # MQTT-bridge: mirror state to villa_gw/<did>/state
                if self.mqtt_bridge:
                    try:
                        await self.mqtt_bridge.publish_state(app)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("MQTT state mirror failed: %s", err)

                sleep_for = interval
            except VillaGwConnectionError as err:
                failures += 1
                if failures >= POLL_BACKOFF_THRESHOLD:
                    sleep_for = bo.next_delay()
                    if self.gateway_online:
                        self.gateway_online = False
                        self.async_set_updated_data(self.data or {})
                        if self.mqtt_bridge:
                            try:
                                await self.mqtt_bridge.publish_availability(False)
                            except Exception:  # noqa: BLE001
                                pass
                    if bo.failure_count == 1 or bo.failure_count % 10 == 0:
                        _LOGGER.warning(
                            "Poll failed %d× — sustained backoff %.1fs: %s",
                            failures, sleep_for, err,
                        )
                else:
                    # first few failures: just retry at normal cadence
                    sleep_for = interval
                    if failures == 1:
                        _LOGGER.info("Poll transient failure: %s — retrying", err)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in poll loop")
                sleep_for = max(interval, BACKOFF_INITIAL_S)
            await asyncio.sleep(sleep_for)

    async def _on_state_transition(
        self,
        prev_state: int,
        prev_call: list[int] | None,
        new_state: int,
        new_call: list[int],
    ) -> None:
        """Classify a state change and fire matching HA events.

        Also runs from the polling path (no telnet-tail needed), so daily
        counters increment even without log-tail. Dedup via _fire().
        """
        prev_has_call = bool(prev_call) and any(c for c in prev_call)
        new_has_call = bool(new_call) and any(c for c in new_call)
        was_live = self.live_view_active

        # Always fire the generic transition event — automations can match on this
        self._fire(
            EVENT_STATE_CHANGED,
            {
                "from_state": prev_state,
                "to_state": new_state,
                "from_call": prev_call,
                "to_call": new_call,
            },
        )

        # Call started — could be incoming (doorbell) or outgoing.
        # We treat any "no call → call" transition as ring/incoming for v0.x.
        if not prev_has_call and new_has_call:
            self.doorbell_active = True
            self.call_active = True
            self.last_doorbell_at = datetime.now(timezone.utc)
            # Counters increment here (poll-path) AND in _on_log_event
            # (log-tail-path); the _fire() dedup window keeps each visible
            # event single, but we want the counter to bump exactly once per
            # logical event so we only do it here in the poll path.
            self.doorbell_count_today += 1
            self.call_count_today += 1
            self._fire(EVENT_CALL_INCOMING, {"state": new_state, "call": new_call})
            self._fire(
                EVENT_DOORBELL_RINGING,
                {"source": "poll", "state": new_state, "call": new_call},
            )

        # Call ended
        if prev_has_call and not new_has_call:
            self.doorbell_active = False
            self.call_active = False
            self.live_view_active = False
            self.live_view_started_at = None
            self._fire(EVENT_CALL_ENDED, {"state": new_state})
            if was_live:
                self._fire(EVENT_LIVE_VIEW_ENDED, {"state": new_state})
