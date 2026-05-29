"""SIP wire transport — abstract protocol + production TLS-over-TCP.

Split from `sip_client.py` so the network layer is isolated and mockable
(tests inject an in-memory fake implementing the `SipTransport` protocol).
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Protocol

DEFAULT_SIP_PORT = 5061


class SipTransport(Protocol):
    """Abstract async wire used by `SipClient`.

    The default implementation is `TlsSipTransport` (TLS over TCP); tests
    use an in-memory fake. Both `send` and `recv` are coroutines.
    """

    local_ip: str
    local_port: int

    async def send(self, data: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    async def close(self) -> None: ...


class TlsSipTransport:
    """Production SIP transport — TLS over TCP via `asyncio.open_connection`.

    The iLifestyle Cloud SIP server presents a self-signed certificate that
    doesn't validate against the public PKI; we therefore disable hostname
    verification and cert checking. This is acceptable because the SIP
    payload itself is the only thing exchanged — no credentials beyond the
    Digest hash (which is already a one-way function).
    """

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.local_ip: str = ""
        self.local_port: int = 0

    @staticmethod
    def _make_unverified_tls_context() -> ssl.SSLContext:
        """Build a client TLS context that verifies nothing — without I/O.

        The Cloud presents a self-signed cert and we use ``CERT_NONE``, so the
        system CA store is irrelevant. ``ssl.create_default_context()`` would
        load it anyway via blocking file I/O (``load_default_certs`` /
        ``set_default_verify_paths``), which trips HA's event-loop
        blocking-call detector. A bare client context skips that entirely.
        ``check_hostname`` must be cleared before ``verify_mode`` or the stdlib
        raises.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @classmethod
    async def connect(
        cls, host: str, port: int = DEFAULT_SIP_PORT,
        *, connect_timeout_s: float = 10.0,
    ) -> "TlsSipTransport":
        ctx = cls._make_unverified_tls_context()
        transport = cls()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host=host, port=port, ssl=ctx),
            timeout=connect_timeout_s,
        )
        transport._reader = reader
        transport._writer = writer
        sock = writer.get_extra_info("socket")
        if sock is not None:
            transport.local_ip, transport.local_port = sock.getsockname()[:2]
        return transport

    async def send(self, data: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("TlsSipTransport not connected")
        self._writer.write(data)
        await self._writer.drain()

    async def recv(self) -> bytes:
        if self._reader is None:
            raise RuntimeError("TlsSipTransport not connected")
        # Bounded read so we yield control regularly to the event loop
        try:
            return await asyncio.wait_for(self._reader.read(16384), timeout=1.0)
        except asyncio.TimeoutError:
            return b""

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None
