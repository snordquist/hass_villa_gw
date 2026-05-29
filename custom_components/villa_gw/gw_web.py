"""Web admin REST surface for the Villa GW client.

HTTP REST via the web admin (`http://<gw>/api/*`) for authenticated
queries: md5 login, `/api/sip`, `/api/video`, `/api/device`, `/api/mac`,
`/api/getCallList`, plus the derived `rtsp_url` and `cloud_link_online`.
"""

from __future__ import annotations

import hashlib

import aiohttp

from .const import PORT_WEB
from .gw_base import VillaGwAuthError, VillaGwConnectionError


class VillaGwWebMixin:
    """REST/web-admin methods. Mixed into VillaGwClient."""

    async def login(self) -> str:
        """POST /api/login → JWT. Cached.

        Serialized via _login_lock so concurrent callers don't fire
        parallel logins. If a second caller waits on the lock and another
        coroutine already refreshed the token, we just return the cached
        value without re-hitting the server.
        """
        async with self._login_lock:
            if self._token:
                return self._token
            url = f"http://{self._host}:{PORT_WEB}/api/login"
            # FW 4.1.12+ expects the client to MD5-hash the password (the
            # web UI's JS does the same). 4.1.11 expected cleartext, but
            # 4.1.12 normalises both sides so MD5 works regardless of
            # whether the DB stores cleartext or a hash.
            pw_hash = hashlib.md5(self._pw.encode()).hexdigest()
            body = {"name": self._user, "password": pw_hash}
            try:
                async with self._session.post(url, json=body, timeout=10) as resp:
                    data = await resp.json(content_type=None)
            except aiohttp.ClientError as err:
                raise VillaGwConnectionError(f"login: {err}") from err
            if data.get("status") != 0 or not data.get("token"):
                raise VillaGwAuthError(f"Login rejected: {data}")
            self._token = data["token"]
            return self._token

    async def _get_json(self, path: str) -> dict:
        if not self._token:
            await self.login()
        url = f"http://{self._host}:{PORT_WEB}{path}"
        headers = {"Cookie": f"token={self._token}"}
        try:
            async with self._session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 401:
                    # Atomic refresh: invalidate the token we just tried
                    # (not whatever a concurrent coroutine may have written
                    # since), then re-login under the lock so we don't fire
                    # parallel login requests on a 401-storm.
                    stale = self._token
                    async with self._login_lock:
                        if self._token == stale:
                            self._token = None
                    await self.login()
                    headers["Cookie"] = f"token={self._token}"
                    async with self._session.get(url, headers=headers, timeout=10) as r2:
                        return await r2.json(content_type=None)
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise VillaGwConnectionError(f"GET {path}: {err}") from err

    async def mac(self) -> str:
        data = await self._get_json("/api/mac")
        return data.get("mac", "").upper().replace(":", "")

    async def video_config(self) -> dict:
        return await self._get_json("/api/video")

    async def device_info(self) -> dict:
        return await self._get_json("/api/device")

    async def call_list(self) -> list[dict]:
        url = f"http://{self._host}:{PORT_WEB}/api/getCallList"
        if not self._token:
            await self.login()
        headers = {"Cookie": f"token={self._token}"}
        try:
            async with self._session.post(
                url, headers=headers, json={"page": 1, "perPage": 50}, timeout=10
            ) as resp:
                data = await resp.json(content_type=None)
                return data.get("ret", []) if data.get("status") == 0 else []
        except aiohttp.ClientError as err:
            raise VillaGwConnectionError(f"call_list: {err}") from err

    async def rtsp_url(self) -> str:
        return f"rtsp://{self._user}:{self._pw}@{self._host}/live.sdp"

    async def cloud_link_online(self) -> bool:
        """GW↔Cloud backend link status, read authoritatively via the web API.

        ``GET /api/sip`` returns ``{"online": <bool>, ...}`` reflecting the
        GW's own SIP/cloud registration with ``de.ilifestyle-cloud.com``.

        Unlike the edge-triggered ``mqtt connect ok`` log line — which only
        appears on a (re)connect and is therefore missed across an HA restart
        (``tail -F`` only replays the last few lines) — this is *pollable*.
        It lets ``binary_sensor.cloud_online`` self-heal to the true state
        instead of staying falsely ``off`` until the next reconnect happens
        to be logged.
        """
        data = await self._get_json("/api/sip")
        return bool(data.get("online"))
