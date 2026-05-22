"""Config flow for gdrive_upload."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector

from .const import CONF_DRIVE_ENTRY_ID, DOMAIN


class GdriveUploadConfigFlow(ConfigFlow, domain=DOMAIN):
    """User-facing flow to wire gdrive_upload to a google_drive entry."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            entry_id = user_input[CONF_DRIVE_ENTRY_ID]
            drive_entry: ConfigEntry | None = self.hass.config_entries.async_get_entry(
                entry_id
            )
            if drive_entry is None or drive_entry.domain != "google_drive":
                return self.async_abort(reason="invalid_drive_entry")
            return self.async_create_entry(
                title="Google Drive Upload",
                data={CONF_DRIVE_ENTRY_ID: entry_id},
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_DRIVE_ENTRY_ID): selector.ConfigEntrySelector(
                    {"integration": "google_drive"}
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
