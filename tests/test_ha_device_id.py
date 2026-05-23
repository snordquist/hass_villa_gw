"""Tests for `generate_ha_device_id` helper.

Uses the same importlib-direct-load pattern as `test_cloud_api.py` so we
don't need the HA framework imported.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "villa_gw"


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pkg = types.ModuleType("villa_gw_test_devid")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_devid"] = pkg

cloud_api = _load_module("villa_gw_test_devid.cloud_api", PKG / "cloud_api.py")
generate_ha_device_id = cloud_api.generate_ha_device_id


def test_generate_ha_device_id_format() -> None:
    """Returned id must match the pattern `homeassistant-villa-<12hex>`.

    Cloud's `device_id` parameter is stored under our user-account; a
    stable but unique id avoids phantom-record pollution across reinstalls
    on the same HA host.
    """
    devid = generate_ha_device_id()
    assert re.fullmatch(r"homeassistant-villa-[0-9a-f]{12}", devid), devid


def test_generate_ha_device_id_unique() -> None:
    """Two calls return different ids — each fresh setup gets its own slot."""
    a = generate_ha_device_id()
    b = generate_ha_device_id()
    assert a != b
