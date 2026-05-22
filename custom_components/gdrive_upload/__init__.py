"""Google Drive Upload — wiederverwendet OAuth-Token der google_drive-Integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up gdrive_upload from a config entry."""
    _LOGGER.debug("gdrive_upload setup for entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"entry": entry}
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
