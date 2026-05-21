"""Optional MQTT-bridge to Home Assistant's Mosquitto broker.

When enabled, the coordinator forwards every parsed log event to MQTT topics
and subscribes to `<base>/<did>/cmd/*` for incoming commands. This lets
NodeRED, external dashboards or other automations consume the GW's events
without writing a Home Assistant custom component.

Topic layout (defaults to base="villa_gw", did=lowercased MAC):

    villa_gw/<did>/availability         online | offline       (retained)
    villa_gw/<did>/state                {state,sip,call}        (retained, JSON)
    villa_gw/<did>/system               {uptime, mem, fw, …}    (retained, JSON)
    villa_gw/<did>/event/<slug>         <json payload>          (NOT retained)
    villa_gw/<did>/cloud/in             raw cloud-MQTT messages (NOT retained)
    villa_gw/<did>/cloud/out            …
    villa_gw/<did>/cloud/connect        ok|fail|err             (retained)
    villa_gw/<did>/cmd/<command>        <- HA → bridge → uart2d

Note: the `availability` topic is published "online" on start and "offline"
on graceful unload. It is NOT a true MQTT Last-Will-Testament — if HA
crashes or the host dies, the broker will keep the retained "online" value.
We could only set a true LWT by hooking into HA-Mosquitto's CONNECT packet,
which is not exposed in the integration API.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from .const import (
    MQTT_CLOUD_ONLY_TYPES,
    MQTT_COMMANDS,
    MQTT_EVENT_SLUGS,
    topic_availability,
    topic_cloud,
    topic_cmd,
    topic_event,
    topic_state,
    topic_system,
)

_LOGGER = logging.getLogger(__name__)


class VillaGwMqttBridge:
    """Glue between the coordinator's parsed events and the HA-Mosquitto broker.

    Lifecycle:
      - `async_start(coordinator)`: subscribe to cmd topics + publish online
      - `publish_event(event)`: called from coordinator on each parsed log event
      - `publish_state(app)`: called from coordinator on AT+B APPLICATION poll
      - `publish_system(sys)`: called from coordinator on periodic refresh
      - `async_stop()`: publish offline, unsubscribe
    """

    def __init__(
        self,
        hass: HomeAssistant,
        base_topic: str,
        device_id: str,
        client,  # VillaGwClient (forward-declared to avoid cycle)
        outdoor_address: int,
        live_view_duration: int,
        publish_discovery: bool = True,
    ) -> None:
        self.hass = hass
        self.base = base_topic
        self.did = device_id.lower().replace(":", "")
        self.client = client
        self.outdoor = outdoor_address
        self.duration = live_view_duration
        self.publish_discovery = publish_discovery
        self._unsubs: list = []
        self._started = False

    # ─────────────────────────────────────────────── HA-MQTT-Discovery

    def _device_block(self) -> dict:
        """Device descriptor shared by all discovery configs."""
        return {
            "identifiers": [f"villa_gw_{self.did}"],
            "name": "Villa GW",
            "manufacturer": "HHG / EGB",
            "model": "AVL20P",
            "configuration_url": f"http://{self.client.host}",
        }

    def _discovery_configs(self) -> list[tuple[str, dict]]:
        """Return [(discovery_topic, config_payload)] for every entity.

        Topic layout: homeassistant/<component>/villa_gw_<did>/<object_id>/config
        """
        node = f"villa_gw_{self.did}"
        avail = topic_availability(self.base, self.did)
        evt = lambda slug: topic_event(self.base, self.did, slug)
        state = topic_state(self.base, self.did)
        system = topic_system(self.base, self.did)
        cloud_in = topic_cloud(self.base, self.did, "in")
        cloud_out = topic_cloud(self.base, self.did, "out")
        cloud_connect = topic_cloud(self.base, self.did, "connect")
        cmd = lambda c: topic_cmd(self.base, self.did, c)
        device = self._device_block()
        availability = [{"topic": avail, "payload_available": "online", "payload_not_available": "offline"}]

        # Binary sensors
        configs = [
            ("binary_sensor", "doorbell_ringing", {
                "name": "Doorbell ringing",
                "device_class": "occupancy",
                "icon": "mdi:doorbell",
                "state_topic": evt("doorbell"),
                "value_template": "{{ 'ON' if value_json.type == 'doorbell_ringing' else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
                "off_delay": 10,
            }),
            ("binary_sensor", "call_active", {
                "name": "Call active",
                "device_class": "running",
                "icon": "mdi:phone-in-talk",
                "state_topic": state,
                "value_template": "{{ 'ON' if value_json.call is defined and (value_json.call | sum) > 0 else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
            }),
            ("binary_sensor", "live_view_active", {
                "name": "Live view active",
                "device_class": "running",
                "icon": "mdi:video",
                "state_topic": evt("live_view_started"),
                "value_template": "ON",
                "payload_on": "ON", "payload_off": "OFF",
                "off_delay": 65,  # default monitor duration + buffer
            }),
            ("binary_sensor", "outdoor_station_ringing", {
                "name": "Outdoor station ringing",
                "device_class": "sound",
                "icon": "mdi:bell-ring",
                "state_topic": evt("ringback"),
                "value_template": "{{ 'ON' if value_json.state == 1 else 'OFF' }}",
                "payload_on": "ON", "payload_off": "OFF",
            }),
            ("binary_sensor", "cloud_online", {
                "name": "Cloud connection",
                "device_class": "connectivity",
                "state_topic": cloud_connect,
                "payload_on": "ok", "payload_off": "fail",
            }),
            # NOTE: no separate `gateway_online` discovery entity. HA's MQTT
            # availability mechanism already marks every entity as
            # 'unavailable' when our `availability` topic publishes 'offline'
            # — adding a binary_sensor that reads the same topic would just
            # show 'unavailable' and never the actual on/off transitions.
            # Status sensors (from state topic)
            ("sensor", "state_id", {
                "name": "State",
                "icon": "mdi:state-machine",
                "state_topic": state,
                "value_template": "{{ value_json.state }}",
            }),
            ("sensor", "sip_status", {
                "name": "SIP",
                "icon": "mdi:phone-check",
                "state_topic": state,
                "value_template": "{{ 'online' if value_json.sip == 1 else 'offline' }}",
            }),
            # System sensors
            ("sensor", "firmware", {
                "name": "Firmware",
                "icon": "mdi:chip",
                "entity_category": "diagnostic",
                "state_topic": system,
                "value_template": "{{ value_json.version }}",
            }),
            ("sensor", "uptime", {
                "name": "Uptime",
                "icon": "mdi:timer-sand",
                "device_class": "duration",
                "state_class": "measurement",
                "unit_of_measurement": "s",
                "entity_category": "diagnostic",
                "state_topic": system,
                "value_template": "{{ value_json.uptime }}",
            }),
            ("sensor", "memory_used", {
                "name": "Memory used",
                "icon": "mdi:memory",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "entity_category": "diagnostic",
                "state_topic": system,
                "value_template": (
                    "{% if value_json.mem is defined and value_json.mem[0] > 0 %}"
                    "{{ (value_json.mem[1] / value_json.mem[0] * 100) | round(1) }}"
                    "{% else %}0{% endif %}"
                ),
            }),
            # Buttons → publish to cmd topics
            ("button", "wake", {
                "name": "Live view",
                "icon": "mdi:cctv",
                "command_topic": cmd("wake"),
                "payload_press": "{}",
            }),
            ("button", "stop_live", {
                "name": "Stop live view",
                "icon": "mdi:cctv-off",
                "command_topic": cmd("stop_live"),
                "payload_press": "{}",
            }),
            ("button", "hook", {
                "name": "Accept call",
                "icon": "mdi:phone",
                "command_topic": cmd("hook"),
                "payload_press": "{}",
            }),
            ("button", "hangup", {
                "name": "Hang up",
                "icon": "mdi:phone-hangup",
                "command_topic": cmd("hangup"),
                "payload_press": "{}",
            }),
            ("button", "switch_camera", {
                "name": "Switch camera",
                "icon": "mdi:camera-switch",
                "command_topic": cmd("switch_camera"),
                "payload_press": "{}",
                "entity_category": "config",
            }),
            ("button", "snapshot", {
                "name": "Snapshot",
                "icon": "mdi:camera",
                "command_topic": cmd("snapshot"),
                "payload_press": "{}",
            }),
            # Lock (momentary unlock)
            # Momentary unlock — optimistic, no state_topic.
            # HA's MQTT lock with `optimistic: true` ignores any state_topic
            # and toggles client-side, so setting one would just clutter the
            # discovery config without effect.
            ("lock", "door", {
                "name": "Door",
                "icon": "mdi:door",
                "command_topic": cmd("door"),
                "payload_lock": "lock",
                "payload_unlock": "unlock",
                "state_locked": "LOCKED",
                "state_unlocked": "UNLOCKED",
                "optimistic": True,
            }),
        ]

        result = []
        for component, object_id, payload in configs:
            payload = {
                **payload,
                "unique_id": f"{node}_{object_id}",
                "object_id": f"{node}_{object_id}",
                "availability": availability,
                "device": device,
            }
            discovery_topic = f"homeassistant/{component}/{node}/{object_id}/config"
            result.append((discovery_topic, payload))
        return result

    async def _publish_discovery(self) -> None:
        """Publish all discovery configs (retained, qos=1)."""
        configs = self._discovery_configs()
        for topic, payload in configs:
            await self._publish(topic, payload, retain=True, qos=1)
        _LOGGER.info("MQTT-discovery: %d entities published", len(configs))

    async def _unpublish_discovery(self) -> None:
        """Send empty retained messages to remove any discovery entries we
        may have published in a previous run. Always safe to call — HA's
        MQTT-Discovery treats empty retained payload as 'forget this entity'."""
        configs = self._discovery_configs()
        for topic, _ in configs:
            await self._publish(topic, "", retain=True, qos=1)
        _LOGGER.debug("MQTT-discovery: %d entries cleared", len(configs))

    # ─────────────────────────────────────────────── lifecycle

    async def async_start(self) -> None:
        """Subscribe to cmd topics + announce availability.

        Times out cleanly if the HA MQTT integration isn't ready within 10s —
        we don't want to block ConfigEntry setup forever when Mosquitto is
        down or not installed.
        """
        if self._started:
            return
        try:
            await asyncio.wait_for(
                mqtt.async_wait_for_mqtt_client(self.hass), timeout=10
            )
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "MQTT-bridge: HA MQTT integration not ready within 10s — bridge disabled"
            )
            return
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "MQTT-bridge: HA MQTT integration error (%s) — bridge disabled", err
            )
            return

        # Subscribe to all cmd topics with a single wildcard sub
        cmd_topic = topic_cmd(self.base, self.did, "+")
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, cmd_topic, self._handle_cmd, qos=1)
        )

        # Publish online (retained)
        await self._publish(
            topic_availability(self.base, self.did), "online", retain=True, qos=1
        )
        self._started = True

        # Discovery management:
        # - if enabled: publish discovery configs (retained) → HA auto-creates
        # - if disabled: unpublish ANY previously retained discovery configs
        #   from earlier runs, so toggling discovery off cleans up stale entities
        if self.publish_discovery:
            await self._publish_discovery()
        else:
            await self._unpublish_discovery()

        _LOGGER.info(
            "MQTT-bridge active under %s/%s/+ (discovery=%s)",
            self.base, self.did, "on" if self.publish_discovery else "off",
        )

    async def async_stop(self) -> None:
        if not self._started:
            return
        # LWT will set offline if we crash, but explicit offline is cleaner on shutdown
        await self._publish(
            topic_availability(self.base, self.did), "offline", retain=True, qos=1
        )
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()
        self._started = False

    # ─────────────────────────────────────────────── publish helpers

    async def _publish(
        self, topic: str, payload: Any, *, retain: bool = False, qos: int = 0
    ) -> None:
        if not self._started and topic != topic_availability(self.base, self.did):
            return
        if isinstance(payload, (dict, list)):
            # default=str: defensive against future payloads with datetime/bytes
            # sub-values. Lossy (datetimes become repr-strings) but the bridge
            # is a best-effort mirror, not the source of truth — better than
            # silently dropping the event on a TypeError.
            payload = json.dumps(payload, separators=(",", ":"), default=str)
        elif not isinstance(payload, (str, bytes)):
            payload = str(payload)
        try:
            await mqtt.async_publish(
                self.hass, topic, payload, qos=qos, retain=retain
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MQTT publish %s failed: %s", topic, err)

    async def publish_event(self, event: dict) -> None:
        """One parsed log-event → one MQTT publish on `event/<slug>`.

        Cloud-* events skip the event/* tree (they're already mirrored under
        cloud/in, cloud/out, cloud/connect below) — keeps the topic tree tidy
        and avoids double-publishing the same payload twice.
        """
        etype = event.get("type", "")
        if etype not in MQTT_CLOUD_ONLY_TYPES:
            slug = MQTT_EVENT_SLUGS.get(etype)
            if not slug:
                # Unmapped event → publish to event/unknown with full payload
                slug = "unknown"
                event = {**event, "_original_type": etype}
            await self._publish(
                topic_event(self.base, self.did, slug), event, retain=False, qos=0
            )

        # Cloud-MQTT-mirror events go to a separate subtree
        if etype == "cloud_mqtt_in":
            await self._publish(
                topic_cloud(self.base, self.did, "in"),
                event.get("payload") or event.get("payload_raw", ""),
                retain=False,
            )
        elif etype == "cloud_mqtt_out":
            await self._publish(
                topic_cloud(self.base, self.did, "out"),
                event.get("payload") or event.get("payload_raw", ""),
                retain=False,
            )
        elif etype == "cloud_connect":
            await self._publish(
                topic_cloud(self.base, self.did, "connect"),
                event.get("status", "?"),
                retain=True,
            )

    async def publish_state(self, application: dict) -> None:
        """Periodic state mirror (retained, JSON)."""
        await self._publish(
            topic_state(self.base, self.did), application, retain=True, qos=0
        )

    async def publish_availability(self, online: bool) -> None:
        """Update the availability topic from the coordinator's gateway_online flag."""
        await self._publish(
            topic_availability(self.base, self.did),
            "online" if online else "offline",
            retain=True, qos=1,
        )

    async def publish_system(self, system: dict) -> None:
        """Slow-changing sensor data (retained, JSON)."""
        await self._publish(
            topic_system(self.base, self.did), system, retain=True, qos=0
        )

    # ─────────────────────────────────────────────── cmd handler

    @staticmethod
    def _payload_to_str(payload: Any) -> str:
        """Normalize msg.payload (bytes-or-str depending on HA version)."""
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
        return ""

    async def _handle_cmd(self, msg) -> None:
        """Incoming cmd topic → uart2d action."""
        topic = msg.topic
        cmd = topic.rsplit("/", 1)[-1]
        if cmd not in MQTT_COMMANDS:
            _LOGGER.debug("MQTT-cmd unknown: %s", cmd)
            return
        raw_payload = self._payload_to_str(msg.payload)
        try:
            payload = json.loads(raw_payload) if raw_payload else {}
        except (ValueError, TypeError):
            payload = {"raw": raw_payload}

        def _int(key: str, default: int) -> int:
            """Coerce payload field to int, falling back on garbage input."""
            try:
                return int(payload.get(key, default))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "MQTT cmd %s: invalid %s=%r → using default %d",
                    cmd, key, payload.get(key), default,
                )
                return default

        try:
            if cmd == "wake":
                dur = _int("duration", self.duration)
                addr = _int("address", self.outdoor)
                await self.client.wake_live_view(address=addr, duration=dur)
            elif cmd == "stop_live":
                addr = _int("address", self.outdoor)
                await self.client.stop_live_view(address=addr)
            elif cmd in ("door", "unlock"):
                await self.client.unlock_door()
            elif cmd == "hook":
                await self.client.hook_call()
            elif cmd == "hangup":
                await self.client.hang_call()
            elif cmd == "switch_camera":
                await self.client.switch_camera()
            elif cmd == "snapshot":
                # MJPG-snap goes via avlink, not uart2d
                await self.client.send_avlink("AT+B MJPG Snap")
            elif cmd == "at_raw":
                raw = payload.get("command") or raw_payload
                # Same validation as the HA send_at_command service —
                # MQTT-broker write access shouldn't bypass it.
                if not raw or not raw.startswith("AT+B"):
                    _LOGGER.warning(
                        "MQTT cmd/at_raw rejected — must start with 'AT+B': %r", raw
                    )
                elif len(raw) > 200:
                    _LOGGER.warning(
                        "MQTT cmd/at_raw rejected — too long (%d): %r", len(raw), raw[:80]
                    )
                elif any(ch in raw for ch in ("\r", "\n", "\x00", ";")):
                    _LOGGER.warning(
                        "MQTT cmd/at_raw rejected — illegal char (CR/LF/NUL/;): %r", raw
                    )
                else:
                    # Send to uart2d (bus-side) — for power users only
                    await self.client.send_uart2d(raw)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("MQTT cmd %s failed: %s", cmd, err)
