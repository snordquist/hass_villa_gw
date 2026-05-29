"""Unit tests for the INVITE response strategies (sip_strategies.py)."""

from __future__ import annotations

import asyncio
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


def test_build_183_offers_pcmu_recvonly_sdp() -> None:
    client = _FakeClient()
    msg = strat.EarlyMedia183Strategy()._build_183(client, _INVITE, "hass-tag").decode()
    assert msg.startswith("SIP/2.0 183 Session Progress")
    assert "Content-Type: application/sdp" in msg
    assert "m=audio 40000 RTP/AVP 0 101" in msg
    assert "a=rtpmap:0 PCMU/8000" in msg
    assert "a=recvonly" in msg
    # To-tag added, Call-ID/CSeq echoed from the request
    assert ";tag=hass-tag" in msg
    assert "Call-ID: cid-1" in msg
    assert "CSeq: 102 INVITE" in msg


@pytest.mark.asyncio
async def test_early_media_probe_reports_when_no_rtp() -> None:
    """With a tiny listen window and no incoming RTP, respond() reports 'no RTP'."""
    client = _FakeClient()
    results: list[str] = []
    s = strat.EarlyMedia183Strategy(on_result=results.append, listen_s=0.05)
    await asyncio.wait_for(s.respond(client, _INVITE, "hass-tag"), timeout=5.0)
    assert any(b"183 Session Progress" in b for b in client.transport.sent)
    assert len(results) == 1
    assert "KEIN early-media RTP" in results[0]
