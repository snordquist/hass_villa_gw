"""Config flow for Villa GW integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow, ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import VillaGwAuthError, VillaGwClient, VillaGwConnectionError
from .const import (
    CONF_ENABLE_LOG_TAIL,
    CONF_ENABLE_MQTT_BRIDGE,
    CONF_LIVE_VIEW_DURATION,
    CONF_MQTT_BASE_TOPIC,
    CONF_OUTDOOR_ADDRESS,
    CONF_POLL_INTERVAL_MS,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    DEFAULT_ENABLE_LOG_TAIL,
    DEFAULT_ENABLE_MQTT_BRIDGE,
    DEFAULT_LIVE_VIEW_DURATION,
    DEFAULT_MQTT_BASE_TOPIC,
    DEFAULT_OUTDOOR_ADDRESS,
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_WEB_PASSWORD,
    DEFAULT_WEB_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_WEB_USERNAME, default=DEFAULT_WEB_USERNAME): str,
        vol.Required(CONF_WEB_PASSWORD, default=DEFAULT_WEB_PASSWORD): str,
        vol.Required(CONF_OUTDOOR_ADDRESS, default=DEFAULT_OUTDOOR_ADDRESS): vol.All(
            int, vol.Range(min=0, max=255)
        ),
        vol.Required(CONF_LIVE_VIEW_DURATION, default=DEFAULT_LIVE_VIEW_DURATION): vol.All(
            int, vol.Range(min=5, max=120)
        ),
        vol.Required(CONF_POLL_INTERVAL_MS, default=DEFAULT_POLL_INTERVAL_MS): vol.All(
            int, vol.Range(min=250, max=10000)
        ),
        vol.Required(CONF_ENABLE_LOG_TAIL, default=DEFAULT_ENABLE_LOG_TAIL): bool,
        vol.Required(CONF_ENABLE_MQTT_BRIDGE, default=DEFAULT_ENABLE_MQTT_BRIDGE): bool,
        vol.Required(CONF_MQTT_BASE_TOPIC, default=DEFAULT_MQTT_BASE_TOPIC): str,
    }
)


class VillaGwConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing setup flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = VillaGwClient(
                host=user_input[CONF_HOST],
                web_username=user_input[CONF_WEB_USERNAME],
                web_password=user_input[CONF_WEB_PASSWORD],
                session=session,
            )
            try:
                await client.login()
                mac = await client.mac()
            except VillaGwAuthError:
                errors["base"] = "invalid_auth"
            except VillaGwConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Villa GW setup")
                errors["base"] = "unknown"
            else:
                # Use MAC as unique_id so re-adding the same GW doesn't duplicate
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                return self.async_create_entry(
                    title=f"Villa GW ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VillaGwOptionsFlow(config_entry)


class VillaGwOptionsFlow(OptionsFlow):
    """Allow changing duration / address without re-entering credentials.

    Note: HA 2024.x made ``OptionsFlow.config_entry`` a read-only property.
    Storing it as ``self._entry`` avoids the AttributeError on init.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = {**self._entry.data, **self._entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OUTDOOR_ADDRESS,
                        default=current.get(CONF_OUTDOOR_ADDRESS, DEFAULT_OUTDOOR_ADDRESS),
                    ): vol.All(int, vol.Range(min=0, max=255)),
                    vol.Required(
                        CONF_LIVE_VIEW_DURATION,
                        default=current.get(CONF_LIVE_VIEW_DURATION, DEFAULT_LIVE_VIEW_DURATION),
                    ): vol.All(int, vol.Range(min=5, max=120)),
                    vol.Required(
                        CONF_POLL_INTERVAL_MS,
                        default=current.get(CONF_POLL_INTERVAL_MS, DEFAULT_POLL_INTERVAL_MS),
                    ): vol.All(int, vol.Range(min=250, max=10000)),
                    vol.Required(
                        CONF_ENABLE_LOG_TAIL,
                        default=current.get(CONF_ENABLE_LOG_TAIL, DEFAULT_ENABLE_LOG_TAIL),
                    ): bool,
                    vol.Required(
                        CONF_ENABLE_MQTT_BRIDGE,
                        default=current.get(CONF_ENABLE_MQTT_BRIDGE, DEFAULT_ENABLE_MQTT_BRIDGE),
                    ): bool,
                    vol.Required(
                        CONF_MQTT_BASE_TOPIC,
                        default=current.get(CONF_MQTT_BASE_TOPIC, DEFAULT_MQTT_BASE_TOPIC),
                    ): str,
                }
            ),
        )
