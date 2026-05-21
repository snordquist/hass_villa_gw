"""Camera entity for the Villa GW RTSP live stream."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import VillaGwCoordinator, get_coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Villa GW camera entity."""
    coordinator = get_coordinator(hass, entry)
    async_add_entities([VillaGwCamera(coordinator, entry)], update_before_add=False)


class VillaGwCamera(Camera):
    """RTSP camera fed from `rtsp://<gw>/live.sdp`.

    The stream only contains real video while a `monitor` or `call` session is
    active on the bus. Outside that window, ffmpeg sees the encoder's standby
    frame ("no signal" blue). Use the Wake button to start a live session.
    """

    _attr_has_entity_name = True
    _attr_name = "Live"
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "HHG / EGB"
    _attr_model = "Villa GW (AVL20P)"

    def __init__(self, coordinator: VillaGwCoordinator, entry: ConfigEntry) -> None:
        super().__init__()
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.unique_id}_camera"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            manufacturer="HHG / EGB",
            model="Villa GW (AVL20P)",
            name="Villa GW",
            configuration_url=f"http://{coordinator.client.host}",
        )

    async def stream_source(self) -> str | None:
        return await self.coordinator.client.rtsp_url()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Single-frame snapshot via ffmpeg."""
        stream_url = await self.coordinator.client.rtsp_url()
        ffmpeg = get_ffmpeg_manager(self.hass)
        from haffmpeg.tools import IMAGE_JPEG, ImageFrame  # noqa: PLC0415

        ff = ImageFrame(ffmpeg.binary)
        image = await ff.get_image(
            stream_url,
            output_format=IMAGE_JPEG,
            extra_cmd="-rtsp_transport tcp",
        )
        return image
