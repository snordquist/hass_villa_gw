"""Device triggers for Villa GW.

Exposes our HA bus events as first-class device triggers so they show up in
the Automation editor without the user needing to remember our event names.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    EVENT_CALL_ENDED,
    EVENT_CALL_INCOMING,
    EVENT_DOOR_UNLOCKED,
    EVENT_DOORBELL_RINGING,
    EVENT_LIVE_VIEW_ENDED,
    EVENT_LIVE_VIEW_STARTED,
    EVENT_RINGBACK,
)

# Trigger type → HA bus event name. Trigger types are short identifiers
# that show up in the automation UI; the values are the actual events
# fired by the coordinator.
TRIGGER_TYPES: dict[str, str] = {
    "doorbell_ringing": EVENT_DOORBELL_RINGING,
    "call_incoming": EVENT_CALL_INCOMING,
    "call_ended": EVENT_CALL_ENDED,
    "ringback": EVENT_RINGBACK,
    "live_view_started": EVENT_LIVE_VIEW_STARTED,
    "live_view_ended": EVENT_LIVE_VIEW_ENDED,
    "door_unlocked": EVENT_DOOR_UNLOCKED,
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(list(TRIGGER_TYPES)),
    }
)


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List device triggers available for the given Villa GW device."""
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger by forwarding it to the event-trigger platform.

    We don't filter by device_id in the event data — the events are
    integration-scoped and there is realistically only one Villa GW per
    install. If multi-device support lands later, plumb entry.unique_id
    through the event payload and add a match here.
    """
    event_type = TRIGGER_TYPES[config[CONF_TYPE]]
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: event_type,
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )
