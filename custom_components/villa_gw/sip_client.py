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
import hashlib
import logging
import re
import secrets
import ssl
from typing import Awaitable, Callable, Protocol


_LOGGER = logging.getLogger(__name__)

USER_AGENT = "HA-Villa-SIP/0.1"
REREGISTER_INTERVAL = 500  # seconds (Cloud's default Expires is 600)
DEFAULT_SIP_PORT = 5061


# ──────────────────────────────────────────────────────── pure helpers


def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def parse_headers(msg: str) -> dict[str, str]:
    """Parse SIP message headers into a dict (case-insensitive keys).

    The method/status line is skipped. Values keep their original
    spacing (we just `.strip()` the leading/trailing whitespace) so
    embedded colons (e.g. in nonces) survive.
    """
    headers: dict[str, str] = {}
    method_prefixes = (
        "SIP/", "INVITE ", "REGISTER ", "OPTIONS ", "NOTIFY ",
        "ACK ", "BYE ", "CANCEL ", "MESSAGE ",
    )
    for line in msg.split("\r\n"):
        if not line or line.startswith(method_prefixes):
            continue
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return headers


def parse_digest_challenge(www_auth: str) -> dict[str, str]:
    """Parse a `Digest realm="x", nonce="y", ...` challenge into kv-dict.

    Non-`Digest` schemes (Basic, Bearer) return an empty dict so callers
    can treat "no challenge" and "unsupported scheme" identically.
    """
    if not www_auth.lower().startswith("digest"):
        return {}
    out: dict[str, str] = {}
    for k, v in re.findall(r'(\w+)\s*=\s*"?([^",]*)"?', www_auth):
        out[k.lower()] = v
    return out


