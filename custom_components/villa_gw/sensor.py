"""Sensor entities — status snapshots and daily counters.

Most sensors read directly from the coordinator (which mirrors AT+B
APPLICATION + AT+B SYSTEM results + parsed log events). Daily counters reset
at midnight in HA's local timezone.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VillaGwCoordinator, get_coordinator


@dataclass(frozen=True, kw_only=True)
class VillaGwSensorDescription(SensorEntityDescription):
    """Sensor description with a value-getter on the coordinator."""

    value: Callable[[VillaGwCoordinator], Any]


# ─────────────────────────────────────────────────────────── extractors

def _uptime(c: VillaGwCoordinator) -> datetime | None:
    sec = (c.sys_state or {}).get("uptime")
    if sec is None:
        return None
    return datetime.now(timezone.utc) - timedelta(seconds=int(sec))


def _mem_pct(c: VillaGwCoordinator) -> float | None:
    mem = (c.sys_state or {}).get("mem")
    if not mem or len(mem) < 2 or not mem[0]:
        return None
    total, used = int(mem[0]), int(mem[1])
    return round(used / total * 100, 1) if total else None


def _firmware(c: VillaGwCoordinator) -> str | None:
    return (c.sys_state or {}).get("version")


def _state_int(c: VillaGwCoordinator) -> int | None:
    return (c.app_state or {}).get("state")


def _sip_status(c: VillaGwCoordinator) -> str:
    sip = (c.app_state or {}).get("sip")
    return "online" if sip == 1 else "offline" if sip == 0 else "unknown"


def _cloud_status(c: VillaGwCoordinator) -> str:
    return "online" if c.cloud_online else "offline"


def _stream_mode(c: VillaGwCoordinator) -> str:
    v = (c.data or {}).get("video") or {}
    transfer = v.get("transfer")
    return {0: "P2P", 1: "RTMP-cloud", 2: "Local"}.get(transfer, "unknown")


def _last_doorbell(c: VillaGwCoordinator) -> datetime | None:
    if c.last_doorbell_at is None:
        return None
    # coordinator stores monotonic loop time; convert to UTC wall clock approximately
    delta_to_now = c.hass.loop.time() - c.last_doorbell_at
    return datetime.now(timezone.utc) - timedelta(seconds=delta_to_now)


def _last_unlock(c: VillaGwCoordinator) -> datetime | None:
    if c.last_unlock_at is None:
        return None
    delta_to_now = c.hass.loop.time() - c.last_unlock_at
    return datetime.now(timezone.utc) - timedelta(seconds=delta_to_now)


# ─────────────────────────────────────────────────────────── descriptors

SENSORS: tuple[VillaGwSensorDescription, ...] = (
    # ── transient status ────────────────────────────────────
    VillaGwSensorDescription(
        key="state",
        translation_key="state",
        name="State",
        icon="mdi:state-machine",
        value=_state_int,
    ),
    VillaGwSensorDescription(
        key="sip_status",
        translation_key="sip_status",
        name="SIP-Status",
        icon="mdi:phone-check",
        value=_sip_status,
    ),
    VillaGwSensorDescription(
        key="cloud_status",
        translation_key="cloud_status",
        name="Cloud-Status",
        icon="mdi:cloud-check",
        value=_cloud_status,
    ),
    VillaGwSensorDescription(
        key="last_doorbell",
        translation_key="last_doorbell",
        name="Letzte Klingel",
        icon="mdi:doorbell",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=_last_doorbell,
    ),
    VillaGwSensorDescription(
        key="last_unlock",
        translation_key="last_unlock",
        name="Letzte Türöffnung",
        icon="mdi:door-open",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=_last_unlock,
    ),
    VillaGwSensorDescription(
        key="last_caller",
        translation_key="last_caller",
        name="Letzter Anrufer",
        icon="mdi:phone-incoming",
        value=lambda c: c.last_caller,
        entity_registry_enabled_default=False,
    ),
    VillaGwSensorDescription(
        key="last_app_user",
        translation_key="last_app_user",
        name="Letzter App-User",
        icon="mdi:account",
        value=lambda c: c.last_app_user,
        entity_registry_enabled_default=False,
    ),
    VillaGwSensorDescription(
        key="stream_mode",
        translation_key="stream_mode",
        name="Stream-Modus",
        icon="mdi:video-input-component",
        value=_stream_mode,
    ),
    # ── system / diagnostic ─────────────────────────────────
    VillaGwSensorDescription(
        key="firmware",
        translation_key="firmware",
        name="Firmware",
        icon="mdi:chip",
        entity_registry_enabled_default=False,
        value=_firmware,
    ),
    VillaGwSensorDescription(
        key="uptime",
        translation_key="uptime",
        name="Uptime",
        icon="mdi:timer-sand",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value=_uptime,
    ),
    VillaGwSensorDescription(
        key="memory_used",
        translation_key="memory_used",
        name="Speicher belegt",
        icon="mdi:memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value=_mem_pct,
    ),
    # ── daily counters (reset at midnight) ──────────────────
    VillaGwSensorDescription(
        key="doorbell_count_today",
        translation_key="doorbell_count_today",
        name="Klingeln heute",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda c: c.doorbell_count_today,
    ),
    VillaGwSensorDescription(
        key="unlock_count_today",
        translation_key="unlock_count_today",
        name="Türöffnungen heute",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda c: c.unlock_count_today,
    ),
    VillaGwSensorDescription(
        key="call_count_today",
        translation_key="call_count_today",
        name="Anrufe heute",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value=lambda c: c.call_count_today,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = get_coordinator(hass, entry)
    async_add_entities(VillaGwSensor(coordinator, entry, d) for d in SENSORS)

    # Reset daily counters at midnight (HA local timezone)
    @callback
    def _reset_counters(now: datetime) -> None:
        coordinator.doorbell_count_today = 0
        coordinator.unlock_count_today = 0
        coordinator.call_count_today = 0
        coordinator.async_set_updated_data(coordinator.data or {})

    entry.async_on_unload(
        async_track_time_change(hass, _reset_counters, hour=0, minute=0, second=0)
    )


class VillaGwSensor(CoordinatorEntity[VillaGwCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: VillaGwSensorDescription

    def __init__(
        self,
        coordinator: VillaGwCoordinator,
        entry: ConfigEntry,
        description: VillaGwSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator)
