"""Lock entity for the door relay.

`AT+B UART unlock 1` triggers the bus-side relay momentarily. The relay
auto-closes after `relay.duration_1` seconds (default 3 s) — so this entity
behaves as a momentary unlock: we report unlocked for the configured duration,
then go back to locked.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import VillaGwCoordinator, get_coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = get_coordinator(hass, entry)
    async_add_entities([VillaGwDoorLock(coordinator, entry)])


class VillaGwDoorLock(LockEntity):
    """Momentary unlock via bus."""

    _attr_has_entity_name = True
    _attr_name = "Türöffner"
    _attr_icon = "mdi:door"

    def __init__(self, coordinator: VillaGwCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.unique_id}_door_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        )
        self._attr_is_locked = True

    async def async_unlock(self, **kwargs: Any) -> None:
        """Trigger the door relay."""
        await self.coordinator.client.unlock_door()
        self._attr_is_locked = False
        self.async_write_ha_state()

        # Re-lock after the relay's configured hold time
        # (parameter.elock_holdtime, default 3s — we read from /api/parameter
        # but a hard-coded 5s is safer if config not yet loaded)
        hold = 5
        try:
            data = self.coordinator.data or {}
            params = data.get("parameter") or {}
            hold = int(params.get("elock_holdtime", 3)) + 2
        except Exception:  # noqa: BLE001
            pass

        async def _relock() -> None:
            await asyncio.sleep(hold)
            self._attr_is_locked = True
            self.async_write_ha_state()

        self.hass.async_create_task(_relock())

    async def async_lock(self, **kwargs: Any) -> None:
        """No-op (relay is auto-closing)."""
        self._attr_is_locked = True
        self.async_write_ha_state()
