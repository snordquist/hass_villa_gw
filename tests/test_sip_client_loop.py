"""Tests for `SipClient` with an in-memory fake transport.

These cover the message-loop behaviour: REGISTER state machine with
401-challenge → Digest-auth retry, INVITE → callback fire + silent
(no response sent), CANCEL → 200 + 487, OPTIONS → 200.

All IPs in here are RFC 5737 documentation addresses (192.0.2.0/24).
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from collections import deque
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "villa_gw"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pkg = types.ModuleType("villa_gw_test_sipclient_loop")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_sipclient_loop"] = pkg
sip = _load_module(
    "villa_gw_test_sipclient_loop.sip_client", PKG / "sip_client.py",
)


class FakeTransport:
    """In-memory SIP transport — bytes go into `sent`, scripted replies come out.

    `script` is a list of bytes that get returned from `recv()` in order.
    After exhaustion, `recv()` returns b"" so the client's read-loop sleeps.
    """

    local_ip = "192.0.2.42"
    local_port = 55555

    def __init__(self, script: list[bytes]) -> None:
        self.sent: list[bytes] = []
        self._script: deque[bytes] = deque(script)
        self.closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        if self._script:
            return self._script.popleft()
        # Idle — give the loop a beat to advance
        await asyncio.sleep(0)
        return b""

    async def close(self) -> None:
        self.closed = True


def _401_response(realm: str = "icloud", nonce: str = "test-nonce-1") -> bytes:
    return (
        f"SIP/2.0 401 Unauthorized\r\n"
        f"Via: SIP/2.0/TLS 192.0.2.42:55555;branch=z9hG4bK-x\r\n"
        f"From: <sip:alice@srv>;tag=t\r\n"
        f"To: <sip:alice@srv>;tag=server-tag\r\n"
        f"Call-ID: cid\r\n"
        f"CSeq: 1 REGISTER\r\n"
        f'WWW-Authenticate: Digest realm="{realm}", nonce="{nonce}", '
        f'algorithm=MD5\r\n'
        f"Content-Length: 0\r\n\r\n"
    ).encode()


def _200_ok_for(method: str) -> bytes:
    return (
        f"SIP/2.0 200 OK\r\n"
        f"Via: SIP/2.0/TLS 192.0.2.42:55555;branch=z9hG4bK-x\r\n"
        f"From: <sip:alice@srv>;tag=t\r\n"
        f"To: <sip:alice@srv>;tag=server-tag\r\n"
        f"Call-ID: cid\r\n"
        f"CSeq: 2 {method}\r\n"
        f"Content-Length: 0\r\n\r\n"
    ).encode()


@pytest.mark.asyncio
async def test_register_handles_401_then_succeeds() -> None:
    """After REGISTER w/o auth → 401, client retries with Digest, then 200 OK."""
    transport = FakeTransport(script=[
        _401_response(),
        _200_ok_for("REGISTER"),
    ])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport,
    )
    ok = await client.register_once()
    assert ok is True
    # Two REGISTER messages sent — one without, one with Authorization
    assert len(transport.sent) == 2
    assert b"REGISTER sip:srv SIP/2.0" in transport.sent[0]
    assert b"Authorization:" not in transport.sent[0]
    assert b"Authorization: Digest" in transport.sent[1]
    # CSeq must increment between attempts
    assert b"CSeq: 1 REGISTER" in transport.sent[0]
    assert b"CSeq: 2 REGISTER" in transport.sent[1]


@pytest.mark.asyncio
async def test_register_returns_false_on_non_401_non_200() -> None:
    transport = FakeTransport(script=[
        (
            b"SIP/2.0 403 Forbidden\r\n"
            b"CSeq: 1 REGISTER\r\n"
            b"Content-Length: 0\r\n\r\n"
        ),
    ])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport,
    )
    ok = await client.register_once()
    assert ok is False


@pytest.mark.asyncio
async def test_invite_fires_callback_and_does_not_respond() -> None:
    """The silent-mode contract: INVITE → callback yes, SIP wire response NO."""
    invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-inv\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: ring-1\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport = FakeTransport(script=[invite])
    received: list[dict[str, str]] = []

    async def on_invite(info: dict[str, str]) -> None:
        received.append(info)

    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=on_invite,
    )
    await client.process_one_message()
    assert len(received) == 1
    assert received[0]["call_id"] == "ring-1"
    # Nothing sent — silent mode
    assert transport.sent == []


@pytest.mark.asyncio
async def test_cancel_replies_with_200_and_487() -> None:
    """CANCEL for a known call-id → 200 to the CANCEL + 487 to original INVITE."""
    invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-inv2\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: ring-2\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    cancel = (
        "CANCEL sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-cnc\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: ring-2\r\n"
        "CSeq: 1 CANCEL\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport = FakeTransport(script=[invite, cancel])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=lambda _info: None,
    )
    await client.process_one_message()  # INVITE
    await client.process_one_message()  # CANCEL
    statuses = [s.split(b"\r\n", 1)[0] for s in transport.sent]
    assert b"SIP/2.0 200 OK" in statuses
    assert b"SIP/2.0 487 Request Terminated" in statuses


@pytest.mark.asyncio
async def test_options_replies_with_200_ok() -> None:
    """Cloud's OPTIONS health-checks must get 200 OK to keep us alive."""
    options = (
        "OPTIONS sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-opt\r\n"
        "From: <sip:srv@srv>;tag=srv-tag\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: opt-7\r\n"
        "CSeq: 7 OPTIONS\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport = FakeTransport(script=[options])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport,
    )
    await client.process_one_message()
    assert len(transport.sent) == 1
    assert transport.sent[0].startswith(b"SIP/2.0 200 OK\r\n")


