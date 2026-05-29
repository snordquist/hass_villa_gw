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
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import VillaGwClient, VillaGwConnectionError
from .const import (
    CONF_CACHED_SIP_ID,
    CONF_CACHED_SIP_PASSWORD,
    CONF_CACHED_SIP_SERVER,
    CONF_ENABLE_CLOUD,
    CONF_ENABLE_LOG_TAIL,
    CONF_ENABLE_MQTT_BRIDGE,
    CONF_ENABLE_MQTT_DISCOVERY,
    CONF_LIVE_VIEW_DURATION,
    CONF_MQTT_BASE_TOPIC,
    CONF_OUTDOOR_ADDRESS,
    CONF_POLL_INTERVAL_MS,
    DEFAULT_ENABLE_CLOUD,
    DEFAULT_ENABLE_LOG_TAIL,
    DEFAULT_ENABLE_MQTT_BRIDGE,
    DEFAULT_ENABLE_MQTT_DISCOVERY,
    DEFAULT_LIVE_VIEW_DURATION,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_OUTDOOR_ADDRESS,
    DEFAULT_POLL_INTERVAL_MS,
    DOMAIN,
    SENSOR_REFRESH_INTERVAL,
)
from .coordinator_events import VillaGwEventsMixin
from .coordinator_poll import VillaGwPollMixin
from .coordinator_sip import VillaGwSipMixin

_LOGGER = logging.getLogger(__name__)


class VillaGwCoordinator(
    VillaGwPollMixin,
    VillaGwEventsMixin,
    VillaGwSipMixin,
    DataUpdateCoordinator[dict[str, Any]],
):
    """Owns the client + event-source tasks + the slow sensor refresh.

    Orchestration only — the avlink poll loop (`coordinator_poll`), the
    event/state-mirror logic (`coordinator_events`) and the Cloud SIP
    listener (`coordinator_sip`) live in their own mixin modules.
    """

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
        self._sip_task: asyncio.Task | None = None
        # Auto-clear ticker — runs independently of the poll loop so doorbell
        # / live-view pulses still expire when the GW is offline and the poll
        # loop is in a long exponential backoff. Initialized here so the
        # ticker can read it even before async_start_tasks runs.
        self._auto_clear_unsub = None  # type: ignore[assignment]
        self._live_view_max_s = 60  # set properly in async_start_tasks

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
        # GW↔Cloud backend link. Authoritative source = the poll loop reading
        # `/api/sip` online (self-healing across restarts); the log-tail
        # `cloud_connect` event is a fast-path update between those polls.
        self.cloud_online = False
        self._last_cloud_check = 0.0  # monotonic ts of last /api/sip poll
        # Cloud SIP REGISTER session health — drives binary_sensor.cloud_sip_connected
        self.cloud_sip_connected = False
        # One-shot Early-Media probe (Schritt 2). Armed via the diagnostic
        # button; the SIP listener replies 183+SDP to the NEXT ring and
        # measures early-media RTP. Auto-disarms after 5 min or after one ring.
        self.early_probe_armed = False
        self._early_probe_armed_at = 0.0
        self.last_probe_result: str | None = None
        # Last source that fired a ring event ("sip"|"log"|"poll"|None).
        # Updated each time `_fire(EVENT_DOORBELL_RINGING, …)` actually fires
        # (not when it dedups), so the diagnostic sensor shows whichever
        # path won the race.
        self.last_ring_source: str | None = None

        # Last-seen scalars (drive sensor.last_*).
        # Stored as wall-clock datetimes — sensors return them directly, so the
        # value is stable across coordinator updates (avoids recorder churn).
        self.last_doorbell_at: datetime | None = None
        self.last_caller: str | None = None     # remote_addr from on_incoming_call
        self.last_app_user: str | None = None   # from-UUID of last MQTT monitor
        self.last_unlock_at: datetime | None = None
        # When live-view was started; used to auto-clear after the configured
        # duration + grace period if no explicit hang-event ever arrives.
        # Loop-time (monotonic) since this is used for timeout math, not display.
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
        # Decoupled auto-clear tick: 1s cadence, independent of poll backoff.
        # Keeps doorbell pulse + stuck live-view-flag from lingering when the
        # GW is offline and the poll loop is in a long exponential backoff.
        # IMPORTANT: pass the bound @callback method directly — wrapping it
        # in a lambda would lose the @callback marker, and HA would run the
        # tick from an executor thread (where async_set_updated_data is unsafe
        # in 2026.x and beyond).
        self._live_view_max_s = (
            opts.get(CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION) + 30
        )
        self._auto_clear_unsub = async_track_time_interval(
            self.hass,
            self._tick_auto_clear,
            timedelta(seconds=1),
        )
        if opts.get(CONF_ENABLE_LOG_TAIL, DEFAULT_ENABLE_LOG_TAIL):
            self._tail_task = self.hass.async_create_background_task(
                self.client.stream_log_events(self._on_log_event),
                name=f"villa_gw_tail_{self.client.host}",
            )

        # Optional Cloud SIP listener — registers as a 2nd App-User at
        # `de.ilifestyle-cloud.com:5061` and receives forked SIP-INVITEs on
        # doorbell rings. Silent-mode only: never sends a SIP response to
        # INVITE (verified 2026-05-23 — any active response makes Cloud
        # treat our branch as the responsible endpoint, breaking the
        # parallel iPhone-fork).
        if opts.get(CONF_ENABLE_CLOUD, DEFAULT_ENABLE_CLOUD):
            sip_id = opts.get(CONF_CACHED_SIP_ID)
            sip_pw = opts.get(CONF_CACHED_SIP_PASSWORD)
            sip_server = opts.get(CONF_CACHED_SIP_SERVER)
            if sip_id and sip_pw and sip_server:
                self._sip_task = self.hass.async_create_background_task(
                    self._sip_loop(
                        server=sip_server, user=sip_id, password=sip_pw,
                    ),
                    name=f"villa_gw_sip_{self.client.host}",
                )
            else:
                _LOGGER.warning(
                    "Cloud enabled but cached SIP creds missing — "
                    "reconfigure the integration to fetch them",
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
                publish_discovery=opts.get(
                    CONF_ENABLE_MQTT_DISCOVERY, DEFAULT_ENABLE_MQTT_DISCOVERY
                ),
            )
            await self.mqtt_bridge.async_start()

    async def async_stop_tasks(self) -> None:
        if self._auto_clear_unsub is not None:
            self._auto_clear_unsub()
            self._auto_clear_unsub = None
        for task in (self._poll_task, self._tail_task, self._sip_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._tail_task = None
        self._sip_task = None
        if self.mqtt_bridge:
            await self.mqtt_bridge.async_stop()
            self.mqtt_bridge = None

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
