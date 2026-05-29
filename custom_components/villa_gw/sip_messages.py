"""Pure SIP message helpers — building/parsing, no I/O, no HA imports.

Extracted from `sip_client.py` so the wire-format logic is independently
testable and reusable by the SIP client, transport and strategies. Everything
here is a pure function of its inputs (plus `secrets`/`hashlib` randomness).
"""

from __future__ import annotations

import hashlib
import re
import secrets

USER_AGENT = "HA-Villa-SIP/0.1"


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