@pytest.mark.asyncio
async def test_callback_can_be_sync_function() -> None:
    """on_invite may be a plain `def` (not async) for ergonomic call sites."""
    invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-inv3\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: ring-3\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport = FakeTransport(script=[invite])
    received: list[dict[str, str]] = []
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=received.append,
    )
    await client.process_one_message()
    assert received == [{
        "call_id": "ring-3",
        "from_sip": "<sip:doorbell@srv>;tag=db",
        "to_sip": "<sip:alice@srv>",
    }]


@pytest.mark.asyncio
async def test_register_does_not_match_cseq_register_in_allow_header() -> None:
    """A 200 OK whose CSeq is NOT a REGISTER but whose body mentions
    'REGISTER' (e.g. in an Allow header) must not be misread as a
    successful REGISTER response.
    """
    spurious_ok = (
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/TLS 192.0.2.42:55555;branch=z9hG4bK-x\r\n"
        b"CSeq: 9 OPTIONS\r\n"
        b"Allow: INVITE, ACK, CANCEL, REGISTER, OPTIONS\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    transport = FakeTransport(script=[spurious_ok])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport,
    )
    ok = await client.register_once(timeout_s=0.3)
    # 200 OK that's NOT a REGISTER response → must NOT count as success
    assert ok is False


@pytest.mark.asyncio
async def test_run_raises_when_periodic_reregister_fails() -> None:
    """If the second/third REGISTER (re-register) fails, run() must NOT
    silently keep listening — it must raise so the coordinator's
    reconnect-with-backoff loop notices and `cloud_sip_connected` flips
    to False.
    """
    # Script: initial REGISTER succeeds (200), then immediately Cloud
    # sends a 403 to a "re-register" attempt. We force re-register by
    # setting REREGISTER_INTERVAL to 0 via monkeypatching the module.
    initial_200 = _200_ok_for("REGISTER")
    forbidden = (
        b"SIP/2.0 403 Forbidden\r\n"
        b"CSeq: 2 REGISTER\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    transport = FakeTransport(script=[initial_200, forbidden])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport,
    )
    # Patch module constant for this test
    orig = sip.REREGISTER_INTERVAL
    sip.REREGISTER_INTERVAL = 0  # re-register on every loop iteration
    try:
        with pytest.raises(RuntimeError, match="REGISTER"):
            await asyncio.wait_for(client.run(), timeout=1.0)
    finally:
        sip.REREGISTER_INTERVAL = orig


@pytest.mark.asyncio
async def test_invite_without_call_id_is_ignored() -> None:
    """A malformed INVITE missing the Call-ID header must NOT collide
    on the empty-string dict key — without the guard, the second
    malformed INVITE would be silently dropped by the retransmit
    suppressor instead of firing on_invite for distinct calls.
    """
    bad_invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-bad\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport = FakeTransport(script=[bad_invite])
    received: list[dict[str, str]] = []
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=received.append,
    )
    await client.process_one_message()
    # Callback NOT fired, table stays empty, no SIP response sent
    assert received == []
    assert client._active_invites == {}  # noqa: SLF001
    assert transport.sent == []


