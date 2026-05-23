"""Unit tests for `CloudApiClient` — talks to `de.ilifestyle-cloud.com`.

We mock `aiohttp.ClientSession` at the call site so the tests are pure / fast
and don't need network. Same import pattern as `test_parse_log_line.py` to
avoid pulling the HA stack in.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
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


# Synthetic package shim (same pattern as the existing parser tests)
pkg = types.ModuleType("villa_gw_test_cloud")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_cloud"] = pkg

cloud_api = _load_module("villa_gw_test_cloud.cloud_api", PKG / "cloud_api.py")
CloudApiClient = cloud_api.CloudApiClient
LoginResult = cloud_api.LoginResult
DeviceInfo = cloud_api.DeviceInfo
BindResult = cloud_api.BindResult
CloudAuthError = cloud_api.CloudAuthError
CloudConnectionError = cloud_api.CloudConnectionError


def _mock_session_with_response(status: int, body: dict):
    """Build an aiohttp.ClientSession-like AsyncMock that returns one response."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=body)
    response.text = AsyncMock(return_value=json.dumps(body))
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.get = MagicMock(return_value=response)
    return session


@pytest.mark.asyncio
async def test_login_returns_jwt_uid_and_city():
    """Successful login (code=0) returns LoginResult with the JWT, uid, city_id."""
    session = _mock_session_with_response(200, {
        "code": 0, "message": "OK",
        "token": "eyJabc123",
        "id": "u00cAAAAAAAAAAAA",
        "city_id": "de",
    })
    client = CloudApiClient(session=session)

    result = await client.login(
        email="x@y.de", password="pw", device_id="ha-test-1",
    )

    assert isinstance(result, LoginResult)
    assert result.token == "eyJabc123"
    assert result.uid == "u00cAAAAAAAAAAAA"
    assert result.city_id == "de"


@pytest.mark.asyncio
async def test_login_malformed_response_raises_auth_error():
    """Cloud returns code=0 but missing token → typed CloudAuthError, not KeyError.

    Defensive: API drift or partial-success response shouldn't crash the
    caller with an unrelated KeyError. Caller can handle CloudAuthError
    uniformly with all other login failures.
    """
    session = _mock_session_with_response(200, {
        "code": 0,
        # token, id, city_id intentionally missing
    })
    client = CloudApiClient(session=session)

    with pytest.raises(CloudAuthError):
        await client.login(email="x@y.de", password="pw", device_id="ha-test-1")


@pytest.mark.asyncio
async def test_login_wrong_credentials_raises_auth_error():
    """Login with bad creds → cloud returns non-zero code → CloudAuthError."""
    session = _mock_session_with_response(200, {
        "code": 2, "message": "wrong password",
    })
    client = CloudApiClient(session=session)

    with pytest.raises(CloudAuthError) as exc:
        await client.login(
            email="x@y.de", password="WRONG", device_id="ha-test-1",
        )
    assert "wrong password" in str(exc.value)


@pytest.mark.asyncio
async def test_login_network_error_raises_connection_error():
    """aiohttp.ClientError from the transport is wrapped as CloudConnectionError."""
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("conn refused"))
    client = CloudApiClient(session=session)

    with pytest.raises(CloudConnectionError) as exc:
        await client.login(
            email="x@y.de", password="pw", device_id="ha-test-1",
        )
    assert "conn refused" in str(exc.value)


@pytest.mark.asyncio
async def test_get_device_info_returns_sip_credentials():
    """`GET /api/device?id=<dev>` returns the cloud-issued SIP creds for our device.

    Cloud auto-issues sip_id+password when the device_record is first created
    (= at first /api/v2/login with that device_id). We just read them back.
    """
    fake_pw = "FAKEPW123"
    session = _mock_session_with_response(200, {
        "code": 0, "message": "OK",
        "id": "ha-test-1",
        "sip_id": "s00cAAAAAAAAAAAA",
        "password": fake_pw,
        "sip_server": "de.ilifestyle-cloud.com",
        "video_url": "rtmp://rtmp.de.ilifestyle-cloud.com/live/abc",
        "binding_id": 99999,
    })
    client = CloudApiClient(session=session)

    info = await client.get_device_info(device_id="ha-test-1", token="eyJabc")

    assert isinstance(info, DeviceInfo)
    assert info.sip_id == "s00cAAAAAAAAAAAA"
    assert info.sip_password == fake_pw
    assert info.sip_server == "de.ilifestyle-cloud.com"

    # Verify the call carried the JWT via Cookie (App-JWT pattern from APK)
    session.get.assert_called_once()
    cookie_arg = session.get.call_args.kwargs.get("headers", {}).get("Cookie", "")
    assert "eyJabc" in cookie_arg


