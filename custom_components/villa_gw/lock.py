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
    lock = VillaGwDoorLock(coordinator, entry)
    # Ensure the relock task is cancelled on entry-unload (reload), not only
    # when the entity is removed — otherwise a reload mid-unlock leaves the
    # task writing state to an entity that no longer belongs to the new entry.
    entry.async_on_unload(lock._cancel_relock_task)
    async_add_entities([lock])


# Default re-lock delay (s). The relay's actual hold time is configured on the
# device (parameter.elock_holdtime, default 3 s); we add a small safety margin
# so the HA-side state flips after the relay has physically closed again.
_RELOCK_DELAY_S = 5


class VillaGwDoorLock(LockEntity):
    """Momentary unlock via bus."""

    _attr_has_entity_name = True
    _attr_translation_key = "door"
    _attr_icon = "mdi:door"

    def __init__(self, coordinator: VillaGwCoordinator, entry: ConfigEntry) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.unique_id}_door_lock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        )
        self._attr_is_locked = True
        self._relock_task: asyncio.Task | None = None

    def _cancel_relock_task(self) -> None:
        """Cancel a pending re-lock without awaiting it.

        Sync so it can be registered with `entry.async_on_unload`. The task
        itself handles CancelledError silently — no state writes on a
        removed entity.
        """
        if self._relock_task and not self._relock_task.done():
            self._relock_task.cancel()
        self._relock_task = None

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending re-lock task when the entity is removed."""
        self._cancel_relock_task()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Trigger the door relay."""
        await self.coordinator.client.unlock_door()
        self._attr_is_locked = False
        self.async_write_ha_state()

        self._cancel_relock_task()

        async def _relock() -> None:
            try:
                await asyncio.sleep(_RELOCK_DELAY_S)
            except asyncio.CancelledError:
                return
            self._attr_is_locked = True
            self.async_write_ha_state()

        self._relock_task = self.hass.async_create_task(_relock())

    async def async_lock(self, **kwargs: Any) -> None:
        """No-op (relay is auto-closing)."""
        self._attr_is_locked = True
        self.async_write_ha_state()