@pytest.mark.asyncio
async def test_invite_retransmit_does_not_refire_or_replace_tag() -> None:
    """If Cloud retransmits the same INVITE (same Call-ID), we must NOT
    fire `on_invite` again and must NOT replace the dialog's local-tag —
    a subsequent CANCEL carries the original tag, and our 487 has to
    match the dialog the caller actually saw.
    """
    invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-rt\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: retx-1\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    transport = FakeTransport(script=[invite, invite])  # delivered twice
    received: list[dict[str, str]] = []
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=received.append,
    )
    await client.process_one_message()  # first INVITE
    first_tag = client._active_invites["retx-1"][1]  # noqa: SLF001
    await client.process_one_message()  # retransmit
    second_tag = client._active_invites["retx-1"][1]  # noqa: SLF001
    # Callback fires exactly once
    assert len(received) == 1
    # Local tag stays stable across retransmits
    assert first_tag == second_tag
    # Silent-mode still respected — no responses sent
    assert transport.sent == []


@pytest.mark.asyncio
async def test_iphone_accept_does_not_leak_invite_entry() -> None:
    """When iPhone accepts a ring, Cloud doesn't send CANCEL to us. Our
    `_active_invites` would otherwise grow unbounded over months. The
    client must drop stale entries by age so memory stays flat.
    """
    invite1 = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-1\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: leak-1\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    invite2 = invite1.replace(b"Call-ID: leak-1", b"Call-ID: leak-2")
    transport = FakeTransport(script=[invite1, invite2])
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=lambda _info: None,
        active_invite_ttl_s=10.0,  # short for testing
    )
    # Process first INVITE at t=100 (we inject the clock via _now_fn)
    now = [100.0]
    client._now_fn = lambda: now[0]  # noqa: SLF001 — explicit test injection
    await client.process_one_message()
    assert "leak-1" in client._active_invites  # noqa: SLF001
    # Jump past TTL and process second INVITE — old entry must be evicted
    now[0] = 200.0
    await client.process_one_message()
    assert "leak-1" not in client._active_invites  # noqa: SLF001
    assert "leak-2" in client._active_invites      # noqa: SLF001


@pytest.mark.asyncio
async def test_partial_message_blocks_until_complete() -> None:
    """If the wire only delivers half a message, the client waits for more."""
    invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 192.0.2.99:5061;branch=z9hG4bK-inv4\r\n"
        "From: <sip:doorbell@srv>;tag=db\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: ring-4\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode()
    head, tail = invite[:80], invite[80:]
    transport = FakeTransport(script=[head, tail])
    received: list[dict[str, str]] = []
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_invite=received.append,
    )
    # First call only sees the head — must not fire yet
    await client.process_one_message()
    assert received == []
    # Second call sees the tail — completes the message
    await client.process_one_message()
    assert received == [{
        "call_id": "ring-4",
        "from_sip": "<sip:doorbell@srv>;tag=db",
        "to_sip": "<sip:alice@srv>",
    }]


@pytest.mark.asyncio
async def test_register_failure_records_last_register_error() -> None:
    """A rejected REGISTER stores a human-readable reason for the WARNING log."""
    transport = FakeTransport(script=[
        b"SIP/2.0 403 Forbidden\r\nCSeq: 1 REGISTER\r\nContent-Length: 0\r\n\r\n",
    ])
    client = sip.SipClient(
        server="srv", user="alice", password="secret", transport=transport,
    )
    assert await client.register_once() is False
    assert client.last_register_error is not None
    assert "403" in client.last_register_error


@pytest.mark.asyncio
async def test_run_fires_on_registered_once_on_success() -> None:
    """run() registers exactly once (no double-REGISTER) and fires the callback.

    The pre-fix coordinator registered in _sip_loop *and* again in run(); the
    Cloud rejected the duplicate. run() is now the single REGISTER path and
    signals success via on_registered so the coordinator can flip the sensor
    and reset its backoff only on a genuine registration.
    """
    transport = FakeTransport(script=[_401_response(), _200_ok_for("REGISTER")])
    calls: list[int] = []
    client = sip.SipClient(
        server="srv", user="alice", password="secret",
        transport=transport, on_registered=lambda: calls.append(1),
    )
    try:
        await asyncio.wait_for(client.run(), timeout=0.3)
    except asyncio.TimeoutError:
        pass
    assert calls == [1]
    # Exactly one REGISTER cycle: unauth + digest-auth retry = 2 sends.
    assert len(transport.sent) == 2
