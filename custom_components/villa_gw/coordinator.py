"""Villa GW data coordinator.

Two parallel tasks:

1. **State poller** — every ``poll_interval_ms`` (default 1 s), queries
   `AT+B APPLICATION` and `AT+B SYSTEM` on avlink:10086, detects transitions,
   and fires HA bus events. This is the **robust** event channel — it uses the
   structured JSON the device returns to its own web UI, so it survives
   firmware updates that only change log strings.

2. **Log tail** — *optional* fast-path via telnet:23, only enabled when
   ``CONF_ENABLE_LOG_TAIL`` is true. Adds sub-100 ms event delivery on top
   of the polling baseline. Same HA events are fired; the binary_sensor
   deduplicates via debouncing.

A periodic ``DataUpdateCoordinator`` refresh (60 s) covers slow-moving sensors
(uptime, mem, firmware) without paying the polling cost for them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._backoff import Backoff
from .api import VillaGwClient, VillaGwConnectionError
from .const import (
    BACKOFF_FACTOR,
    BACKOFF_INITIAL_S,
    BACKOFF_JITTER,
    BACKOFF_MAX_S,
    CONF_ENABLE_LOG_TAIL,
    CONF_ENABLE_MQTT_BRIDGE,
    CONF_LIVE_VIEW_DURATION,
    CONF_MQTT_BASE_TOPIC,
    CONF_OUTDOOR_ADDRESS,
    CONF_POLL_INTERVAL_MS,
    DEFAULT_ENABLE_LOG_TAIL,
    DEFAULT_ENABLE_MQTT_BRIDGE,
    DEFAULT_LIVE_VIEW_DURATION,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_OUTDOOR_ADDRESS,
    DEFAULT_POLL_INTERVAL_MS,
    DOMAIN,
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
    EVENT_STATE_CHANGED,
    EVENT_STATE_TIMEOUT,
    SENSOR_REFRESH_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Debounce: if the same logical event arrives from both poller and log-tail
# within this window, ignore the second one.
DEDUP_WINDOW = 2.0  # seconds


class VillaGwCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the client + two event-source tasks + the slow sensor refresh."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: VillaGwClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"Villa GW {client.host}",
            update_interval=SENSOR_REFRESH_INTERVAL,
        )
        self.entry = entry
        self.client = client

        # Bg-tasks
        self._poll_task: asyncio.Task | None = None
        self._tail_task: asyncio.Task | None = None

        # Optional MQTT-Bridge (set up by async_start_tasks if enabled)
        self.mqtt_bridge = None  # type: ignore[assignment]

        # Latest snapshot of application state — drives entity properties
        self.app_state: dict[str, Any] = {}  # {state, sip, call}
        self.sys_state: dict[str, Any] = {}  # {door_in, mem, …}

        # Connectivity: false while poll-loop is in a sustained-failure backoff.
        # Updates a `binary_sensor.gateway_online` entity.
        self.gateway_online = False

        # Live transient flags driven by the log-tail + poll loop
        self.live_view_active = False
        self.doorbell_active = False
        self.call_active = False
        self.outdoor_station_ringing = False
        self.cloud_online = False  # from last `mqtt connect ok` event

        # Last-seen scalars (drive sensor.last_*)
        self.last_doorbell_at: float | None = None
        self.last_caller: str | None = None     # remote_addr from on_incoming_call
        self.last_app_user: str | None = None   # from-UUID of last MQTT monitor
        self.last_unlock_at: float | None = None
        # When live-view was started; used to auto-clear after the configured
        # duration + grace period if no explicit hang-event ever arrives.
        self.live_view_started_at: float | None = None

        # Daily counters (reset at midnight by sensor.py)
        self.doorbell_count_today = 0
        self.unlock_count_today = 0
        self.call_count_today = 0

        # Event-dedup bookkeeping
        self._last_event_at: dict[str, float] = {}

    # ─────────────────────────────────────────── background-task lifecycle

    async def async_start_tasks(self) -> None:
        opts = {**self.entry.data, **self.entry.options}
        interval = opts.get(CONF_POLL_INTERVAL_MS, DEFAULT_POLL_INTERVAL_MS) / 1000.0
        self._poll_task = self.hass.async_create_background_task(
            self._poll_loop(interval),
            name=f"villa_gw_poll_{self.client.host}",
        )
        if opts.get(CONF_ENABLE_LOG_TAIL, DEFAULT_ENABLE_LOG_TAIL):
            self._tail_task = self.hass.async_create_background_task(
                self.client.stream_log_events(self._on_log_event),
                name=f"villa_gw_tail_{self.client.host}",
            )

        # Optional MQTT-bridge
        if opts.get(CONF_ENABLE_MQTT_BRIDGE, DEFAULT_ENABLE_MQTT_BRIDGE):
            # Lazy import — only need this code path if user opts in
            from .mqtt_bridge import VillaGwMqttBridge  # noqa: PLC0415

            self.mqtt_bridge = VillaGwMqttBridge(
                hass=self.hass,
                base_topic=opts.get(CONF_MQTT_BASE_TOPIC, DEFAULT_MQTT_BASE_TOPIC),
                device_id=self.entry.unique_id or self.entry.entry_id,
                client=self.client,
                outdoor_address=opts.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS),
                live_view_duration=opts.get(
                    CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION
                ),
            )
            await self.mqtt_bridge.async_start()

    async def async_stop_tasks(self) -> None:
        for task in (self._poll_task, self._tail_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._tail_task = None
        if self.mqtt_bridge:
            await self.mqtt_bridge.async_stop()
            self.mqtt_bridge = None

    # ─────────────────────────────────────────── polling loop (robust path)

    async def _poll_loop(self, interval: float) -> None:
        """Poll `AT+B APPLICATION` every `interval`s, detect state transitions.

        Robustness:
        - On transient failures we keep polling at `interval` (the cadence is
          already slow enough to be polite). After ``POLL_BACKOFF_THRESHOLD``
          consecutive failures we switch into capped exponential backoff so we
          don't hammer an unreachable / rebooting GW.
        - `gateway_online` flag mirrors the success/failure state for entities.
        - Auto-clears `live_view_active` after `live_view_duration + 30s` so it
          doesn't get stuck ON if we miss the hang-event.
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
        opts = {**self.entry.data, **self.entry.options}
        live_view_max_s = opts.get(CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION) + 30
        failures = 0
        while True:
            # Auto-clear stuck live_view_active flag
            if (
                self.live_view_active
                and self.live_view_started_at is not None
                and self.hass.loop.time() - self.live_view_started_at > live_view_max_s
            ):
                _LOGGER.debug("live_view auto-clear after %.0fs", live_view_max_s)
                self.live_view_active = False
                self.live_view_started_at = None
                self.async_set_updated_data(self.data or {})
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
        """Classify a state change and fire matching HA events."""
        prev_has_call = bool(prev_call) and any(c for c in prev_call)
        new_has_call = bool(new_call) and any(c for c in new_call)

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
        # We don't know the exact enum value semantics yet; we'll treat any
        # "no call → call" transition as a ring/incoming event for v0.1.
        if not prev_has_call and new_has_call:
            self.doorbell_active = True
            self.call_active = True
            self._fire(EVENT_CALL_INCOMING, {"state": new_state, "call": new_call})
            self._fire(EVENT_DOORBELL_RINGING, {"state": new_state, "call": new_call})

        # Call ended
        if prev_has_call and not new_has_call:
            self.doorbell_active = False
            self.call_active = False
            self.live_view_active = False
            self._fire(EVENT_CALL_ENDED, {"state": new_state})
            if self.live_view_active:
                self._fire(EVENT_LIVE_VIEW_ENDED, {"state": new_state})

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
        "monitor_response":   None,   # we infer live_view_* from state instead
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
        now = self.hass.loop.time()

        # State mirror + bookkeeping
        if etype == "doorbell_ringing":
            self.doorbell_active = True
            self.last_doorbell_at = now
            self.doorbell_count_today += 1
        elif etype == "call_incoming":
            self.call_active = True
            remote = event.get("remote_addr")
            if remote:
                self.last_caller = remote
            self.call_count_today += 1
        elif etype == "call_ended":
            self.call_active = False
            self.live_view_active = False
            self.outdoor_station_ringing = False
        elif etype == "live_view_started":
            self.live_view_active = True
            self.live_view_started_at = now
            from_uuid = event.get("from")
            if from_uuid:
                self.last_app_user = from_uuid
        elif etype == "live_view_ended":
            self.live_view_active = False
            self.live_view_started_at = None
        elif etype == "ringback":
            self.outdoor_station_ringing = event.get("state", 0) == 1
        elif etype == "door_unlocked":
            self.last_unlock_at = now
            self.unlock_count_today += 1
        elif etype == "cloud_connect":
            self.cloud_online = event.get("status") == "ok"
        elif etype == "cloud_mqtt_in":
            payload = event.get("payload") or {}
            from_uuid = payload.get("from") if isinstance(payload, dict) else None
            if from_uuid:
                self.last_app_user = from_uuid

        # Push entity updates
        self.async_set_updated_data(self.data or {})

        # Fire HA bus event (with dedup against polling-loop)
        ha_event = self._LOG_EVENT_MAP.get(etype)
        if ha_event:
            self._fire(ha_event, event)

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
        self.hass.bus.async_fire(event_type, payload)

    # ─────────────────────────────────────────── slow sensor refresh

    async def _async_update_data(self) -> dict[str, Any]:
        """Slow refresh — system + video state. Robust against partial failures.

        AT+B SYSTEM and the REST /api/video are independent calls; if one fails
        we still publish the other. Prior versions raised UpdateFailed on the
        first error, leaving the system sensors permanently 'unknown' if
        avlink:10086 was briefly unreachable during startup.
        """
        sys: dict | None = None
        video: dict | None = None
        try:
            sys = await self.client.system_status()
            self.sys_state = sys
        except VillaGwConnectionError as err:
            _LOGGER.debug("system_status fetch failed: %s", err)
        try:
            video = await self.client.video_config()
        except VillaGwConnectionError as err:
            _LOGGER.debug("video_config fetch failed: %s", err)

        # If we have NO data at all (both failed) AND we never got anything
        # before, the entities will stay "unknown" — that's the expected
        # behaviour, but we still need to keep the coordinator alive so the
        # poll-loop transitions still fire entity updates.
        data = {
            "system": sys or self.sys_state or {},
            "video": video or (self.data or {}).get("video") or {},
            "application": self.app_state,
            "live_view_active": self.live_view_active,
            "doorbell_active": self.doorbell_active,
            "call_active": self.call_active,
            "outdoor_station_ringing": self.outdoor_station_ringing,
            "cloud_online": self.cloud_online,
        }
        if self.mqtt_bridge and sys:
            try:
                await self.mqtt_bridge.publish_system(sys)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("MQTT system mirror failed: %s", err)
        return data


def get_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> VillaGwCoordinator:
    return hass.data[DOMAIN][entry.entry_id]["coordinator"]