@pytest.mark.asyncio
async def test_get_device_info_malformed_response_raises_auth_error():
    """Cloud returns code=0 but missing sip_id → typed CloudAuthError."""
    session = _mock_session_with_response(200, {
        "code": 0, "id": "x",
        # sip_id, password, sip_server intentionally missing
    })
    client = CloudApiClient(session=session)

    with pytest.raises(CloudAuthError):
        await client.get_device_info(device_id="x", token="eyJabc")


@pytest.mark.asyncio
async def test_get_device_info_url_encodes_device_id():
    """`device_id` is interpolated into the URL — must be URL-escaped.

    A device_id containing '&', '#' or '?' would otherwise inject query-string
    args or truncate the value. aiohttp's `params=` kwarg encodes correctly.
    """
    session = _mock_session_with_response(200, {
        "code": 0, "id": "x", "sip_id": "s", "password": "p",
        "sip_server": "de.ilifestyle-cloud.com",
    })
    client = CloudApiClient(session=session)

    await client.get_device_info(
        device_id="weird&id=injected#frag", token="eyJabc",
    )

    # The id must reach the server via `params`, not by raw f-string interpolation
    # — so the URL stays `/api/device` and the value is properly encoded.
    call = session.get.call_args
    url_arg = call.args[0] if call.args else call.kwargs.get("url", "")
    assert "weird" not in url_arg, "device_id must NOT be interpolated raw into URL"
    # aiohttp encodes via params kwarg
    params = call.kwargs.get("params", {})
    assert params.get("id") == "weird&id=injected#frag"


@pytest.mark.asyncio
async def test_bind_device_fresh_returns_bound():
    """First-time bind with a valid binding_code → BindResult.BOUND."""
    session = _mock_session_with_response(200, {
        "code": 0, "message": "OK", "id": "AABBCCDDEEFF",
    })
    client = CloudApiClient(session=session)

    result = await client.bind_device(
        binding_code="3339.examplecodeXYZ", token="eyJabc",
    )

    assert result is BindResult.BOUND


@pytest.mark.asyncio
async def test_bind_device_already_bound_returns_already_bound():
    """User already bound to this key → cloud responds code=2 (DuplicateEntry).

    Treated as a non-fatal 'already bound' — same outcome functionally.
    """
    session = _mock_session_with_response(200, {
        "code": 2, "message": "db.DuplicateEntry",
    })
    client = CloudApiClient(session=session)

    result = await client.bind_device(
        binding_code="3339.examplecodeXYZ", token="eyJabc",
    )

    assert result is BindResult.ALREADY_BOUND


@pytest.mark.asyncio
async def test_bind_device_other_error_raises():
    """Other cloud-side errors (bad code, forbidden, …) raise CloudAuthError."""
    session = _mock_session_with_response(200, {
        "code": 3, "message": "Forbidden",
    })
    client = CloudApiClient(session=session)

    with pytest.raises(CloudAuthError) as exc:
        await client.bind_device(
            binding_code="invalid", token="eyJabc",
        )
    assert "Forbidden" in str(exc.value)


@pytest.mark.asyncio
async def test_setup_cloud_device_orchestrates_login_info_bind():
    """`setup_cloud_device` is the one-shot helper config_flow calls.

    Sequence:
      1. login → app_JWT
      2. get_device_info → sip_id, sip_password, sip_server
      3. optional bind_device (best-effort)
    Returns a dict ready to store in the HA config-entry.data.
    """
    fake_pw = "FAKEPW123"
    # Need a session that returns DIFFERENT bodies for each call
    responses = [
        # 1: /api/v2/login
        {"code": 0, "token": "eyJabc", "id": "u00cAAAAAAAAAAAA", "city_id": "de"},
        # 2: /api/device?id=...
        {"code": 0, "id": "ha-test-1", "sip_id": "s00cAAAAAAAAAAAA",
         "password": fake_pw, "sip_server": "de.ilifestyle-cloud.com",
         "video_url": "rtmp://x/y"},
        # 3: /api/device (bind) — fresh
        {"code": 0, "id": "AABBCCDDEEFF"},
    ]
    iter_responses = iter(responses)

    def make_response(*a, **kw):
        r = MagicMock()
        r.status = 200
        r.json = AsyncMock(return_value=next(iter_responses))
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=None)
        return r

    session = MagicMock()
    session.post = MagicMock(side_effect=make_response)
    session.get = MagicMock(side_effect=make_response)
    client = CloudApiClient(session=session)

    result = await cloud_api.setup_cloud_device(
        client,
        email="x@y.de",
        password="cloud-pw",
        device_id="ha-test-1",
        binding_code="3339.examplecodeXYZ",
    )

    assert result == {
        "uid":          "u00cAAAAAAAAAAAA",
        "city_id":      "de",
        "sip_id":       "s00cAAAAAAAAAAAA",
        "sip_password": fake_pw,
        "sip_server":   "de.ilifestyle-cloud.com",
        "bind_result":  "bound",
    }


