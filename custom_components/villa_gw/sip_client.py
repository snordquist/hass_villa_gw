"""iLifestyle Cloud SIP listener — passive ring observer.

Registers as a 2nd App-User at the iLifestyle Cloud SIP-server. When the
doorbell is rung, Cloud forks the SIP-INVITE to all bound endpoints; this
client receives the INVITE, parses it, and fires an `on_invite` callback
**without sending any SIP response**.

## Why silent — see project memory `feedback_villa_gw_rtsp_no_keyframes_idle`
and the POC findings: any active response (180 / 486 / CANCEL+487) makes
the Cloud SIP-server treat OUR branch as the responsible endpoint,
breaking the parallel iPhone-fork. Silent observation lets the iPhone-fork
own the call while we still receive the ring event.

## Architecture

`SipClient` owns a `SipTransport` (the wire). `SipTransport` is an
abstract async send/recv pair so tests can inject an in-memory fake
without needing a TLS socket. The default transport `TlsSipTransport`
opens a TCP+TLS socket to the Cloud server.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Awaitable, Callable

from .sip_messages import (
    build_digest_auth,
    build_ok_response,
    build_register,
    build_terminated_response,
    extract_invite_info,
    parse_digest_challenge,
    parse_headers,
)
from .sip_strategies import EarlyMedia183Strategy, SilentStrategy
# SipTransport used in annotations; TlsSipTransport re-exported for coordinator + tests
from .sip_transport import SipTransport, TlsSipTransport  # noqa: F401

_LOGGER = logging.getLogger(__name__)

REREGISTER_INTERVAL = 500  # seconds (Cloud's default Expires is 600)


OnInvite = Callable[[dict[str, str]], Awaitable[None]] | Callable[[dict[str, str]], None]


# ──────────────────────────────────────────────────────── SipClient


class SipClient:
    """Owns a `SipTransport` and runs the SIP protocol state machine.

    Use:
      client = SipClient(server="...", user="...", password="...",
                          transport=tls_transport, on_invite=async_cb)
      await client.register_once()
      while running: await client.process_one_message()

    Or simply `await client.run()` for the full lifecycle (REGISTER →
    listen → re-REGISTER every 500s) — see `run()` docstring.
    """

    def __init__(
        self,
        *,
        server: str,
        user: str,
        password: str,
        transport: "SipTransport",
        on_invite: OnInvite | None = None,
        on_registered: Callable[[], None] | None = None,
        is_probe_armed: Callable[[], bool] | None = None,
        on_probe_result: Callable[[str], None] | None = None,
        active_invite_ttl_s: float = 60.0,
    ) -> None:
        self._server = server
        self._user = user
        self._password = password
        self._transport = transport
        self._on_invite = on_invite
        # INVITE response is delegated to a pluggable strategy (ring-detection
        # via on_invite fires independently). Silent by default; when
        # is_probe_armed() is True at INVITE time we use the one-shot
        # Early-Media 183 probe (Schritt 2) — see sip_strategies.py.
        self._is_probe_armed = is_probe_armed
        self._silent_strategy: object = SilentStrategy()
        self._probe_strategy: object = EarlyMedia183Strategy(on_result=on_probe_result)
        # Fired after every successful (re-)REGISTER inside run(). Lets the
        # coordinator flip `cloud_sip_connected` True and reset its backoff
        # only on a genuine, sustained registration — not optimistically.
        self._on_registered = on_registered
        # Human-readable reason for the last REGISTER failure (401 w/o digest,
        # unexpected status, timeout). Surfaced in the coordinator's WARNING
        # log so a rejected listener is debuggable without DEBUG logging.
        self.last_register_error: str | None = None
        # SIP message state
        self._call_id = secrets.token_hex(8) + "@hass"
        self._from_tag = secrets.token_hex(4)
        self._cseq = 0
        # Per-INVITE state: call-id → (raw INVITE bytes, local-tag, added_at_mono)
        # so a later CANCEL can be answered with 487 against the right dialog.
        # In silent-mode many INVITEs never see a CANCEL (iPhone accepted →
        # Cloud doesn't notify us). Without eviction this dict grows
        # unbounded over months — see `_evict_stale_invites`.
        self._active_invites: dict[str, tuple[str, str, float]] = {}
        self._active_invite_ttl_s = active_invite_ttl_s
        # Receive buffer (SIP messages can arrive split across TCP segments)
        self._buf = b""
        # Clock injectable for deterministic tests
        self._now_fn: Callable[[], float] = lambda: asyncio.get_event_loop().time()

    @property
    def transport(self) -> "SipTransport":
        """The active wire — read by strategies to send responses / read addrs."""
        return self._transport

    @property
    def user(self) -> str:
        """Our SIP user (for Contact / SDP origin in strategies)."""
        return self._user

    # ────────────────────────── REGISTER

    def _next_branch(self) -> str:
        return "z9hG4bK-hass-" + secrets.token_hex(4)

    def _new_register(self, auth_header: str | None = None) -> bytes:
        self._cseq += 1
        return build_register(
            user=self._user, server=self._server,
            local_ip=self._transport.local_ip,
            local_port=self._transport.local_port,
            call_id=self._call_id, from_tag=self._from_tag,
            branch=self._next_branch(), cseq=self._cseq,
            auth_header=auth_header,
        )

    # CSeq line of a 200 OK response to a REGISTER — anchored to the
    # CSeq header so an unrelated 200 OK whose body mentions "REGISTER"
    # (e.g. in an Allow header) is not misread as success.
    _CSEQ_REGISTER_RE = re.compile(
        r"(?im)^CSeq:\s*\d+\s+REGISTER\s*$",
    )

    async def register_once(self, *, timeout_s: float = 6.0) -> bool:
        """Send REGISTER, handle 401-challenge → Digest → 200 OK.

        Returns True on success (200 OK with `CSeq: N REGISTER`), False on
        any other terminal response or after `timeout_s` of no useful reply.
        """
        await self._transport.send(self._new_register())
        end = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < end:
            chunk = await self._transport.recv()
            if not chunk:
                await asyncio.sleep(0.05)
                continue
            text = chunk.decode(errors="replace")
            if "SIP/2.0 401" in text:
                m = re.search(
                    r"WWW-Authenticate:\s*Digest\s+(.+?)(?:\r\n[A-Z]|\r\n\r\n)",
                    text, re.DOTALL | re.IGNORECASE,
                )
                if not m:
                    self.last_register_error = "401 without Digest challenge"
                    _LOGGER.debug("SIP REGISTER 401 without Digest challenge")
                    return False
                chall = parse_digest_challenge("Digest " + m.group(1))
                auth = build_digest_auth(
                    self._user, self._password, chall,
                    method="REGISTER", uri=f"sip:{self._server}",
                )
                await self._transport.send(self._new_register(auth_header=auth))
                continue
            if "SIP/2.0 200" in text and self._CSEQ_REGISTER_RE.search(text):
                self.last_register_error = None
                _LOGGER.debug("SIP REGISTER OK")
                return True
            # Capture the status line (e.g. "SIP/2.0 403 Forbidden") so the
            # caller can log *why* registration was rejected.
            status = text.splitlines()[0].strip() if text.strip() else "<empty>"
            self.last_register_error = f"unexpected response: {status}"
            _LOGGER.debug("SIP REGISTER unexpected response: %s", text[:120])
            return False
        self.last_register_error = f"timeout after {timeout_s:.0f}s (no useful reply)"
        _LOGGER.debug("SIP REGISTER timeout")
        return False

    # ────────────────────────── Message loop

    def _evict_stale_invites(self, now_mono: float) -> None:
        """Drop entries older than `_active_invite_ttl_s`.

        Called whenever we add a new INVITE. iPhone-accepted rings never
        produce a CANCEL to our endpoint, so without this the dict
        accumulates one entry per ring forever (~1 kB each).
        """
        ttl = self._active_invite_ttl_s
        stale = [
            cid for cid, (_, _, added) in self._active_invites.items()
            if (now_mono - added) > ttl
        ]
        for cid in stale:
            self._active_invites.pop(cid, None)

    async def _dispatch(self, msg: str) -> None:
        first_line = msg.split("\r\n", 1)[0]
        if first_line.startswith("INVITE "):
            info = extract_invite_info(msg)
            cid = info["call_id"]
            # RFC 3261 requires a Call-ID; an INVITE without one is
            # malformed. Without this guard, every header-less INVITE
            # would collide on the empty-string key and silently drop
            # subsequent rings. Log + ignore so a real protocol-level
            # bug stays visible.
            if not cid:
                _LOGGER.warning("SIP INVITE without Call-ID — ignoring")
                return
            now_mono = self._now_fn()
            self._evict_stale_invites(now_mono)
            # SIP INVITE retransmits (same Call-ID, e.g. on a brief TCP
            # blip just after the first send) must NOT replace the dialog
            # tag — if a CANCEL arrives later it carries the original
            # tag, and our 487 must match. Keep the first INVITE intact
            # and skip re-firing the on_invite callback.
            if cid in self._active_invites:
                return
            local_tag = f"hass-{secrets.token_hex(4)}"
            self._active_invites[cid] = (msg, local_tag, now_mono)
            # Full raw INVITE (incl. SDP offer) at DEBUG — inspect offered
            # codec / SRTP / media addresses for the audio-capture path.
            # First INVITE only (retransmits returned above).
            _LOGGER.debug("Cloud SIP INVITE (raw):\n%s", msg)
            # Ring-detection fires first (fast) and is independent of how we
            # respond on the wire.
            if self._on_invite is not None:
                res = self._on_invite(info)
                if asyncio.iscoroutine(res):
                    await res
            # The SIP response is the strategy's job: SilentStrategy (default,
            # lets the iPhone-fork own the call) or the one-shot Early-Media
            # 183 probe when armed. The strategy reports its own result/errors.
            armed = self._is_probe_armed is not None and self._is_probe_armed()
            strategy = self._probe_strategy if armed else self._silent_strategy
            await strategy.respond(self, msg, local_tag)
            return
        if first_line.startswith("CANCEL "):
            h = parse_headers(msg)
            cid = h.get("call-id", "")
            # 200 OK to the CANCEL transaction (stops Cloud's retransmits).
            await self._transport.send(build_ok_response(msg))
            # 487 Request Terminated for the original INVITE — RFC 3261 §9.2
            # cleanup. NOT a call-stealing response: by the time CANCEL
            # arrives, either the iPhone-fork already accepted (Cloud is just
            # cleaning us up) or the caller hung up (no fork to interfere
            # with). 487 is the verified-working behaviour from the POC.
            if cid in self._active_invites:
                orig, local_tag, _ = self._active_invites.pop(cid)
                await self._transport.send(
                    build_terminated_response(orig, local_tag=local_tag),
                )
            return
        if first_line.startswith(("OPTIONS ", "NOTIFY ", "BYE ")):
            await self._transport.send(build_ok_response(msg))
            return
        if first_line.startswith("ACK "):
            # Mid-dialog ACK — nothing to do
            return
        _LOGGER.debug("SIP unhandled: %s", first_line[:100])

    async def process_one_message(self) -> bool:
        """Read up to one full SIP message and dispatch it.

        Returns True if a message was processed, False if no full message
        was available yet (caller may want to sleep / continue).
        """
        # Try to assemble a message from the existing buffer first
        full = self._extract_complete_message()
        if full is None:
            chunk = await self._transport.recv()
            if not chunk:
                return False
            self._buf += chunk
            full = self._extract_complete_message()
        if full is None:
            return False
        await self._dispatch(full.decode(errors="replace"))
        return True

    def _extract_complete_message(self) -> bytes | None:
        if b"\r\n\r\n" not in self._buf:
            return None
        header_end = self._buf.index(b"\r\n\r\n") + 4
        head = self._buf[:header_end].decode(errors="replace")
        m = re.search(r"(?i)content-length:\s*(\d+)", head)
        content_len = int(m.group(1)) if m else 0
        total = header_end + content_len
        if len(self._buf) < total:
            return None
        msg = self._buf[:total]
        self._buf = self._buf[total:]
        return msg

    async def run(self) -> None:
        """REGISTER, then process messages forever; re-REGISTER every 500s.

        Caller is responsible for transport reconnect on terminal failure;
        this loop runs the SIP protocol layer only. Caller should wrap the
        whole thing in a backoff-loop at the coordinator level.

        Raises `RuntimeError` if the initial REGISTER or any periodic
        re-REGISTER fails — silently looping while unregistered would
        leave `cloud_sip_connected=True` lying to the user while INVITEs
        no longer reach us.
        """
        if not await self.register_once():
            raise RuntimeError(
                f"SIP REGISTER failed (initial): {self.last_register_error}"
            )
        if self._on_registered is not None:
            self._on_registered()
        loop = asyncio.get_event_loop()
        last_register = loop.time()
        while True:
            if loop.time() - last_register > REREGISTER_INTERVAL:
                if not await self.register_once():
                    raise RuntimeError(
                        f"SIP REGISTER failed (periodic): {self.last_register_error}"
                    )
                if self._on_registered is not None:
                    self._on_registered()
                last_register = loop.time()
            got = await self.process_one_message()
            if not got:
                await asyncio.sleep(0.1)


