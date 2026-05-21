"""Button entities — Wake live-view, Hangup, Call outdoor, Switch camera."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import VillaGwClient
from .const import (
    CONF_LIVE_VIEW_DURATION,
    CONF_OUTDOOR_ADDRESS,
    DEFAULT_LIVE_VIEW_DURATION,
    DEFAULT_OUTDOOR_ADDRESS,
    DOMAIN,
)
from .coordinator import VillaGwCoordinator, get_coordinator


@dataclass(frozen=True, kw_only=True)
class VillaGwButtonDescription(ButtonEntityDescription):
    """Description with bound async action."""

    action: Callable[[VillaGwClient, dict[str, Any]], Awaitable[None]]


def _wake(opts: dict[str, Any]) -> Callable[[VillaGwClient, dict[str, Any]], Awaitable[None]]:
    async def _do(c: VillaGwClient, _o: dict[str, Any]) -> None:
        await c.wake_live_view(
            address=opts.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS),
            duration=opts.get(CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION),
        )
    return _do


BUTTONS: tuple[VillaGwButtonDescription, ...] = (
    VillaGwButtonDescription(
        key="wake",
        translation_key="wake",
        icon="mdi:cctv",
        action=lambda c, o: c.wake_live_view(
            address=o.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS),
            duration=o.get(CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION),
        ),
    ),
    VillaGwButtonDescription(
        key="stop_live",
        translation_key="stop_live",
        icon="mdi:cctv-off",
        action=lambda c, o: c.stop_live_view(
            address=o.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS),
        ),
    ),
    VillaGwButtonDescription(
        key="hook",
        translation_key="hook",
        icon="mdi:phone",
        action=lambda c, _o: c.hook_call(),
    ),
    VillaGwButtonDescription(
        key="hang",
        translation_key="hang",
        icon="mdi:phone-hangup",
        action=lambda c, _o: c.hang_call(),
    ),
    VillaGwButtonDescription(
        key="call_outdoor",
        translation_key="call_outdoor",
        icon="mdi:phone-outgoing",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,  # caution: rings the outdoor station!
        action=lambda c, o: c.call_outdoor(
            address=o.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS),
        ),
    ),
    VillaGwButtonDescription(
        key="switch_camera",
        translation_key="switch_camera",
        icon="mdi:camera-switch",
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,  # only useful with multiple outdoor stations
        action=lambda c, _o: c.switch_camera(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = get_coordinator(hass, entry)
    async_add_entities(
        VillaGwButton(coordinator, entry, desc) for desc in BUTTONS
    )


class VillaGwButton(ButtonEntity):
    """Press-only action button against the bus."""

    _attr_has_entity_name = True
    entity_description: VillaGwButtonDescription

    def __init__(
        self,
        coordinator: VillaGwCoordinator,
        entry: ConfigEntry,
        description: VillaGwButtonDescription,
    ) -> None:
        self.entity_description = description
        self.coordinator = coordinator
        self.entry = entry
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        )

    async def async_press(self) -> None:
        opts = {**self.entry.data, **self.entry.options}
        await self.entity_description.action(self.coordinator.client, opts)
