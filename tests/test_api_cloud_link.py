"""Unit tests for `VillaGwClient.cloud_link_online`.

Authoritative GW↔Cloud link status read from the web API (`GET /api/sip`
→ `online`). This is the pollable, restart-surviving source that replaces
the edge-triggered `mqtt connect ok` log line as the source of truth for
`binary_sensor.cloud_online`.

Same importlib shim pattern as `test_cloud_api.py` so we don't pull the HA
stack — `api.py` only imports `.const` and `._backoff`, both HA-free.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


pkg = types.ModuleType("villa_gw_test_api")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_api"] = pkg

_load_module("villa_gw_test_api.const", PKG / "const.py")
_load_module("villa_gw_test_api._backoff", PKG / "_backoff.py")
api = _load_module("villa_gw_test_api.api", PKG / "api.py")
VillaGwClient = api.VillaGwClient


def _client() -> "api.VillaGwClient":
    return VillaGwClient(
        host="gw.test",
        web_username="admin",
        web_password="pw",
        session=MagicMock(),
    )


@pytest.mark.asyncio
async def test_cloud_link_online_true_when_gw_reports_online():
    client = _client()
    client._get_json = AsyncMock(
        return_value={"online": True, "status": 0, "server": "de.ilifestyle-cloud.com"}
    )
    assert await client.cloud_link_online() is True
    client._get_json.assert_awaited_once_with("/api/sip")


@pytest.mark.asyncio
async def test_cloud_link_online_false_when_gw_reports_offline():
    client = _client()
    client._get_json = AsyncMock(return_value={"online": False, "status": 1})
    assert await client.cloud_link_online() is False


@pytest.mark.asyncio
async def test_cloud_link_online_missing_field_defaults_to_false():
    client = _client()
    client._get_json = AsyncMock(return_value={"status": 0})
    assert await client.cloud_link_online() is False


@pytest.mark.asyncio
async def test_cloud_link_online_coerces_truthy_to_bool():
    client = _client()
    client._get_json = AsyncMock(return_value={"online": 1})
    result = await client.cloud_link_online()
    assert result is True
