"""Binary sensors — event-driven via coordinator state mirror.

All entities subscribe to the coordinator's CoordinatorEntity update channel,
which is bumped on every parsed log event (sub-100ms via Telnet-tail) and on
every poll-loop iteration (default 1s).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, DOORBELL_PULSE_SECONDS
from .coordinator import VillaGwCoordinator, get_coordinator


@dataclass(frozen=True, kw_only=True)
class VillaGwBinarySensorDescription(BinarySensorEntityDescription):
    """Description with a getter that reads from coordinator state."""

    is_on: Callable[[VillaGwCoordinator], bool]


BINARY_SENSORS: tuple[VillaGwBinarySensorDescription, ...] = (
    VillaGwBinarySensorDescription(
        key="doorbell_ringing",
        translation_key="doorbell_ringing",
        name="Klingelt",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        icon="mdi:doorbell",
        is_on=lambda c: c.doorbell_active,
    ),
    VillaGwBinarySensorDescription(
        key="call_active",
        translation_key="call_active",
        name="Anruf aktiv",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:phone-in-talk",
        is_on=lambda c: c.call_active,
    ),
    VillaGwBinarySensorDescription(
        key="live_view_active",
        translation_key="live_view_active",
        name="Live-Sicht aktiv",
        device_class=BinarySensorDeviceClass.RUNNING,
        icon="mdi:video",
        is_on=lambda c: c.live_view_active,
    ),
    VillaGwBinarySensorDescription(
        key="outdoor_station_ringing",
        translation_key="outdoor_station_ringing",
        name="Türstation klingelt",
        device_class=BinarySensorDeviceClass.SOUND,
        icon="mdi:bell-ring",
        is_on=lambda c: c.outdoor_station_ringing,
    ),
    VillaGwBinarySensorDescription(
        key="cloud_online",
        translation_key="cloud_online",
        name="Cloud-Verbindung",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=None,
        icon="mdi:cloud-check",
        is_on=lambda c: c.cloud_online,
    ),
    # Reflects whether the polling loop is currently reaching the GW.
    # Drops to OFF after sustained failures (capped exp backoff).
    VillaGwBinarySensorDescription(
        key="gateway_online",
        translation_key="gateway_online",
        name="Gateway erreichbar",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        icon="mdi:lan-connect",
        is_on=lambda c: c.gateway_online,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = get_coordinator(hass, entry)
    entities = [VillaGwBinarySensor(coordinator, entry, d) for d in BINARY_SENSORS]
    async_add_entities(entities)

    # Doorbell needs an auto-clear timer (the coordinator only sets it ON;
    # we clear after DOORBELL_PULSE_SECONDS so automations see a pulse).
    async def _auto_clear_doorbell() -> None:
        while True:
            await asyncio.sleep(1)
            now = hass.loop.time()
            if (
                coordinator.doorbell_active
                and coordinator.last_doorbell_at
                and now - coordinator.last_doorbell_at > DOORBELL_PULSE_SECONDS
            ):
                coordinator.doorbell_active = False
                coordinator.async_set_updated_data(coordinator.data or {})

    entry.async_create_background_task(
        hass, _auto_clear_doorbell(), name="villa_gw_doorbell_pulse"
    )


class VillaGwBinarySensor(CoordinatorEntity[VillaGwCoordinator], BinarySensorEntity):
    """Generic binary sensor backed by a coordinator field."""

    _attr_has_entity_name = True
    entity_description: VillaGwBinarySensorDescription

    def __init__(
        self,
        coordinator: VillaGwCoordinator,
        entry: ConfigEntry,
        description: VillaGwBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        )

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on(self.coordinator)
