"""Unit tests for the log-line parser.

`VillaGwClient._parse_log_line` is a pure classmethod and the cheapest thing
to lock down. Anything that flips its outputs will break automations
downstream — these are smoke tests, not coverage targets.

We load `api.py` and `_backoff.py` directly via importlib (NOT through the
package's `__init__.py`) because the package's __init__ pulls in voluptuous /
homeassistant which we don't want as test-time dependencies.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "villa_gw"

# Stub aiohttp if not installed — api.py only references aiohttp.ClientError
# and aiohttp.ClientSession by name, never invokes them in the parser path.
if "aiohttp" not in sys.modules:
    stub = types.ModuleType("aiohttp")
    stub.ClientError = type("ClientError", (Exception,), {})
    stub.ClientSession = type("ClientSession", (), {})
    sys.modules["aiohttp"] = stub


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Synthetic package to satisfy `from ._backoff` / `from .const` relative imports
pkg = types.ModuleType("villa_gw_test")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test"] = pkg

_load_module("villa_gw_test._backoff", PKG / "_backoff.py")
_load_module("villa_gw_test.const", PKG / "const.py")
api = _load_module("villa_gw_test.api", PKG / "api.py")
VillaGwClient = api.VillaGwClient


def test_doorbell_ringing():
    event = VillaGwClient._parse_log_line(
        "[2026-05-21 18:42:01] [INFO] [av_link] call_btn_trigger key_index=1"
    )
    assert event == {"type": "doorbell_ringing", "key_index": 1}


def test_door_unlocked():
    event = VillaGwClient._parse_log_line(
        "[2026-05-21 18:42:30] [INFO] [av_link] AT_UART_UNLOCK response=ok"
    )
    assert event == {"type": "door_unlocked", "response": "ok"}


def test_incoming_call():
    event = VillaGwClient._parse_log_line(
        "[2026-05-21 18:42:05] [INFO] [av_link] on_incoming_call state=4, "
        "callID=1, local_addr=2, remote_addr=1"
    )
    assert event is not None
    assert event["type"] == "call_incoming"
    assert event["state"] == 4
    assert event["call_id"] == 1
    assert event["remote_addr"] == "1"


def test_live_view_started_vs_ended():
    started = VillaGwClient._parse_log_line(
        "[ts] on_receive_monitor: state=1, from=u00c0000000022cd, key_index=1"
    )
    ended = VillaGwClient._parse_log_line(
        "[ts] on_receive_monitor: state=0, from=u00c0000000022cd, key_index=1"
    )
    assert started and started["type"] == "live_view_started"
    assert ended and ended["type"] == "live_view_ended"


def test_call_ended_via_hang():
    event = VillaGwClient._parse_log_line(
        "[ts] AT_UART_HANG state=1 self->key_index=1"
    )
    assert event == {"type": "call_ended", "state": 1, "key_index": 1}


def test_state_timeout():
    event = VillaGwClient._parse_log_line("[ts] STATE_RINGING 4 timeout")
    assert event and event["type"] == "state_timeout"
    assert event["state_name"] == "RINGING"


def test_cloud_mqtt_connect():
    event = VillaGwClient._parse_log_line("[ts] mqtt connect ok")
    assert event == {"type": "cloud_connect", "status": "ok"}


def test_unrelated_line_returns_none():
    assert VillaGwClient._parse_log_line("just some unrelated log noise") is None


def test_empty_line_returns_none():
    assert VillaGwClient._parse_log_line("") is None
