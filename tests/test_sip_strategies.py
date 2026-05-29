"""Unit tests for the INVITE response strategies (sip_strategies.py)."""

from __future__ import annotations

import importlib.util
import sys
import types
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


pkg = types.ModuleType("villa_gw_test_strat")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_strat"] = pkg
_load_module("villa_gw_test_strat.sip_messages", PKG / "sip_messages.py")
strat = _load_module("villa_gw_test_strat.sip_strategies", PKG / "sip_strategies.py")


class _FakeTransport:
    local_ip = "192.0.2.10"
    local_port = 55060

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


class _FakeClient:
    def __init__(self) -> None:
        self._t = _FakeTransport()

    @property
    def transport(self) -> _FakeTransport:
        return self._t

    @property
    def user(self) -> str:
        return "sipuser"


_INVITE = (
    "INVITE sip:sipuser@192.0.2.10:55060;transport=tls SIP/2.0\r\n"
    "Via: SIP/2.0/TLS 198.51.100.5:5061;branch=z9hG4bK-x\r\n"
    "From: <sip:gw@srv>;tag=t\r\n"
    "To: <sip:me@srv>\r\n"
    "Call-ID: cid-1\r\n"
    "CSeq: 102 INVITE\r\n"
    "Content-Type: application/sdp\r\n\r\n"
    "v=0\r\nc=IN IP4 198.51.100.5\r\nm=audio 10128 RTP/AVP 0 8\r\n"
)


@pytest.mark.asyncio
async def test_silent_strategy_sends_nothing() -> None:
    client = _FakeClient()
    await strat.SilentStrategy().respond(client, _INVITE, "hass-tag")
    assert client.transport.sent == []


@pytest.mark.asyncio
async def test_ringing_strategy_sends_100_then_180() -> None:
    client = _FakeClient()
    await strat.RingingStrategy().respond(client, _INVITE, "hass-tag")
    sent = [b.decode() for b in client.transport.sent]
    assert len(sent) == 2
    assert sent[0].startswith("SIP/2.0 100 Trying")
    assert sent[1].startswith("SIP/2.0 180 Ringing")

    # 100 Trying is NOT dialog-establishing → MUST NOT carry a To-tag.
    trying_to = next(
        ln for ln in sent[0].split("\r\n") if ln.lower().startswith("to:")
    )
    assert ";tag=" not in trying_to

    # 180 Ringing establishes the early dialog → carries the To-tag, and it
    # MUST be the same local_tag the later 487-on-CANCEL reuses.
    ringing_to = next(
        ln for ln in sent[1].split("\r\n") if ln.lower().startswith("to:")
    )
    assert ";tag=hass-tag" in ringing_to

    # Both responses echo the request's Call-ID / CSeq (transaction match).
    for resp in sent:
        assert "Call-ID: cid-1" in resp
        assert "CSeq: 102 INVITE" in resp
