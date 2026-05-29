"""Bus action surface (uart2d, port 10087).

TCP `uart2d:10087` for fire-and-forget bus commands (wake/live-view,
unlock, hook/hang, intercom call, switch camera). These map to the
source-filtered `AT+B UART …` command family.
"""

from __future__ import annotations

import asyncio

from .const import PORT_UART2D
from .gw_base import VillaGwConnectionError

UART2D_TIMEOUT = 5


class VillaGwBusMixin:
    """uart2d bus actions (port 10087). Mixed into VillaGwClient."""

    async def _uart2d_send(self, command: str) -> None:
        """Fire-and-forget command to uart2d:10087."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, PORT_UART2D),
                timeout=UART2D_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise VillaGwConnectionError(f"uart2d connect: {err}") from err
        try:
            try:
                writer.write(command.encode() + b"\r\n")
                await writer.drain()
            except OSError as err:
                raise VillaGwConnectionError(f"uart2d io: {err}") from err
            try:
                await asyncio.wait_for(reader.read(256), timeout=1)
            except (asyncio.TimeoutError, OSError):
                pass  # don't fail on optional response read
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def wake_live_view(
        self, address: int = 1, duration: int = 30, key_index: int = 1
    ) -> None:
        await self._uart2d_send(f"AT+B UART monitor 1 {address} {duration}")

    async def stop_live_view(self, address: int = 1) -> None:
        await self._uart2d_send(f"AT+B UART monitor 0 {address} 0")

    async def unlock_door(self, relay: int = 1) -> None:
        await self._uart2d_send(f"AT+B UART unlock {relay}")

    async def hook_call(self, call_id: int = 1) -> None:
        await self._uart2d_send(f"AT+B UART hook {call_id}")

    async def hang_call(self, call_id: int = 1) -> None:
        await self._uart2d_send(f"AT+B UART hang {call_id}")

    async def call_outdoor(self, key_index: int = 1, address: int = 1) -> None:
        await self._uart2d_send(f"AT+B UART call {key_index} {address}")

    async def switch_camera(self) -> None:
        await self._uart2d_send("AT+B UART switchCamera")

    async def send_uart2d(self, command: str) -> None:
        """Public alias for _uart2d_send — fire-and-forget."""
        await self._uart2d_send(command)
