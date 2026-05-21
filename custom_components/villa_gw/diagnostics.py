"""Diagnostics support for Villa GW.

Dumps the current coordinator state plus a redacted config entry so users can
attach useful context to an issue without leaking credentials, MAC, or
phone-number-like SIP IDs.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_HOST,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    DOMAIN,
)
from .coordinator import VillaGwCoordinator

# Fields removed from the dump regardless of where they appear.
# CONF_HOST stays — IPs help debug NAT/routing issues — but credentials and
# anything resembling caller identity are stripped.
_REDACT_KEYS = {
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    "mac",
    "sip",
    "from",
    "remote_addr",
    "local_addr",
    "last_caller",
    "last_app_user",
    "token",
    "uuid",
    "did",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the given config entry."""
    coordinator: VillaGwCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinator")
    )

    coord_state: dict[str, Any] = {}
    if coordinator:
        coord_state = {
            "gateway_online": coordinator.gateway_online,
            "cloud_online": coordinator.cloud_online,
            "live_view_active": coordinator.live_view_active,
            "doorbell_active": coordinator.doorbell_active,
            "call_active": coordinator.call_active,
            "outdoor_station_ringing": coordinator.outdoor_station_ringing,
            "last_doorbell_at": (
                coordinator.last_doorbell_at.isoformat()
                if coordinator.last_doorbell_at else None
            ),
            "last_unlock_at": (
                coordinator.last_unlock_at.isoformat()
                if coordinator.last_unlock_at else None
            ),
            "doorbell_count_today": coordinator.doorbell_count_today,
            "unlock_count_today": coordinator.unlock_count_today,
            "call_count_today": coordinator.call_count_today,
            "app_state": coordinator.app_state,
            "sys_state": coordinator.sys_state,
            "data": coordinator.data,
            "mqtt_bridge_enabled": coordinator.mqtt_bridge is not None,
        }

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(entry.data, _REDACT_KEYS),
            "options": async_redact_data(entry.options, _REDACT_KEYS),
        },
        "coordinator": async_redact_data(coord_state, _REDACT_KEYS),
    }
