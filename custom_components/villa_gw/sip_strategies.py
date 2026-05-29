"""INVITE response strategies — pluggable SIP behaviour on an incoming ring.

Ring-detection (the client's `on_invite` callback) fires independently; a
strategy decides only the SIP *response* and any media handling. New
experiments — e.g. a future `Answer200Strategy` — drop in as new classes
without touching the `SipClient` state machine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Protocol

from .sip_messages import USER_AGENT, extract_remote_media

if TYPE_CHECKING:
    from .sip_client import SipClient

_LOGGER = logging.getLogger(__name__)


class InviteStrategy(Protocol):
    """Decides how the client responds to an incoming INVITE."""

    async def respond(
        self, client: "SipClient", invite_msg: str, local_tag: str,
    ) -> None: ...


class SilentStrategy:
    """Default & safe: send NO SIP response.

    Lets the Cloud's parallel iPhone-fork own the call; our branch times out
    cleanly. Any active response (180/183/486/200) marginalises the iPhone
    fork, so silence is the production default.
    """

    async def respond(
        self, client: "SipClient", invite_msg: str, local_tag: str,
    ) -> None:
        return


class EarlyMedia183Strategy:
    """One-shot diagnostic (Schritt 2): 183 Session Progress + SDP, no 200 OK.

    Replies 183 + SDP (PCMU recvonly), nudges symmetric RTP at the Cloud
    relay, and listens ~25 s for early-media RTP — then reports via
    `on_result`. Never answers, so the outdoor station stays out of talk-mode,
    the iPhone fork can still take the call, and the ring can still be missed.
    """

    def __init__(
        self,
        on_result: Callable[[str], None] | None = None,
        *,
        rtp_port: int = 40000,
        listen_s: float = 23.0,
    ) -> None:
        self._on_result = on_result
        self._rtp_port = rtp_port
        self._listen_s = listen_s

    def _build_183(self, client: "SipClient", req_msg: str, local_tag: str) -> bytes:
        lines = req_msg.split("\r\n")
        via = next((ln for ln in lines if ln.lower().startswith("via:")), "")
        fr = next((ln for ln in lines if ln.lower().startswith("from:")), "")
        to = next((ln for ln in lines if ln.lower().startswith("to:")), "")
        cid = next((ln for ln in lines if ln.lower().startswith("call-id:")), "")
        cseq = next((ln for ln in lines if ln.lower().startswith("cseq:")), "")
        if ";tag=" not in to:
            to = to.rstrip() + f";tag={local_tag}"
        ip = client.transport.local_ip
        contact = f"sip:{client.user}@{ip}:{client.transport.local_port};transport=tls"
        body = (
            "v=0\r\n"
            f"o=hass 0 0 IN IP4 {ip}\r\n"
            "s=villa-gw-earlymedia-probe\r\n"
            f"c=IN IP4 {ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {self._rtp_port} RTP/AVP 0 101\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=recvonly\r\n"
        ).encode()
        head = (
            "SIP/2.0 183 Session Progress\r\n"
            f"{via}\r\n{fr}\r\n{to}\r\n{cid}\r\n{cseq}\r\n"
            f"Contact: <{contact}>\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            "Content-Type: application/sdp\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        return head + body

    def _report(self, summary: str) -> None:
        _LOGGER.info("Early-media probe result: %s", summary)
        if self._on_result is not None:
            self._on_result(summary)

    async def respond(
        self, client: "SipClient", invite_msg: str, local_tag: str,
    ) -> None:
        try:
            remote = extract_remote_media(invite_msg)
            local_ip = client.transport.local_ip
            await client.transport.send(
                self._build_183(client, invite_msg, local_tag)
            )
            _LOGGER.info(
                "Early-media probe: 183 gesendet, höre auf %s:%d, remote-media=%s",
                local_ip, self._rtp_port, remote,
            )
            loop = asyncio.get_event_loop()
            stats = {"pkts": 0, "bytes": 0, "src": None}

            class _RtpProto(asyncio.DatagramProtocol):
                def datagram_received(self, data: bytes, addr: tuple) -> None:
                    stats["pkts"] += 1
                    stats["bytes"] += len(data)
                    if stats["src"] is None:
                        stats["src"] = f"{addr[0]}:{addr[1]}"

            try:
                udp, _ = await loop.create_datagram_endpoint(
                    _RtpProto, local_addr=("0.0.0.0", self._rtp_port),
                )
            except OSError as err:
                self._report(f"bind failed: {err}")
                return
            try:
                # Symmetric-RTP nudge so the Cloud's NAT-aware relay latches
                # our public mapping and returns media to us.
                if remote:
                    pkt = (
                        b"\x80\x00\x00\x01\x00\x00\x00\xa0\x00\x00\x00\x01"
                        + b"\xff" * 160
                    )
                    for _ in range(8):
                        try:
                            udp.sendto(pkt, remote)
                        except OSError:
                            pass
                        await asyncio.sleep(0.25)
                await asyncio.sleep(self._listen_s)
            finally:
                udp.close()
            if stats["pkts"]:
                self._report(
                    f"{stats['pkts']} RTP-Pakete / {stats['bytes']} B von "
                    f"{stats['src']} (~{stats['pkts'] / 25.0:.0f}/s) → EARLY MEDIA OK"
                )
            else:
                self._report("KEIN early-media RTP empfangen (kein Early-Media oder NAT)")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Early-media probe error: %s", err)
            self._report(f"probe error: {err}")
