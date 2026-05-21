"""Villa GW integration setup."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VillaGwAuthError, VillaGwClient, VillaGwConnectionError
from .const import (
    CONF_HOST,
    CONF_LIVE_VIEW_DURATION,
    CONF_OUTDOOR_ADDRESS,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    DEFAULT_LIVE_VIEW_DURATION,
    DEFAULT_OUTDOOR_ADDRESS,
    DOMAIN,
)
from .coordinator import VillaGwCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.LOCK,
    Platform.SENSOR,
]

# ─────────────────────────────────────────────── Service schemas

SERVICE_WAKE = "wake"
SERVICE_STOP_LIVE = "stop_live"
SERVICE_UNLOCK_DOOR = "unlock_door"
SERVICE_HANGUP = "hangup"
SERVICE_ACCEPT_CALL = "accept_call"
SERVICE_SWITCH_CAMERA = "switch_camera"
SERVICE_SNAPSHOT = "snapshot"
SERVICE_SEND_AT = "send_at_command"

SCHEMA_WAKE = vol.Schema(
    {
        vol.Optional("duration"): vol.All(int, vol.Range(min=5, max=120)),
        vol.Optional("address"): vol.All(int, vol.Range(min=0, max=255)),
    }
)

SCHEMA_STOP_LIVE = vol.Schema(
    {vol.Optional("address"): vol.All(int, vol.Range(min=0, max=255))}
)

SCHEMA_EMPTY = vol.Schema({})

SCHEMA_AT = vol.Schema(
    {
        vol.Required("command"): cv.string,
        vol.Optional("target", default="uart2d"): vol.In(("uart2d", "avlink")),
        vol.Optional("expect_response", default=False): cv.boolean,
    }
)


# ─────────────────────────────────────────────── setup / unload

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Villa GW from a config entry."""
    session = async_get_clientsession(hass)
    client = VillaGwClient(
        host=entry.data[CONF_HOST],
        web_username=entry.data[CONF_WEB_USERNAME],
        web_password=entry.data[CONF_WEB_PASSWORD],
        session=session,
    )

    try:
        await client.login()
    except VillaGwAuthError as err:
        _LOGGER.error("Web admin auth failed: %s", err)
        return False
    except VillaGwConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot reach Villa GW: {err}") from err

    coordinator = VillaGwCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_tasks()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "client": client,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Register services once (re-registration is fine — HA dedupes by domain+name)
    await _async_register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator: VillaGwCoordinator = data["coordinator"]
        await coordinator.async_stop_tasks()

    # If no more entries, unregister services
    if not hass.data.get(DOMAIN):
        for name in (SERVICE_WAKE, SERVICE_STOP_LIVE, SERVICE_UNLOCK_DOOR,
                     SERVICE_HANGUP, SERVICE_ACCEPT_CALL, SERVICE_SWITCH_CAMERA,
                     SERVICE_SNAPSHOT, SERVICE_SEND_AT):
            hass.services.async_remove(DOMAIN, name)
    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options change."""
    await hass.config_entries.async_reload(entry.entry_id)


# ─────────────────────────────────────────────── service registration

async def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration-wide services. Idempotent."""

    def _first_client() -> VillaGwClient | None:
        for entry_data in (hass.data.get(DOMAIN) or {}).values():
            return entry_data.get("client")
        return None

    def _first_entry_opts() -> dict[str, Any]:
        for entry_id, entry_data in (hass.data.get(DOMAIN) or {}).items():
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry:
                return {**entry.data, **entry.options}
        return {}

    async def svc_wake(call: ServiceCall) -> None:
        client = _first_client()
        if not client: return
        opts = _first_entry_opts()
        duration = call.data.get("duration", opts.get(CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION))
        address = call.data.get("address", opts.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS))
        await client.wake_live_view(address=address, duration=duration)

    async def svc_stop_live(call: ServiceCall) -> None:
        client = _first_client()
        if not client: return
        opts = _first_entry_opts()
        address = call.data.get("address", opts.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS))
        await client.stop_live_view(address=address)

    async def svc_unlock(call: ServiceCall) -> None:
        client = _first_client()
        if client: await client.unlock_door()

    async def svc_hangup(call: ServiceCall) -> None:
        client = _first_client()
        if client: await client.hang_call()

    async def svc_accept(call: ServiceCall) -> None:
        client = _first_client()
        if client: await client.hook_call()

    async def svc_switch(call: ServiceCall) -> None:
        client = _first_client()
        if client: await client.switch_camera()

    async def svc_snapshot(call: ServiceCall) -> None:
        client = _first_client()
        if client:
            # MJPG snap is an avlink command (mimedia executes it)
            await client._avlink_query("AT+B MJPG Snap")  # noqa: SLF001

    async def svc_send_at(call: ServiceCall) -> dict[str, Any] | None:
        client = _first_client()
        if not client:
            return None
        cmd = call.data["command"]
        target = call.data.get("target", "uart2d")
        expect = call.data.get("expect_response", False)

        # Hardening — reject obvious abuse / shell-injection-style payloads
        if not cmd.startswith("AT+B"):
            _LOGGER.warning("send_at_command rejected — must start with 'AT+B': %r", cmd)
            return {"error": "command must start with 'AT+B'"}
        if len(cmd) > 200:
            _LOGGER.warning("send_at_command rejected — too long (%d): %r", len(cmd), cmd[:80])
            return {"error": "command exceeds 200 chars"}
        if any(ch in cmd for ch in ("\r", "\n", "\x00", ";")):
            _LOGGER.warning("send_at_command rejected — illegal char (CR/LF/NUL/;): %r", cmd)
            return {"error": "command contains CR/LF/NUL/;"}

        if target == "avlink":
            response = await client._avlink_query(cmd)  # noqa: SLF001
            return {"response": response}
        # uart2d (default) — fire-and-forget, but optionally collect short response
        await client._uart2d_send(cmd)  # noqa: SLF001
        return {"response": ""} if expect else None

    hass.services.async_register(DOMAIN, SERVICE_WAKE, svc_wake, schema=SCHEMA_WAKE)
    hass.services.async_register(DOMAIN, SERVICE_STOP_LIVE, svc_stop_live, schema=SCHEMA_STOP_LIVE)
    hass.services.async_register(DOMAIN, SERVICE_UNLOCK_DOOR, svc_unlock, schema=SCHEMA_EMPTY)
    hass.services.async_register(DOMAIN, SERVICE_HANGUP, svc_hangup, schema=SCHEMA_EMPTY)
    hass.services.async_register(DOMAIN, SERVICE_ACCEPT_CALL, svc_accept, schema=SCHEMA_EMPTY)
    hass.services.async_register(DOMAIN, SERVICE_SWITCH_CAMERA, svc_switch, schema=SCHEMA_EMPTY)
    hass.services.async_register(DOMAIN, SERVICE_SNAPSHOT, svc_snapshot, schema=SCHEMA_EMPTY)
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_AT, svc_send_at, schema=SCHEMA_AT, supports_response=True
    )
