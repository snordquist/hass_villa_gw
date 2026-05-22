"""Google Drive Upload — wiederverwendet OAuth-Token der google_drive-Integration."""

from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import config_entry_oauth2_flow

from .api import DriveApi, DriveApiError
from .const import (
    ATTR_FILE_PATH,
    ATTR_FOLDER_PATH,
    ATTR_SHARE,
    CONF_DRIVE_ENTRY_ID,
    DOMAIN,
    SERVICE_UPLOAD,
)

_LOGGER = logging.getLogger(__name__)

UPLOAD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_FILE_PATH): cv.string,
        vol.Required(ATTR_FOLDER_PATH): cv.string,
        vol.Optional(ATTR_SHARE, default=False): cv.boolean,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up gdrive_upload — bind to the chosen google_drive entry."""
    drive_entry_id = entry.data[CONF_DRIVE_ENTRY_ID]
    drive_entry = hass.config_entries.async_get_entry(drive_entry_id)
    if drive_entry is None or drive_entry.state.name != "LOADED":
        raise HomeAssistantError(
            f"google_drive entry {drive_entry_id} not loaded — re-link in options"
        )

    # Build an OAuth2Session bound to the foreign (google_drive) entry's
    # implementation. This refreshes tokens automatically on 401.
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, drive_entry
        )
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, drive_entry, implementation)

    async def _request(method: str, url: str, **kwargs: Any):
        return await session.async_request(method, url, **kwargs)

    api = DriveApi(_request)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = api

    async def _upload(call: ServiceCall) -> dict[str, Any]:
        file_path = call.data[ATTR_FILE_PATH]
        folder_path = call.data[ATTR_FOLDER_PATH]
        share = call.data.get(ATTR_SHARE, False)

        if not os.path.isfile(file_path):
            raise HomeAssistantError(f"file not found: {file_path}")
        try:
            folder_id = await api.ensure_folder(folder_path)
            meta = await api.upload(
                file_path, folder_id=folder_id, filename=os.path.basename(file_path)
            )
            response: dict[str, Any] = {
                "file_id": meta["id"],
                "web_view_link": meta.get("webViewLink"),
            }
            if share:
                response["share_url"] = await api.make_shareable(meta["id"])
            return response
        except DriveApiError as err:
            raise HomeAssistantError(f"drive upload failed: {err}") from err

    # Idempotent registration (handle reload)
    if not hass.services.has_service(DOMAIN, SERVICE_UPLOAD):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPLOAD,
            _upload,
            schema=UPLOAD_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_UPLOAD)
    return True
