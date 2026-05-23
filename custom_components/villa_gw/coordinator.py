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
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ._backoff import Backoff
from .api import VillaGwClient, VillaGwConnectionError
from .const import (
    BACKOFF_FACTOR,
    BACKOFF_INITIAL_S,
    BACKOFF_JITTER,
    BACKOFF_MAX_S,
    CONF_CACHED_SIP_ID,
    CONF_CACHED_SIP_PASSWORD,
    CONF_CACHED_SIP_SERVER,
    CONF_ENABLE_CLOUD,
    CONF_ENABLE_LOG_TAIL,
    DOORBELL_PULSE_SECONDS,
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
        self.cloud_online = False  # from last `mqtt connect ok` event
        # Cloud SIP REGISTER session health — drives binary_sensor.cloud_sip_connected
        self.cloud_sip_connected = False
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

    # ─────────────────────────────────────────── polling loop (robust path)

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
            self.cloud_online = event.get("status") == "ok"
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

    # ─────────────────────────────────────────── Cloud SIP listener

    async def _sip_loop(
        self, *, server: str, user: str, password: str,
    ) -> None:
        """Maintain a TLS SIP-REGISTER session with the iLifestyle Cloud.

        Reconnects with capped exponential backoff on any disconnect or
        REGISTER failure. While registered, INVITE-on-the-wire fires
        `EVENT_DOORBELL_RINGING` via the same `_fire()` dedup as poll/log.
        """
        # Lazy import so test environments without the sip_client module
        # don't hard-fail at coordinator-import time.
        from .sip_client import SipClient, TlsSipTransport  # noqa: PLC0415

        bo = Backoff(
            initial=BACKOFF_INITIAL_S, factor=BACKOFF_FACTOR,
            cap=BACKOFF_MAX_S, jitter=BACKOFF_JITTER,
        )
        while True:
            transport: TlsSipTransport | None = None
            try:
                transport = await TlsSipTransport.connect(server)
                client = SipClient(
                    server=server, user=user, password=password,
                    transport=transport, on_invite=self._on_sip_invite,
                )
                ok = await client.register_once()
                self.cloud_sip_connected = ok
                self.async_set_updated_data(self.data or {})
                if not ok:
                    raise RuntimeError("SIP REGISTER rejected")
                bo.reset()
                _LOGGER.info("Villa GW SIP-listener registered at %s", server)
                await client.run()  # loops forever until exception
            except asyncio.CancelledError:
                self.cloud_sip_connected = False
                raise
            except Exception as err:  # noqa: BLE001
                self.cloud_sip_connected = False
                self.async_set_updated_data(self.data or {})
                delay = bo.next_delay()
                _LOGGER.warning(
                    "SIP listener disconnected (%s) — reconnecting in %.0fs",
                    err, delay,
                )
                await asyncio.sleep(delay)
            finally:
                if transport is not None:
                    try:
                        await transport.close()
                    except Exception:  # noqa: BLE001
                        pass

    @callback
    def _on_sip_invite(self, info: dict[str, str]) -> None:
        """Callback from SipClient on each incoming INVITE.

        Fires the canonical ring event with `source="sip"`. The `_fire()`
        dedup handles the case where poll/log already saw the same ring
        within DEDUP_WINDOW.
        """
        self._fire(
            EVENT_DOORBELL_RINGING,
            {
                "source":   "sip",
                "call_id":  info.get("call_id", ""),
                "from_sip": info.get("from_sip", ""),
                "to_sip":   info.get("to_sip", ""),
            },
        )

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