def _session_yielding(*bodies: dict):
    """Build a session mock that returns each body in turn for each call."""
    it = iter(bodies)
    def make_r(*a, **kw):
        r = MagicMock()
        r.status = 200
        r.json = AsyncMock(return_value=next(it))
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=None)
        return r
    session = MagicMock()
    session.post = MagicMock(side_effect=make_r)
    session.get = MagicMock(side_effect=make_r)
    return session


@pytest.mark.asyncio
async def test_setup_cloud_device_without_binding_code_skips_bind():
    """If no binding_code given, helper only does login + device_info."""
    fake_pw = "FAKEPW123"
    session = _session_yielding(
        {"code": 0, "token": "eyJabc", "id": "u00cAAAAAAAAAAAA", "city_id": "de"},
        {"code": 0, "id": "ha-test-1", "sip_id": "s00cAAAAAAAAAAAA",
         "password": fake_pw, "sip_server": "de.ilifestyle-cloud.com"},
    )

    result = await cloud_api.setup_cloud_device(
        CloudApiClient(session=session),
        email="x@y.de", password="pw", device_id="ha-test-1",
        binding_code=None,
    )

    assert result["bind_result"] is None
    assert result["sip_id"] == "s00cAAAAAAAAAAAA"
    # Only 1 POST call (login), no bind
    assert session.post.call_count == 1


@pytest.mark.asyncio
async def test_setup_cloud_device_swallows_bind_connection_error_non_fatal():
    """Bind hitting a network error (TCP reset, TLS drop) must be non-fatal —
    we already have sip_id+password from earlier steps, that's what matters.
    """
    fake_pw = "FAKEPW123"
    # Custom session: 2 successful responses for login+device_info, then
    # the 3rd POST (bind) raises ClientError → CloudConnectionError.
    bodies = iter([
        {"code": 0, "token": "eyJabc", "id": "u00cAAAAAAAAAAAA", "city_id": "de"},
        {"code": 0, "id": "ha-test-1", "sip_id": "s00cAAAAAAAAAAAA",
         "password": fake_pw, "sip_server": "de.ilifestyle-cloud.com"},
    ])
    def make_r(*a, **kw):
        r = MagicMock()
        r.status = 200
        r.json = AsyncMock(return_value=next(bodies))
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=None)
        return r

    post_call_count = [0]
    def post_side_effect(*a, **kw):
        post_call_count[0] += 1
        if post_call_count[0] == 1:
            return make_r()                          # login
        raise aiohttp.ClientError("conn reset")      # bind → network error

    session = MagicMock()
    session.post = MagicMock(side_effect=post_side_effect)
    session.get = MagicMock(side_effect=make_r)       # get_device_info

    result = await cloud_api.setup_cloud_device(
        CloudApiClient(session=session),
        email="x@y.de", password="pw", device_id="ha-test-1",
        binding_code="3339.examplecode",
    )

    # Cloud creds successfully delivered despite bind failure
    assert result["sip_id"] == "s00cAAAAAAAAAAAA"
    assert result["sip_password"] == fake_pw
    # Bind result reflects the failure
    assert result["bind_result"] == "failed"


@pytest.mark.asyncio
async def test_setup_cloud_device_swallows_bind_error_non_fatal():
    """If bind fails, we still return the cloud-credentials successfully.

    Bind is best-effort. Failure is non-fatal — login + device_info gave us
    everything we need for SIP-listening.
    """
    fake_pw = "FAKEPW123"
    session = _session_yielding(
        {"code": 0, "token": "eyJabc", "id": "u00cAAAAAAAAAAAA", "city_id": "de"},
        {"code": 0, "id": "ha-test-1", "sip_id": "s00cAAAAAAAAAAAA",
         "password": fake_pw, "sip_server": "de.ilifestyle-cloud.com"},
        # Bind fails with 'Forbidden'
        {"code": 3, "message": "Forbidden"},
    )

    result = await cloud_api.setup_cloud_device(
        CloudApiClient(session=session),
        email="x@y.de", password="pw", device_id="ha-test-1",
        binding_code="bad-code",
    )

    # Cloud creds still returned
    assert result["sip_id"] == "s00cAAAAAAAAAAAA"
    # Bind result reflects failure
    assert result["bind_result"] == "failed"
