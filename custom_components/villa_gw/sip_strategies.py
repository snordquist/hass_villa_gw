"""INVITE response strategies — pluggable SIP behaviour on an incoming ring.

Ring-detection (the client's `on_invite` callback) fires independently; a
strategy decides only the SIP *response* and any media handling. New
experiments — e.g. a future `Answer200Strategy` — drop in as new classes
without touching the `SipClient` state machine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .sip_messages import build_ringing_response, build_trying_response

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


class RingingStrategy:
    """Behave like a real, never-answered phone: `100 Trying` + `180 Ringing`,
    then NEVER `200` (we don't answer). The matching `487` on CANCEL is emitted
    by `SipClient` with the *same* `local_tag`, so the dialog stays consistent.

    Why this might fix the answer-breakage that pure silence causes:
    a fully-silent UAS gives the Cloud B2BUA no live transaction on our branch;
    when the iPhone answers and the Cloud CANCELs us, the B2BUA mishandles our
    out-of-the-blue 487 and tears down the GW's call leg (observed:
    `AT_UART_MONITOR err` → `Request Terminated`). A real phone first establishes
    transaction state via `100 Trying` — pjproject does exactly this in
    `pjsua_call.c` (`pjsip_inv_initial_answer(..., 100, ...)`) *before* the app
    callback. Multi-device ringing is a supported Cloud scenario, so a
    correctly-ringing 2nd endpoint should coexist with the iPhone.

    EXPERIMENTAL — opt-in via `enable_sip_ringing`. Default stays SilentStrategy.
    """

    async def respond(
        self, client: "SipClient", invite_msg: str, local_tag: str,
    ) -> None:
        await client.transport.send(build_trying_response(invite_msg))
        await client.transport.send(
            build_ringing_response(invite_msg, local_tag=local_tag),
        )
