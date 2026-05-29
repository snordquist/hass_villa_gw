"""Read-only AT+B query surface (avlink, port 10086).

TCP `avlink:10086` for read-only `AT+B` queries (`APPLICATION`, `SYSTEM`,
`CHECKSIP …`) used by the polling event detector. These are accepted from
any source (only `AT+B UART …` is source-filtered — see `gw_bus`).
"""

from __future__ import annotations

import asyncio
import json

from .const import PORT_AVLINK
from .gw_base import VillaGwConnectionError

# Per-connection timeout. AVLINK_TIMEOUT > smallest allowed poll interval
# (250 ms) on purpose — slow gateways genuinely take up to ~3 s to answer
# AT+B APPLICATION on a busy bus. Result: when the device is slow, the
# effective poll cadence is dominated by this timeout, not by the user's
# poll_interval_ms setting. That is intentional — a 250 ms cadence against
# a 2 s device just queues coroutines, it doesn't speed anything up.
AVLINK_TIMEOUT = 3


class VillaGwAvlinkMixin:
    """AT+B status queries (port 10086). Mixed into VillaGwClient."""

    async def _avlink_query(self, command: str) -> str:
        """Send one AT+B command to avlink and return the raw response.

        Used for `AT+B APPLICATION`, `AT+B SYSTEM`, `AT+B CONTACTS`, `AT+B CHECKSIP …`.
        These are accepted from any source (only `AT+B UART …` is source-filtered).
        Any I/O error is wrapped in VillaGwConnectionError so callers can handle
        connection issues uniformly.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, PORT_AVLINK),
                timeout=AVLINK_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise VillaGwConnectionError(f"avlink connect: {err}") from err
        try:
            try:
                writer.write(command.encode() + b"\r\n")
                await writer.drain()
                data = await asyncio.wait_for(reader.read(4096), timeout=AVLINK_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as err:
                raise VillaGwConnectionError(f"avlink io: {err}") from err
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        return data.decode("utf-8", errors="replace").strip()

    async def application(self) -> dict:
        """Poll `AT+B APPLICATION` → `{state, sip, call}`.

        Primary event-detection signal. Poll at ~1 Hz to catch ringings.
        """
        raw = await self._avlink_query("AT+B APPLICATION")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as err:
            raise VillaGwConnectionError(f"application: bad JSON {raw!r}") from err

    async def system_status(self) -> dict:
        """`AT+B SYSTEM` → uptime, memory, ADC pins (door_in is the bell pin)."""
        raw = await self._avlink_query("AT+B SYSTEM")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as err:
            raise VillaGwConnectionError(f"system: bad JSON {raw!r}") from err

    async def sip_status(self) -> str:
        """`AT+B CHECKSIP 2` → current SIP-registration status string."""
        return await self._avlink_query("AT+B CHECKSIP 2")

    async def send_avlink(self, command: str) -> str:
        """Public alias for _avlink_query — returns the raw response string."""
        return await self._avlink_query(command)
