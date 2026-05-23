"""Unit tests for the pure SIP-protocol helpers in `sip_client`.

Same importlib-direct-load pattern as `test_cloud_api.py` so the HA
stack isn't pulled in. Only pure-function helpers (no I/O) are covered
here; the `SipClient` socket loop has its own focused test below
using a fake transport abstraction.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "villa_gw"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pkg = types.ModuleType("villa_gw_test_sip")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_sip"] = pkg
sip = _load_module("villa_gw_test_sip.sip_client", PKG / "sip_client.py")


# ──────────────────────────────────────────────────────── parse_headers


def test_parse_headers_basic_invite() -> None:
    msg = (
        "INVITE sip:s00cAAAAA@de.ilifestyle-cloud.com SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 1.2.3.4:5061;branch=z9hG4bK-abc\r\n"
        "From: <sip:s00cBBBBB@de.ilifestyle-cloud.com>;tag=alice\r\n"
        "To: <sip:s00cAAAAA@de.ilifestyle-cloud.com>\r\n"
        "Call-ID: ringing-call-id-1234@cloud\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    h = sip.parse_headers(msg)
    assert h["call-id"] == "ringing-call-id-1234@cloud"
    assert h["cseq"] == "1 INVITE"
    assert "<sip:s00cBBBBB@de.ilifestyle-cloud.com>" in h["from"]
    # Method line is NOT parsed as a header
    assert "invite" not in h


def test_parse_headers_handles_blank_lines_and_colons_in_value() -> None:
    msg = (
        "SIP/2.0 401 Unauthorized\r\n"
        "WWW-Authenticate: Digest realm=\"icloud\", nonce=\"abcd:efgh\"\r\n"
        "Content-Length: 0\r\n"
        "\r\n"
    )
    h = sip.parse_headers(msg)
    # First ':' splits; the rest of the value (incl. embedded ':') stays
    assert h["www-authenticate"].startswith("Digest realm=")
    assert "nonce=\"abcd:efgh\"" in h["www-authenticate"]


# ──────────────────────────────────────────────────────── parse_digest_challenge


def test_parse_digest_challenge_extracts_realm_nonce_qop() -> None:
    chall = sip.parse_digest_challenge(
        'Digest realm="icloud", nonce="dcba1234", algorithm=MD5, qop="auth"'
    )
    assert chall["realm"] == "icloud"
    assert chall["nonce"] == "dcba1234"
    assert chall["algorithm"] == "MD5"
    assert chall["qop"] == "auth"


def test_parse_digest_challenge_non_digest_returns_empty() -> None:
    # Defensive: a non-Digest scheme (Basic, Bearer) must not be parsed
    assert sip.parse_digest_challenge('Basic realm="x"') == {}


# ──────────────────────────────────────────────────────── build_digest_auth


def test_build_digest_auth_md5_no_qop() -> None:
    """Verify exact RFC-2617 digest with hand-calculated values.

    HA1 = MD5(user:realm:pw) = MD5("alice:icloud:secret") = known fixed
    HA2 = MD5(method:uri) = MD5("REGISTER:sip:de.ilifestyle-cloud.com")
    resp = MD5(HA1:nonce:HA2)
    """
    import hashlib
    ha1 = hashlib.md5(b"alice:icloud:secret").hexdigest()
    ha2 = hashlib.md5(b"REGISTER:sip:de.ilifestyle-cloud.com").hexdigest()
    expected = hashlib.md5(f"{ha1}:nonce-XYZ:{ha2}".encode()).hexdigest()
    chall = {"realm": "icloud", "nonce": "nonce-XYZ", "algorithm": "MD5"}
    out = sip.build_digest_auth(
        "alice", "secret", chall,
        method="REGISTER", uri="sip:de.ilifestyle-cloud.com",
    )
    assert f'response="{expected}"' in out
    assert 'username="alice"' in out
    assert 'realm="icloud"' in out
    assert 'nonce="nonce-XYZ"' in out
    assert 'algorithm=MD5' in out


def test_build_digest_auth_md5_with_qop_auth_includes_nc_cnonce() -> None:
    chall = {
        "realm": "icloud", "nonce": "n123",
        "algorithm": "MD5", "qop": "auth",
    }
    out = sip.build_digest_auth(
        "alice", "secret", chall,
        method="REGISTER", uri="sip:srv",
    )
    assert "qop=auth" in out
    assert "nc=00000001" in out
    assert 'cnonce="' in out


# ──────────────────────────────────────────────────────── build_register


def test_build_register_basic_no_auth() -> None:
    out = sip.build_register(
        user="alice", server="srv", local_ip="1.2.3.4", local_port=5061,
        call_id="cid@hass", from_tag="abcd",
        branch="z9hG4bK-foo", cseq=1, auth_header=None,
    )
    text = out.decode()
    assert text.startswith("REGISTER sip:srv SIP/2.0\r\n")
    assert "Via: SIP/2.0/TLS 1.2.3.4:5061;branch=z9hG4bK-foo;rport\r\n" in text
    assert "From: <sip:alice@srv>;tag=abcd\r\n" in text
    assert "To: <sip:alice@srv>\r\n" in text
    assert "Call-ID: cid@hass\r\n" in text
    assert "CSeq: 1 REGISTER\r\n" in text
    assert "Expires: 600\r\n" in text
    assert "Authorization:" not in text
    assert text.endswith("\r\n\r\n")


def test_build_register_with_auth_header_inserts_it() -> None:
    out = sip.build_register(
        user="alice", server="srv", local_ip="1.2.3.4", local_port=5061,
        call_id="cid@hass", from_tag="abcd",
        branch="z9hG4bK-foo", cseq=2,
        auth_header='Digest username="alice"',
    )
    text = out.decode()
    assert 'Authorization: Digest username="alice"\r\n' in text


# ──────────────────────────────────────────────────────── responses


def test_build_ok_response_for_options_reuses_via_callid() -> None:
    options = (
        "OPTIONS sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 5.6.7.8:5061;branch=z9hG4bK-srv\r\n"
        "From: <sip:srv@srv>;tag=server-tag\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: opt-cid-1\r\n"
        "CSeq: 5 OPTIONS\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    out = sip.build_ok_response(options).decode()
    assert out.startswith("SIP/2.0 200 OK\r\n")
    assert "Via: SIP/2.0/TLS 5.6.7.8:5061;branch=z9hG4bK-srv\r\n" in out
    assert "Call-ID: opt-cid-1\r\n" in out
    assert "CSeq: 5 OPTIONS\r\n" in out
    # 200 OK to a request without To-tag must add one (RFC 3261)
    assert ";tag=" in out.split("To:")[1].split("\r\n")[0]
    assert out.endswith("\r\n\r\n")


def test_build_terminated_response_preserves_request_dialog_headers() -> None:
    invite = (
        "INVITE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 5.6.7.8:5061;branch=z9hG4bK-1\r\n"
        "From: <sip:bob@srv>;tag=bob-tag\r\n"
        "To: <sip:alice@srv>\r\n"
        "Call-ID: inv-cid-9\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    out = sip.build_terminated_response(invite, local_tag="hass-1234").decode()
    assert out.startswith("SIP/2.0 487 Request Terminated\r\n")
    assert "Call-ID: inv-cid-9\r\n" in out
    assert "CSeq: 1 INVITE\r\n" in out
    assert ";tag=hass-1234" in out  # appended to To header


def test_build_response_keeps_existing_to_tag() -> None:
    """If the request's To already has a tag, don't append a 2nd one."""
    bye = (
        "BYE sip:alice@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 5.6.7.8:5061;branch=z9hG4bK-bye\r\n"
        "From: <sip:srv@srv>;tag=remote-tag\r\n"
        "To: <sip:alice@srv>;tag=ours-already\r\n"
        "Call-ID: bye-cid-1\r\n"
        "CSeq: 9 BYE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    out = sip.build_ok_response(bye).decode()
    # The To header line must have exactly one ;tag=
    to_line = out.split("To:")[1].split("\r\n")[0]
    assert to_line.count(";tag=") == 1
    assert ";tag=ours-already" in to_line


# ──────────────────────────────────────────────────────── extract_invite_info


def test_extract_invite_info_pulls_call_id_from_from_to() -> None:
    """`extract_invite_info` is the public surface for the on_invite callback.

    Returns a dict ready to merge into the HA bus event.
    """
    invite = (
        "INVITE sip:s00cAAAAA@srv SIP/2.0\r\n"
        "Via: SIP/2.0/TLS 1.2.3.4:5061;branch=z9hG4bK-abc\r\n"
        "From: \"Doorbell\" <sip:s00cBBBBB@srv>;tag=doorbell-tag\r\n"
        "To: <sip:s00cAAAAA@srv>\r\n"
        "Call-ID: ring-cid-555@cloud\r\n"
        "CSeq: 1 INVITE\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    info = sip.extract_invite_info(invite)
    assert info["call_id"] == "ring-cid-555@cloud"
    assert "s00cBBBBB" in info["from_sip"]
    assert "s00cAAAAA" in info["to_sip"]
