"""Cloud SIP listener for the Villa GW coordinator.

Maintains a TLS SIP-REGISTER session with the iLifestyle Cloud as a 2nd
App-User and receives forked SIP-INVITEs on doorbell rings. Silent-mode
only: never answers an INVITE (no 200 OK) so the parallel iPhone-fork is
untouched. Mixed into `VillaGwCoordinator`; relies on shared state
(`cloud_sip_connected`, `_fire`).
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import callback

from ._backoff import Backoff
from .const import (
    BACKOFF_FACTOR,
    BACKOFF_INITIAL_S,
    BACKOFF_JITTER,
    BACKOFF_MAX_S,
    EVENT_DOORBELL_RINGING,
)

_LOGGER = logging.getLogger(__name__)


class VillaGwSipMixin:
    """Cloud SIP REGISTER listener + INVITE → ring-event bridge."""

    async def _sip_loop(
        self, *, server: str, user: str, password: str,
    ) -> None:
        """Maintain a TLS SIP-REGISTER session with the iLifestyle Cloud.

        Reconnects with capped exponential backoff on any disconnect or
        REGISTER failure. While registered, INVITE-on-the-wire fires
        `EVENT_DOORBELL_RINGING` via the same `_fire()` dedup as poll/log.
        """
        # Lazy import so test environments without the sip_client module
        # don't hard-fail at coordinator-import time.
        from .sip_client import SipClient, TlsSipTransport  # noqa: PLC0415

        bo = Backoff(
            initial=BACKOFF_INITIAL_S, factor=BACKOFF_FACTOR,
            cap=BACKOFF_MAX_S, jitter=BACKOFF_JITTER,
        )

        def _on_registered() -> None:
            # Fired by run() after a genuine (re-)REGISTER succeeds. Flip the
            # sensor and reset the backoff only *now* — never optimistically.
            # A rejected listener therefore escalates its retry interval
            # (2s → … → cap) instead of hammering the Cloud every
            # BACKOFF_INITIAL_S (which previously got us close to a block).
            if not self.cloud_sip_connected:
                _LOGGER.info("Villa GW SIP-listener registered at %s", server)
            self.cloud_sip_connected = True
            bo.reset()
            self.async_set_updated_data(self.data or {})

        while True:
            transport: TlsSipTransport | None = None
            try:
                transport = await TlsSipTransport.connect(server)
                # run() is the SINGLE REGISTER path. The old code registered
                # here *and* again inside run(); the Cloud rejected the
                # duplicate REGISTER ("failed (initial)"), so the listener
                # never stayed up and the backoff kept resetting.
                client = SipClient(
                    server=server, user=user, password=password,
                    transport=transport, on_invite=self._on_sip_invite,
                    on_registered=_on_registered,
                )
                await client.run()  # registers → fires callback → loops until error
            except asyncio.CancelledError:
                self.cloud_sip_connected = False
                raise
            except Exception as err:  # noqa: BLE001
                self.cloud_sip_connected = False
                self.async_set_updated_data(self.data or {})
                delay = bo.next_delay()
                _LOGGER.warning(
                    "SIP listener disconnected (%s) — reconnecting in %.0fs",
                    err, delay,
                )
                await asyncio.sleep(delay)
            finally:
                if transport is not None:
                    try:
                        await transport.close()
                    except Exception:  # noqa: BLE001
                        pass

    @callback
    def _on_sip_invite(self, info: dict[str, str]) -> None:
        """Callback from SipClient on each incoming INVITE.

        Fires the canonical ring event with `source="sip"`. The `_fire()`
        dedup handles the case where poll/log already saw the same ring
        within DEDUP_WINDOW.
        """
        self._fire(
            EVENT_DOORBELL_RINGING,
            {
                "source":   "sip",
                "call_id":  info.get("call_id", ""),
                "from_sip": info.get("from_sip", ""),
                "to_sip":   info.get("to_sip", ""),
            },
        )
