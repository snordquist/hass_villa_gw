"""Cloud-API client for `de.ilifestyle-cloud.com`.

Builds on the reverse-engineered REST surface (see `docs/api-cloud.md` and
`villa_gw/cloud_fcm/findings/01_rest_api_surface.md`). Talks to the cloud
as an App-User (device_type=3, device_model="Android") so the server
issues us our own SIP-id+password and routes ring-INVITEs to us in
parallel with the official iLifestyle phone app.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import aiohttp


CLOUD_BASE = "https://de.ilifestyle-cloud.com/api"
DEVICE_TYPE_ANDROID = 3
DEVICE_MODEL_ANDROID = "Android"


class CloudAuthError(Exception):
    """Cloud rejected the credentials."""


class CloudConnectionError(Exception):
    """Network-level error reaching the cloud."""


@dataclass(frozen=True)
class LoginResult:
    """Successful `/api/v2/login` response payload (excerpt)."""

    token: str
    uid: str
    city_id: str


class BindResult(Enum):
    """Outcome of `POST /api/device {"code": ...}`.

    `BOUND` and `ALREADY_BOUND` are both success states — the cloud will
    route SIP-INVITEs to us regardless. The distinction is informational
    (e.g. for diagnostics).
    """

    BOUND = "bound"
    ALREADY_BOUND = "already_bound"


@dataclass(frozen=True)
class DeviceInfo:
    """Subset of `/api/device?id=<dev>` response we care about.

    The cloud auto-issues `sip_id` + `password` when a device_record is
    first created via `/api/v2/login`. These let us register at the
    iLifestyle Cloud SIP-server as a 2nd App-user endpoint and receive
    forked SIP-INVITEs on doorbell rings.
    """

    device_id: str
    sip_id: str
    sip_password: str
    sip_server: str
    video_url: str | None = None


class CloudApiClient:
    """Async client for the iLifestyle Cloud-API.

    Use `login()` first to obtain an app_JWT, then pass it via the
    `token` arg to authenticated calls.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def login(
        self, *, email: str, password: str, device_id: str,
    ) -> LoginResult:
        """POST /api/v2/login — exchange account creds for an app_JWT.

        Cloud creates a device_record under our user-account for `device_id`
        if it didn't already exist, and auto-issues a sip_id+password for it.
        """
        body = {
            "user_id":      email,
            "password":     password,
            "device_id":    device_id,
            "device_type":  DEVICE_TYPE_ANDROID,
            "device_model": DEVICE_MODEL_ANDROID,
            "app":          1,
            "type":         1,
        }
        try:
            async with self._session.post(f"{CLOUD_BASE}/v2/login", json=body) as resp:
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(str(err)) from err
        if data.get("code") != 0:
            raise CloudAuthError(f"login rejected: {data}")
        try:
            return LoginResult(
                token=data["token"],
                uid=data["id"],
                city_id=data["city_id"],
            )
        except KeyError as err:
            raise CloudAuthError(f"malformed login response: {data}") from err

    async def get_device_info(
        self, *, device_id: str, token: str,
    ) -> DeviceInfo:
        """GET /api/device?id=<device_id> — read our SIP-credentials.

        Auth is `Cookie: token=<app_JWT>` (App-style auth, not the
        `Authorization`-header used for device-bootstrap JWTs).
        """
        # Use aiohttp's `params=` so device_id is URL-encoded — interpolating
        # it raw into the query string would let '&'/'#'/'=' inject or
        # truncate the value.
        url = f"{CLOUD_BASE}/device"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self._session.get(
                url, params={"id": device_id}, headers=headers,
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(str(err)) from err
        if data.get("code") != 0:
            raise CloudAuthError(f"device info rejected: {data}")
        try:
            return DeviceInfo(
                device_id=data["id"],
                sip_id=data["sip_id"],
                sip_password=data["password"],
                sip_server=data["sip_server"],
                video_url=data.get("video_url"),
            )
        except KeyError as err:
            raise CloudAuthError(f"malformed device info response: {data}") from err

    async def bind_device(
        self, *, binding_code: str, token: str,
    ) -> BindResult:
        """POST /api/device {"code": "<binding_code>"} — formally bind us.

        Outcomes:
          - code=0 → BOUND (fresh)
          - code=2 db.DuplicateEntry → ALREADY_BOUND (= same user re-binds,
            also a success state, cloud still routes us)
          - anything else → CloudAuthError
        """
        url = f"{CLOUD_BASE}/device"
        headers = {"Cookie": f"token={token}"}
        try:
            async with self._session.post(
                url, json={"code": binding_code}, headers=headers,
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise CloudConnectionError(str(err)) from err
        code = data.get("code")
        if code == 0:
            return BindResult.BOUND
        if code == 2:
            return BindResult.ALREADY_BOUND
        raise CloudAuthError(f"bind rejected: {data}")


async def setup_cloud_device(
    client: CloudApiClient,
    *,
    email: str,
    password: str,
    device_id: str,
    binding_code: str | None = None,
) -> dict:
    """One-shot orchestration for HA config-flow.

    Sequence:
      1. `login(email, password, device_id)` → app_JWT + uid + city_id
      2. `get_device_info(device_id, token)` → sip_id + sip_password + sip_server
      3. If `binding_code` given: `bind_device(binding_code, token)` (best-effort,
         failure is non-fatal because Cloud routes SIP-INVITEs based on the
         device_record/user relationship regardless of formal slot binding).

    Returns a dict ready to merge into the HA config-entry. Strings only —
    safe to persist via HA's storage layer.
    """
    login = await client.login(
        email=email, password=password, device_id=device_id,
    )
    info = await client.get_device_info(
        device_id=device_id, token=login.token,
    )
    bind_value: str | None = None
    if binding_code:
        # Best-effort: bind failure (auth OR network) must NOT prevent us
        # from returning the cloud SIP creds we already fetched. Cloud
        # routes ring-INVITEs based on the user/device-record relationship,
        # not the formal slot binding (verified 2026-05-23).
        try:
            bind = await client.bind_device(
                binding_code=binding_code, token=login.token,
            )
            bind_value = bind.value
        except (CloudAuthError, CloudConnectionError):
            bind_value = "failed"
    return {
        "uid":          login.uid,
        "city_id":      login.city_id,
        "sip_id":       info.sip_id,
        "sip_password": info.sip_password,
        "sip_server":   info.sip_server,
        "bind_result":  bind_value,
    }
