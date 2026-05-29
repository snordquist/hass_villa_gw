"""Event/state-mirror logic for the Villa GW coordinator.

Everything that turns signals (log-tail events, HA-side live-view marks,
the auto-clear ticker, the early-media probe) into state-mirror flags and
deduplicated HA bus events. Mixed into `VillaGwCoordinator`; relies on
shared coordinator state (`mqtt_bridge`, the transient flags, the
last-seen scalars, daily counters, `_last_event_at`, …).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from homeassistant.core import callback

from .const import (
    DOORBELL_PULSE_SECONDS,
    EVENT_CALL_ENDED,
    EVENT_CALL_INCOMING,
    EVENT_CLOUD_CONNECT,
    EVENT_CLOUD_MQTT_IN,
    EVENT_CLOUD_MQTT_OUT,
    EVENT_DOOR_UNLOCKED,
    EVENT_DOORBELL_RINGING,
    EVENT_LIVE_VIEW_ENDED,
    EVENT_LIVE_VIEW_STARTED,
    EVENT_RINGBACK,
    EVENT_STATE_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

# Debounce: if the same logical event arrives from both poller and log-tail
# within this window, ignore the second one.
DEDUP_WINDOW = 2.0  # seconds


class VillaGwEventsMixin:
    """Auto-clear ticker, live-view markers, log-event mapping, event firing."""

    # ─────────────────────────────────────────── auto-clear ticker
    @callback
    def _tick_auto_clear(self, _now: datetime | None = None) -> None:
        """Expire transient pulses independently of poll cadence.

        Doorbell pulse → DOORBELL_PULSE_SECONDS (wall-clock).
        Live-view → self._live_view_max_s (monotonic loop time).

        Must stay sync + decorated with @callback so HA dispatches it on
        the event loop (without that decorator HA classifies a plain
        sync function as an executor job and `async_set_updated_data`
        would run from a worker thread).
        """
        changed = False
        now_wall = datetime.now(timezone.utc)
        if self.doorbell_active and self.last_doorbell_at is not None:
            age = (now_wall - self.last_doorbell_at).total_seconds()
            if age > DOORBELL_PULSE_SECONDS:
                self.doorbell_active = False
                changed = True
        now_loop = self.hass.loop.time()
        if (
            self.live_view_active
            and self.live_view_started_at is not None
            and now_loop - self.live_view_started_at > self._live_view_max_s
        ):
            _LOGGER.debug("live_view auto-clear after %.0fs", self._live_view_max_s)
            self.live_view_active = False
            self.live_view_started_at = None
            changed = True
        if changed:
            self.async_set_updated_data(self.data or {})

    # ─────────────────────────────────────────── client-side live-view marker

    @callback
    def mark_live_view_started(self, source: str) -> None:
        """Flip live_view_active on without waiting for log/state evidence.

        The HA-direct `AT+B UART monitor` path on uart2d:10087 bypasses the
        avlink state machine — `AT+B APPLICATION` stays in `{state:1, call:[0]}`
        for the duration of a HA-initiated live view, so the poll-path can
        never observe it. We commit the flag locally as soon as the command
        is sent; `_tick_auto_clear` cleans it up after the configured
        duration + 30s grace.
        """
        self.live_view_active = True
        self.live_view_started_at = self.hass.loop.time()
        self.last_app_user = source
        self._fire(EVENT_LIVE_VIEW_STARTED, {"source": source})
        self.async_set_updated_data(self.data or {})

    @callback
    def mark_live_view_ended(self, source: str) -> None:
        """Clear live_view_active on explicit HA-side stop."""
        was_active = self.live_view_active
        self.live_view_active = False
        self.live_view_started_at = None
        if was_active:
            self._fire(EVENT_LIVE_VIEW_ENDED, {"source": source})
        self.async_set_updated_data(self.data or {})

    # ─────────────────────────────────────────── log-tail callback (fast path)

    # Map parsed log-event type → HA bus event name
    _LOG_EVENT_MAP = {
        "doorbell_ringing":   EVENT_DOORBELL_RINGING,
        "call_incoming":      EVENT_CALL_INCOMING,
        "call_ended":         EVENT_CALL_ENDED,
        "ringback":           EVENT_RINGBACK,
        "live_view_started":  EVENT_LIVE_VIEW_STARTED,
        "live_view_ended":    EVENT_LIVE_VIEW_ENDED,
        "live_view_state":    None,   # transitional — no HA event
        # `monitor_response=ok` is a backup signal for the log-tail path —
        # when the app-side `AT+B UART monitor` succeeds we treat it as a
        # live_view_started, since the polling path (AT+B APPLICATION) cannot
        # see HA/app-initiated monitors that bypass the avlink state machine.
        "monitor_response":   EVENT_LIVE_VIEW_STARTED,
        "state_timeout":      EVENT_STATE_TIMEOUT,
        "door_unlocked":      EVENT_DOOR_UNLOCKED,
        "cloud_mqtt_in":      EVENT_CLOUD_MQTT_IN,
        "cloud_mqtt_out":     EVENT_CLOUD_MQTT_OUT,
        "cloud_connect":      EVENT_CLOUD_CONNECT,
    }

    async def _on_log_event(self, event: dict) -> None:
        """Translate a log-tail event into our HA event vocabulary.

        Side effects:
        - Update state-mirror fields (live_view_active, doorbell_active, …)
        - Update last-seen scalars and daily counters
        - Fire HA bus event (deduped)
        - Forward to MQTT-bridge if active
        """
        etype = event.get("type")
        wall_now = datetime.now(timezone.utc)
        loop_now = self.hass.loop.time()

        # State mirror + bookkeeping.
        # Counters (doorbell/call/unlock) are incremented from the POLL-PATH
        # transition handler (`_on_state_transition`) to avoid double-counting
        # when both poll-loop and log-tail are active. Door-unlocked is the
        # exception: it has no poll-path equivalent (no state-machine flip),
        # so we count it here.
        if etype == "doorbell_ringing":
            self.doorbell_active = True
            self.last_doorbell_at = wall_now
        elif etype == "call_incoming":
            self.call_active = True
            remote = event.get("remote_addr")
            if remote:
                self.last_caller = remote
        elif etype == "call_ended":
            # C2: also clear doorbell_active so telnet-only setups (no poll
            # loop transitions) don't keep it stuck until the 10s pulse-timer
            # eventually expires.
            self.doorbell_active = False
            self.call_active = False
            self.live_view_active = False
            self.live_view_started_at = None
            self.outdoor_station_ringing = False
        elif etype == "live_view_started":
            self.live_view_active = True
            self.live_view_started_at = loop_now
            from_uuid = event.get("from")
            if from_uuid:
                self.last_app_user = from_uuid
        elif etype == "live_view_ended":
            self.live_view_active = False
            self.live_view_started_at = None
        elif etype == "monitor_response":
            # Backup channel: a successful `AT+B UART monitor` ACK (regardless
            # of initiator). Only mirror state on `response=ok`; failures
            # leave the flag where it was.
            if event.get("response") == "ok":
                self.live_view_active = True
                self.live_view_started_at = loop_now
        elif etype == "ringback":
            self.outdoor_station_ringing = event.get("state", 0) == 1
        elif etype == "door_unlocked":
            self.last_unlock_at = wall_now
            self.unlock_count_today += 1
        elif etype == "cloud_connect":
            new_online = event.get("status") == "ok"
            if new_online != self.cloud_online:
                _LOGGER.info(
                    "Villa GW cloud link → %s (log: mqtt connect %s)",
                    "online" if new_online else "offline", event.get("status"),
                )
            self.cloud_online = new_online
        elif etype == "cloud_mqtt_in":
            payload = event.get("payload") or {}
            from_uuid = payload.get("from") if isinstance(payload, dict) else None
            if from_uuid:
                self.last_app_user = from_uuid

        # Push entity updates
        self.async_set_updated_data(self.data or {})

        # Fire HA bus event (with dedup against polling-loop).
        # `monitor_response` is mapped to live_view_started only when the
        # ACK is positive — gate that here rather than encoding it in the map.
        ha_event = self._LOG_EVENT_MAP.get(etype)
        if ha_event:
            if etype == "monitor_response" and event.get("response") != "ok":
                ha_event = None
        if ha_event:
            # Tag ring events with `source="log"` so the diagnostic sensor
            # `last_ring_source` can show which path won the dedup race.
            # Only set if upstream didn't already supply one — keeps the
            # log dict authoritative on its own fields.
            payload = dict(event)
            if ha_event == EVENT_DOORBELL_RINGING and "source" not in payload:
                payload["source"] = "log"
            self._fire(ha_event, payload)

        # Forward to MQTT-bridge if enabled
        if self.mqtt_bridge:
            try:
                await self.mqtt_bridge.publish_event(event)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("MQTT-bridge publish failed: %s", err)

    # ─────────────────────────────────────────── event firing with dedup

    def _fire(self, event_type: str, payload: dict) -> None:
        now = self.hass.loop.time()
        last = self._last_event_at.get(event_type, 0.0)
        if now - last < DEDUP_WINDOW:
            return  # already fired recently (from the other source)
        self._last_event_at[event_type] = now
        # Track last fired ring source for the diagnostic sensor — only the
        # winning path updates the field; suppressed duplicates don't.
        source_changed = False
        if event_type == EVENT_DOORBELL_RINGING:
            source = payload.get("source")
            if isinstance(source, str) and source != self.last_ring_source:
                self.last_ring_source = source
                source_changed = True
        self.hass.bus.async_fire(event_type, payload)
        # Push a coordinator update so the `last_ring_source` sensor refreshes
        # immediately instead of waiting for the next poll-tick.
        if source_changed:
            self.async_set_updated_data(self.data or {})
