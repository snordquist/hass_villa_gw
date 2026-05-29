"""Shared base + exceptions for the Villa GW client mixins.

The client surface is split across transport-specific mixins
(`gw_web`, `gw_avlink`, `gw_bus`, `gw_logtail`) that are composed into
`VillaGwClient` in `api.py`. They all share the same connection state
(host / web creds / session / cached token) defined here, plus the two
exception types every transport raises.
"""

from __future__ import annotations

import asyncio

import aiohttp


class VillaGwAuthError(Exception):
    """Web admin authentication failed."""


class VillaGwConnectionError(Exception):
    """Network-level error reaching the GW."""


class VillaGwBase:
    """Shared connection state for all Villa GW client mixins."""

    def __init__(
        self,
        host: str,
        web_username: str,
        web_password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._host = host
        self._user = web_username
        self._pw = web_password
        self._session = session
        self._token: str | None = None
        # Serialize login() so a 401-storm doesn't fire N parallel logins
        # that overwrite each other's tokens. Acquired only around the
        # actual POST /api/login call, not for every request.
        self._login_lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────── meta

    @property
    def host(self) -> str:
        return self._host
