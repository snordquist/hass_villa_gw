"""INVITE response strategies — pluggable SIP behaviour on an incoming ring.

Ring-detection (the client's `on_invite` callback) fires independently; a
strategy decides only the SIP *response* and any media handling. New
experiments — e.g. a future `Answer200Strategy` — drop in as new classes
without touching the `SipClient` state machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .sip_client import SipClient


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
