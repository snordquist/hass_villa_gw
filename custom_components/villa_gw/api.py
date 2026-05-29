"""Villa GW low-level client.

`VillaGwClient` composes four transport-specific mixins, each in its own
flat module, so the surfaces stay small and independently testable:

- `gw_web.VillaGwWebMixin` — HTTP REST via the web admin
  (`http://<gw>/api/*`): md5 login, `/api/sip`, `/api/video`, `/api/device`,
  `/api/mac`, `/api/getCallList`, `rtsp_url`, `cloud_link_online`.
- `gw_avlink.VillaGwAvlinkMixin` — read-only `AT+B` queries on
  `avlink:10086` (`APPLICATION`, `SYSTEM`, `CHECKSIP …`) for the polling
  event detector.
- `gw_bus.VillaGwBusMixin` — fire-and-forget bus commands on
  `uart2d:10087` (wake/live-view, unlock, hook/hang, intercom, switch
  camera).
- `gw_logtail.VillaGwLogTailMixin` — optional telnet `:23` `tail -F` log
  stream + the `LOG_PATTERN_*` line parser.

Shared connection state and the two exception types live in `gw_base`.
The exceptions are re-exported here so existing callers keep importing
them from `.api`.
"""

from __future__ import annotations

from .gw_avlink import VillaGwAvlinkMixin
from .gw_base import VillaGwAuthError, VillaGwBase, VillaGwConnectionError
from .gw_bus import VillaGwBusMixin
from .gw_logtail import VillaGwLogTailMixin
from .gw_web import VillaGwWebMixin

__all__ = ["VillaGwAuthError", "VillaGwClient", "VillaGwConnectionError"]


class VillaGwClient(
    VillaGwWebMixin,
    VillaGwAvlinkMixin,
    VillaGwBusMixin,
    VillaGwLogTailMixin,
    VillaGwBase,
):
    """High-level interface to one Villa GW.

    Composed from transport-specific mixins; the public method surface
    (login, mac, application, wake_live_view, stream_log_events, …) is
    unchanged from the previous monolithic implementation.
    """
