"""Optional MQTT-bridge to Home Assistant's Mosquitto broker.

When enabled, the coordinator forwards every parsed log event to MQTT topics
and subscribes to `<base>/<did>/cmd/*` for incoming commands. This lets
NodeRED, external dashboards or other automations consume the GW's events
without writing a Home Assistant custom component.

Topic layout (defaults to base="villa_gw", did=lowercased MAC):

    villa_gw/<did>/availability         online | offline       (LWT, retained)
    villa_gw/<did>/state                {state,sip,call}        (retained, JSON)
    villa_gw/<did>/system               {uptime, mem, fw, …}    (retained, JSON)
    villa_gw/<did>/event/<slug>         <json payload>          (NOT retained)
    villa_gw/<did>/cloud/in             raw cloud-MQTT messages (NOT retained)
    villa_gw/<did>/cloud/out            …
    villa_gw/<did>/cloud/connect        ok|fail|err             (retained)
    villa_gw/<did>/cmd/<command>        <- HA → bridge → uart2d
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from .const import (
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
            ("binary_sensor", "gateway_online", {
                "name": "Gateway reachable",
                "device_class": "connectivity",
                "icon": "mdi:lan-connect",
                # Mirrors the `availability` topic directly — when the bridge
                # publishes "offline" via LWT, this entity flips.
                "state_topic": avail,
                "payload_on": "online", "payload_off": "offline",
            }),
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
                "unit_of_measurement": "s",
                "entity_category": "diagnostic",
                "state_topic": system,
                "value_template": "{{ value_json.uptime }}",
            }),
            ("sensor", "memory_used", {
                "name": "Memory used",
                "icon": "mdi:memory",
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
            ("lock", "door", {
                "name": "Door",
                "icon": "mdi:door",
                "command_topic": cmd("door"),
                "payload_lock": "lock",
                "payload_unlock": "unlock",
                "state_topic": evt("door_unlocked"),
                "value_template": "UNLOCKED",
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
        if not self.publish_discovery:
            return
        for topic, payload in self._discovery_configs():
            await self._publish(topic, payload, retain=True, qos=1)
        _LOGGER.info("MQTT-discovery: %d entities published", len(self._discovery_configs()))

    async def _unpublish_discovery(self) -> None:
        """Send empty retained messages to remove discovery entries."""
        if not self.publish_discovery:
            return
        for topic, _ in self._discovery_configs():
            await self._publish(topic, "", retain=True, qos=1)

    # ─────────────────────────────────────────────── lifecycle

    async def async_start(self) -> None:
        """Subscribe to cmd topics + announce availability."""
        if self._started:
            return
        try:
            await mqtt.async_wait_for_mqtt_client(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "MQTT-bridge: HA MQTT integration not ready (%s) — bridge disabled", err
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

        # Publish discovery configs so HA auto-creates entities (retained)
        await self._publish_discovery()

        _LOGGER.info("MQTT-bridge active under %s/%s/+", self.base, self.did)

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
            payload = json.dumps(payload, separators=(",", ":"))
        elif not isinstance(payload, (str, bytes)):
            payload = str(payload)
        try:
            await mqtt.async_publish(
                self.hass, topic, payload, qos=qos, retain=retain
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("MQTT publish %s failed: %s", topic, err)

    async def publish_event(self, event: dict) -> None:
        """One parsed log-event → one MQTT publish on `event/<slug>`."""
        etype = event.get("type", "")
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

    async def _handle_cmd(self, msg) -> None:
        """Incoming cmd topic → uart2d action."""
        topic = msg.topic
        cmd = topic.rsplit("/", 1)[-1]
        if cmd not in MQTT_COMMANDS:
            _LOGGER.debug("MQTT-cmd unknown: %s", cmd)
            return
        try:
            payload = json.loads(msg.payload) if msg.payload else {}
        except (ValueError, TypeError):
            payload = {"raw": msg.payload}

        try:
            if cmd == "wake":
                dur = int(payload.get("duration", self.duration))
                addr = int(payload.get("address", self.outdoor))
                await self.client.wake_live_view(address=addr, duration=dur)
            elif cmd == "stop_live":
                addr = int(payload.get("address", self.outdoor))
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
                await self.client._avlink_query("AT+B MJPG Snap")  # noqa: SLF001
            elif cmd == "at_raw":
                raw = payload.get("command") or (
                    msg.payload if isinstance(msg.payload, str) else ""
                )
                if raw and raw.startswith("AT+B"):
                    # Send to uart2d (bus-side) — for power users only
                    await self.client._uart2d_send(raw)  # noqa: SLF001
                else:
                    _LOGGER.warning("MQTT cmd/at_raw rejected: payload=%r", raw)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("MQTT cmd %s failed: %s", cmd, err)
