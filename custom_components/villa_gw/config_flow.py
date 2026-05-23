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
from .cloud_api import (
    CloudApiClient,
    CloudAuthError,
    CloudConnectionError,
    generate_ha_device_id,
    setup_cloud_device,
)
from .const import (
    CONF_BINDING_CODE,
    CONF_CACHED_CITY_ID,
    CONF_CACHED_CLOUD_UID,
    CONF_CACHED_SIP_ID,
    CONF_CACHED_SIP_PASSWORD,
    CONF_CACHED_SIP_SERVER,
    CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD,
    CONF_ENABLE_CLOUD,
    CONF_ENABLE_LOG_TAIL,
    CONF_ENABLE_MQTT_BRIDGE,
    CONF_ENABLE_MQTT_DISCOVERY,
    CONF_HA_DEVICE_ID,
    CONF_LIVE_VIEW_DURATION,
    CONF_MQTT_BASE_TOPIC,
    CONF_OUTDOOR_ADDRESS,
    CONF_POLL_INTERVAL_MS,
    CONF_WEB_PASSWORD,
    CONF_WEB_USERNAME,
    DEFAULT_ENABLE_CLOUD,
    DEFAULT_ENABLE_LOG_TAIL,
    DEFAULT_ENABLE_MQTT_BRIDGE,
    DEFAULT_ENABLE_MQTT_DISCOVERY,
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
        vol.Required(CONF_ENABLE_MQTT_DISCOVERY, default=DEFAULT_ENABLE_MQTT_DISCOVERY): bool,
        vol.Required(CONF_MQTT_BASE_TOPIC, default=DEFAULT_MQTT_BASE_TOPIC): str,
        vol.Required(CONF_ENABLE_CLOUD, default=DEFAULT_ENABLE_CLOUD): bool,
    }
)

# Second step — only shown when CONF_ENABLE_CLOUD is true.
# binding_code is optional: a missing/wrong code does NOT block SIP routing,
# Cloud routes ring-INVITEs to every device_record under the user account.
STEP_CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLOUD_EMAIL): str,
        vol.Required(CONF_CLOUD_PASSWORD): str,
        vol.Optional(CONF_BINDING_CODE, default=""): str,
    }
)


class VillaGwConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the user-facing setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        # Carries the validated user-step data into async_step_cloud so the
        # cloud-step can finalize the entry in a single async_create_entry.
        self._gw_data: dict[str, Any] | None = None
        # Generated lazily on the first cloud-step submit and REUSED across
        # validation-error retries. Regenerating per attempt would mint a
        # fresh device_record on every retried /api/v2/login, leaving orphan
        # records in the user's Cloud account.
        self._ha_device_id: str | None = None

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
                # MAC must be non-empty — empty/missing MAC would break unique_id
                # uniqueness and produce f-string identifiers with a leading
                # underscore (e.g. "_state" for entity_id). Reject upfront so
                # the user sees a clear error rather than silently corrupting
                # the entry's identity.
                if not mac:
                    _LOGGER.error("Villa GW returned empty MAC — cannot proceed")
                    errors["base"] = "cannot_connect"
                else:
                    # Use MAC as unique_id so re-adding the same GW doesn't duplicate
                    await self.async_set_unique_id(mac)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                    if user_input.get(CONF_ENABLE_CLOUD):
                        # Hold the GW data and route to the cloud step.
                        self._gw_data = dict(user_input)
                        return await self.async_step_cloud()
                    return self.async_create_entry(
                        title=f"Villa GW ({user_input[CONF_HOST]})",
                        data=user_input,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Second step: ask for Cloud account credentials.

        On submit, talks to `de.ilifestyle-cloud.com` to log in (which
        creates a device-record under our account and auto-issues
        sip_id+password). The optional binding_code is best-effort — failure
        does NOT block setup because Cloud routes SIP-INVITEs based on the
        user/device-record relationship, not formal slot binding.
        """
        if self._gw_data is None:
            # Defensive: should never happen because async_step_user gates this
            # transition. Raise visibly rather than relying on a stripped assert.
            raise RuntimeError("async_step_cloud called without GW data")
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = CloudApiClient(session=session)
            # Mint the HA device_id once and stick to it across retries — see
            # __init__ for the rationale (avoids phantom device_records).
            if self._ha_device_id is None:
                self._ha_device_id = generate_ha_device_id()
            ha_device_id = self._ha_device_id
            binding_code = (user_input.get(CONF_BINDING_CODE) or "").strip() or None
            try:
                cloud_data = await setup_cloud_device(
                    client,
                    email=user_input[CONF_CLOUD_EMAIL],
                    password=user_input[CONF_CLOUD_PASSWORD],
                    device_id=ha_device_id,
                    binding_code=binding_code,
                )
            except CloudAuthError:
                errors["base"] = "cloud_invalid_auth"
            except CloudConnectionError:
                errors["base"] = "cloud_cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Cloud setup")
                errors["base"] = "unknown"
            else:
                # Merge GW + Cloud into a single entry. Email+password are
                # persisted so the coordinator can re-issue SIP-credentials
                # via /api/v2/login if the cached pair ever rotates or fails.
                data = {
                    **self._gw_data,
                    CONF_CLOUD_EMAIL:           user_input[CONF_CLOUD_EMAIL],
                    CONF_CLOUD_PASSWORD:        user_input[CONF_CLOUD_PASSWORD],
                    CONF_HA_DEVICE_ID:          ha_device_id,
                    CONF_CACHED_CLOUD_UID:      cloud_data["uid"],
                    CONF_CACHED_CITY_ID:        cloud_data["city_id"],
                    CONF_CACHED_SIP_ID:         cloud_data["sip_id"],
                    CONF_CACHED_SIP_PASSWORD:   cloud_data["sip_password"],
                    CONF_CACHED_SIP_SERVER:     cloud_data["sip_server"],
                }
                return self.async_create_entry(
                    title=f"Villa GW ({self._gw_data[CONF_HOST]})",
                    data=data,
                )

        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_SCHEMA,
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
        # Held between init- and cloud-step so the cloud-step result can
        # be merged into the original toggle payload on commit.
        self._pending_options: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # Enabling cloud on an entry that has no cached SIP creds yet
            # requires the creds step — route there and finalize on return.
            data = {**self._entry.data, **self._entry.options}
            wants_cloud = user_input.get(CONF_ENABLE_CLOUD, False)
            has_cached_creds = bool(data.get(CONF_CACHED_SIP_ID))
            if wants_cloud and not has_cached_creds:
                self._pending_options = dict(user_input)
                return await self.async_step_cloud()
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
                        CONF_ENABLE_MQTT_DISCOVERY,
                        default=current.get(CONF_ENABLE_MQTT_DISCOVERY, DEFAULT_ENABLE_MQTT_DISCOVERY),
                    ): bool,
                    vol.Required(
                        CONF_MQTT_BASE_TOPIC,
                        default=current.get(CONF_MQTT_BASE_TOPIC, DEFAULT_MQTT_BASE_TOPIC),
                    ): str,
                    vol.Required(
                        CONF_ENABLE_CLOUD,
                        default=current.get(CONF_ENABLE_CLOUD, DEFAULT_ENABLE_CLOUD),
                    ): bool,
                }
            ),
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect Cloud creds when enabling SIP listener post-setup.

        Persists `cached_sip_*` + `ha_device_id` into the entry's `data`
        via `async_update_entry` (options-flow normally only writes to
        `options`, but these are persistent identity, not user-tweakable
        settings). The toggle itself goes through options as usual.
        """
        if self._pending_options is None:
            raise RuntimeError("async_step_cloud called without pending options")
        errors: dict[str, str] = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = CloudApiClient(session=session)
            # Reuse the existing ha_device_id if present — fresh setups
            # mint a new one. Stable id keeps Cloud's device-record under
            # the user's account from drifting on re-configures.
            existing = {**self._entry.data, **self._entry.options}
            ha_device_id = existing.get(CONF_HA_DEVICE_ID) or generate_ha_device_id()
            binding_code = (user_input.get(CONF_BINDING_CODE) or "").strip() or None
            try:
                cloud_data = await setup_cloud_device(
                    client,
                    email=user_input[CONF_CLOUD_EMAIL],
                    password=user_input[CONF_CLOUD_PASSWORD],
                    device_id=ha_device_id,
                    binding_code=binding_code,
                )
            except CloudAuthError:
                errors["base"] = "cloud_invalid_auth"
            except CloudConnectionError:
                errors["base"] = "cloud_cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during Cloud setup (options)")
                errors["base"] = "unknown"
            else:
                new_data = {
                    **self._entry.data,
                    CONF_CLOUD_EMAIL:         user_input[CONF_CLOUD_EMAIL],
                    CONF_CLOUD_PASSWORD:      user_input[CONF_CLOUD_PASSWORD],
                    CONF_HA_DEVICE_ID:        ha_device_id,
                    CONF_CACHED_CLOUD_UID:    cloud_data["uid"],
                    CONF_CACHED_CITY_ID:      cloud_data["city_id"],
                    CONF_CACHED_SIP_ID:       cloud_data["sip_id"],
                    CONF_CACHED_SIP_PASSWORD: cloud_data["sip_password"],
                    CONF_CACHED_SIP_SERVER:   cloud_data["sip_server"],
                }
                self.hass.config_entries.async_update_entry(
                    self._entry, data=new_data,
                )
                return self.async_create_entry(
                    title="", data=self._pending_options,
                )

        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_SCHEMA,
            errors=errors,
        )