def build_digest_auth(
    user: str, password: str, challenge: dict[str, str],
    *, method: str, uri: str,
) -> str:
    """Build an `Authorization: Digest ...` header value.

    Honours `qop=auth` by including the required `nc` + `cnonce`. The
    cnonce is freshly random per call, so two REGISTERs with the same
    nonce can't reuse digest material.
    """
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algorithm = challenge.get("algorithm", "MD5")
    qop = challenge.get("qop", "")
    ha1 = md5_hex(f"{user}:{realm}:{password}")
    ha2 = md5_hex(f"{method}:{uri}")
    if "auth" in qop:
        nc = "00000001"
        cnonce = secrets.token_hex(8)
        resp = md5_hex(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        return (
            f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
            f'uri="{uri}", response="{resp}", algorithm={algorithm}, '
            f'qop=auth, nc={nc}, cnonce="{cnonce}"'
        )
    resp = md5_hex(f"{ha1}:{nonce}:{ha2}")
    return (
        f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
        f'uri="{uri}", response="{resp}", algorithm={algorithm}'
    )


def build_register(
    *, user: str, server: str, local_ip: str, local_port: int,
    call_id: str, from_tag: str, branch: str, cseq: int,
    auth_header: str | None = None,
) -> bytes:
    contact = f"sip:{user}@{local_ip}:{local_port};transport=tls"
    msg = (
        f"REGISTER sip:{server} SIP/2.0\r\n"
        f"Via: SIP/2.0/TLS {local_ip}:{local_port};branch={branch};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:{user}@{server}>;tag={from_tag}\r\n"
        f"To: <sip:{user}@{server}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: {cseq} REGISTER\r\n"
        f"Contact: <{contact}>;expires=600\r\n"
        f"Expires: 600\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        f"Allow: INVITE, ACK, CANCEL, OPTIONS, BYE\r\n"
    )
    if auth_header:
        msg += f"Authorization: {auth_header}\r\n"
    msg += "Content-Length: 0\r\n\r\n"
    return msg.encode()


def _copy_response_headers(
    req_msg: str, status_line: str, local_tag: str,
) -> bytes:
    """Build a SIP response by reusing Via/From/To/Call-ID/CSeq from request.

    Appends `;tag=<local_tag>` to the To-header only if not already
    present (RFC 3261 §8.2.6.2 — dialog-establishing responses MUST
    carry a To-tag, but if the request side already produced one for
    this dialog we keep it).
    """
    lines = req_msg.split("\r\n")
    via = next((ln for ln in lines if ln.lower().startswith("via:")), "")
    fr = next((ln for ln in lines if ln.lower().startswith("from:")), "")
    to = next((ln for ln in lines if ln.lower().startswith("to:")), "")
    cid = next((ln for ln in lines if ln.lower().startswith("call-id:")), "")
    cseq = next((ln for ln in lines if ln.lower().startswith("cseq:")), "")
    if ";tag=" not in to:
        to = to.rstrip() + f";tag={local_tag}"
    resp = (
        f"{status_line}\r\n"
        f"{via}\r\n{fr}\r\n{to}\r\n{cid}\r\n{cseq}\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    return resp.encode()


def build_ok_response(req_msg: str) -> bytes:
    """Generic 200 OK for OPTIONS/NOTIFY/CANCEL/BYE — keeps Cloud happy."""
    return _copy_response_headers(
        req_msg, "SIP/2.0 200 OK", f"hass-{secrets.token_hex(4)}",
    )


def build_terminated_response(req_msg: str, *, local_tag: str) -> bytes:
    """487 Request Terminated for an INVITE we already learned was cancelled."""
    return _copy_response_headers(
        req_msg, "SIP/2.0 487 Request Terminated", local_tag,
    )


def extract_invite_info(invite_msg: str) -> dict[str, str]:
    """Return a dict ready to merge into the HA bus payload."""
    h = parse_headers(invite_msg)
    return {
        "call_id": h.get("call-id", ""),
        "from_sip": h.get("from", ""),
        "to_sip": h.get("to", ""),
    }


def extract_remote_media(msg: str) -> tuple[str, int] | None:
    """From an INVITE+SDP return the remote audio (ip, port).

    That is the address the Cloud relay receives on for our leg — where we
    send symmetric-RTP nudges so its NAT-aware relay latches our mapping and
    starts sending early-media back to us. Returns None if no audio media line.
    """
    ip: str | None = None
    port: int | None = None
    for raw in msg.split("\r\n"):
        s = raw.strip()
        if s.startswith("c=IN IP4 ") and ip is None:
            ip = s[len("c=IN IP4 "):].strip().split("/")[0] or None
        elif s.startswith("m=audio "):
            parts = s.split()
            if len(parts) >= 2 and parts[1].isdigit():
                port = int(parts[1])
    if ip and port:
        return ip, port
    return None


# ──────────────────────────────────────────────────────── transport protocol


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
        # One-shot Early-Media probe (Schritt 2): when is_probe_armed() is True
        # at INVITE time, reply 183 Session Progress + SDP (PCMU, recvonly) —
        # WITHOUT a 200 OK — and listen for early-media RTP. Never answers, so
        # the outdoor station stays out of talk-mode and the iPhone fork can
        # still take the call. on_probe_result(summary) reports what arrived.
        self._is_probe_armed = is_probe_armed
        self._on_probe_result = on_probe_result
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
            # Full raw INVITE (incl. SDP offer) at DEBUG. Lets us inspect the
            # offered audio codec (PCMU/PCMA), SRTP (a=crypto) and media
            # c=/m= addresses for the planned audio-capture path — without
            # sending anything (routing/iPhone-fork untouched). First INVITE
            # only (retransmits already returned above).
            _LOGGER.debug("Cloud SIP INVITE (raw):\n%s", msg)
            # One-shot Early-Media probe (Schritt 2): reply 183 + SDP and
            # listen for early-media RTP, but NEVER 200 OK — the call stays
            # unanswered (no door talk-mode, iPhone fork can still take it).
            if self._is_probe_armed is not None and self._is_probe_armed():
                try:
                    await self._run_early_media_probe(msg, local_tag)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Early-media probe error: %s", err)
                    if self._on_probe_result is not None:
                        self._on_probe_result(f"probe error: {err}")
            # Silent mode — do NOT send any SIP response. The Cloud will let
            # the parallel iPhone-fork take the call and our branch times
            # out cleanly in ~32s without affecting routing.
            if self._on_invite is not None:
                res = self._on_invite(info)
                if asyncio.iscoroutine(res):
                    await res
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

    def _build_183(self, req_msg: str, rtp_port: int, local_tag: str) -> bytes:
        """183 Session Progress + SDP (PCMU recvonly) — NOT a 200 OK.

        Signals we can receive early media without answering the call, so the
        outdoor station never enters talk-mode and the iPhone fork can still
        take it.
        """
        lines = req_msg.split("\r\n")
        via = next((ln for ln in lines if ln.lower().startswith("via:")), "")
        fr = next((ln for ln in lines if ln.lower().startswith("from:")), "")
        to = next((ln for ln in lines if ln.lower().startswith("to:")), "")
        cid = next((ln for ln in lines if ln.lower().startswith("call-id:")), "")
        cseq = next((ln for ln in lines if ln.lower().startswith("cseq:")), "")
        if ";tag=" not in to:
            to = to.rstrip() + f";tag={local_tag}"
        ip = self._transport.local_ip
        contact = f"sip:{self._user}@{ip}:{self._transport.local_port};transport=tls"
        body = (
            "v=0\r\n"
            f"o=hass 0 0 IN IP4 {ip}\r\n"
            "s=villa-gw-earlymedia-probe\r\n"
            f"c=IN IP4 {ip}\r\n"
            "t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0 101\r\n"
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

    async def _run_early_media_probe(self, invite_msg: str, local_tag: str) -> None:
        """One-shot: send 183+SDP, nudge symmetric RTP, listen ~25s.

        Diagnostic for Schritt 2 — does the Cloud deliver the outdoor-mic audio
        as early media (before any 200 OK)? Never answers the call.
        """
        rtp_port = 40000
        remote = extract_remote_media(invite_msg)
        local_ip = self._transport.local_ip
        await self._transport.send(self._build_183(invite_msg, rtp_port, local_tag))
        _LOGGER.info(
            "Early-media probe: 183 gesendet, höre auf %s:%d, remote-media=%s",
            local_ip, rtp_port, remote,
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
                _RtpProto, local_addr=("0.0.0.0", rtp_port),
            )
        except OSError as err:
            _LOGGER.warning("Early-media probe: UDP %d nicht bindbar: %s", rtp_port, err)
            if self._on_probe_result is not None:
                self._on_probe_result(f"bind failed: {err}")
            return
        try:
            # Symmetric-RTP nudge so the Cloud's NAT-aware relay latches our
            # public mapping and returns media to us.
            if remote:
                pkt = b"\x80\x00\x00\x01\x00\x00\x00\xa0\x00\x00\x00\x01" + b"\xff" * 160
                for _ in range(8):
                    try:
                        udp.sendto(pkt, remote)
                    except OSError:
                        pass
                    await asyncio.sleep(0.25)
            await asyncio.sleep(23)
        finally:
            udp.close()
        if stats["pkts"]:
            summary = (
                f"{stats['pkts']} RTP-Pakete / {stats['bytes']} B von "
                f"{stats['src']} (~{stats['pkts'] / 25.0:.0f}/s) → EARLY MEDIA OK"
            )
        else:
            summary = "KEIN early-media RTP empfangen (kein Early-Media oder NAT)"
        _LOGGER.info("Early-media probe result: %s", summary)
        if self._on_probe_result is not None:
            self._on_probe_result(summary)

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


# ──────────────────────────────────────────────────────── TLS transport


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
