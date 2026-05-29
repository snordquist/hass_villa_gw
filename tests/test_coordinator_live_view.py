"""Tests for the coordinator's live_view_active state-mirror.

Covers the regression where HA-initiated `AT+B UART monitor` never flipped
`binary_sensor.live_sicht_aktiv` to ON because:

1. The polling path observes only `AT+B APPLICATION`, which stays idle
   during HA-direct monitor sessions.
2. The log-tail `monitor_response` event was previously mapped to None.

The fix routes HA-button presses through coordinator.mark_live_view_*
methods and adds `monitor_response=ok` as a backup signal.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "villa_gw"


# ──────────────────────────────────────────── stub external deps

if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientError = type("ClientError", (Exception,), {})
    aiohttp_stub.ClientSession = type("ClientSession", (), {})
    sys.modules["aiohttp"] = aiohttp_stub


def _stub(name: str, **attrs: object) -> types.ModuleType:
    mod = sys.modules.get(name) or types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _callback(fn):  # @callback no-op
    return fn


# Build the homeassistant.* tree
_stub("homeassistant")
_stub("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
_stub(
    "homeassistant.core",
    HomeAssistant=type("HomeAssistant", (), {}),
    callback=_callback,
)
_stub("homeassistant.helpers")
_stub(
    "homeassistant.helpers.event",
    async_track_time_interval=lambda *a, **kw: (lambda: None),
)


class _DataUpdateCoordinator:
    """Minimal stand-in for HA's DataUpdateCoordinator.

    Only what coordinator.py touches in the code paths we exercise: __init__
    signature, .data attribute, async_set_updated_data().
    """

    def __class_getitem__(cls, _item):  # support `DataUpdateCoordinator[...]`
        return cls

    def __init__(self, hass, logger, *, name, update_interval):  # noqa: D401
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data: dict | None = None
        self._set_calls = 0

    def async_set_updated_data(self, data) -> None:
        self.data = data
        self._set_calls += 1


_stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_DataUpdateCoordinator,
)


# ──────────────────────────────────────────── load villa_gw modules

def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pkg = types.ModuleType("villa_gw_cotest")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_cotest"] = pkg

_load("villa_gw_cotest._backoff", PKG / "_backoff.py")
const = _load("villa_gw_cotest.const", PKG / "const.py")
_load("villa_gw_cotest.api", PKG / "api.py")
coord_mod = _load("villa_gw_cotest.coordinator", PKG / "coordinator.py")
VillaGwCoordinator = coord_mod.VillaGwCoordinator


# ──────────────────────────────────────────── helpers

class _FakeLoop:
    def __init__(self) -> None:
        self._t = 1000.0

    def time(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _FakeBus:
    def __init__(self) -> None:
        self.fired: list[tuple[str, dict]] = []

    def async_fire(self, event_type, payload):
        self.fired.append((event_type, payload))


class _FakeHass:
    def __init__(self) -> None:
        self.loop = _FakeLoop()
        self.bus = _FakeBus()


class _FakeEntry:
    data: dict = {}
    options: dict = {}
    unique_id = "test"
    entry_id = "test_entry"


def _make_coord() -> VillaGwCoordinator:
    hass = _FakeHass()
    coord = VillaGwCoordinator.__new__(VillaGwCoordinator)
    _DataUpdateCoordinator.__init__(
        coord,
        hass,
        None,
        name="t",
        update_interval=None,
    )
    # Mirror the rest of __init__'s state-initialization (without touching client)
    coord.entry = _FakeEntry()
    coord.client = None
    coord._poll_task = None
    coord._tail_task = None
    coord._auto_clear_unsub = None
    coord._live_view_max_s = 60
    coord.mqtt_bridge = None
    coord.app_state = {}
    coord.sys_state = {}
    coord.gateway_online = False
    coord.live_view_active = False
    coord.doorbell_active = False
    coord.call_active = False
    coord.outdoor_station_ringing = False
    coord.cloud_online = False
    coord.early_probe_armed = False
    coord._early_probe_armed_at = 0.0
    coord.last_probe_result = None
    coord.last_doorbell_at = None
    coord.last_caller = None
    coord.last_app_user = None
    coord.last_unlock_at = None
    coord.live_view_started_at = None
    coord.doorbell_count_today = 0
    coord.unlock_count_today = 0
    coord.call_count_today = 0
    coord._last_event_at = {}
    return coord


# ──────────────────────────────────────────── tests

def test_mark_live_view_started_flips_flag_and_fires_event():
    coord = _make_coord()
    coord.mark_live_view_started("ha_local")

    assert coord.live_view_active is True
    assert coord.live_view_started_at == 1000.0
    assert coord.last_app_user == "ha_local"
    assert (const.EVENT_LIVE_VIEW_STARTED, {"source": "ha_local"}) in coord.hass.bus.fired


def test_mark_live_view_ended_clears_flag_and_fires_event():
    coord = _make_coord()
    coord.mark_live_view_started("ha_local")
    coord.hass.bus.fired.clear()

    coord.mark_live_view_ended("ha_local")
    assert coord.live_view_active is False
    assert coord.live_view_started_at is None
    assert (const.EVENT_LIVE_VIEW_ENDED, {"source": "ha_local"}) in coord.hass.bus.fired


def test_mark_live_view_ended_when_inactive_does_not_fire():
    coord = _make_coord()
    coord.mark_live_view_ended("ha_local")
    assert coord.live_view_active is False
    # No spurious event when already inactive
    assert all(et != const.EVENT_LIVE_VIEW_ENDED for et, _ in coord.hass.bus.fired)


def test_auto_clear_after_max_duration():
    coord = _make_coord()
    coord._live_view_max_s = 60
    coord.mark_live_view_started("ha_local")

    # 59s in → still active
    coord.hass.loop.advance(59.0)
    coord._tick_auto_clear()
    assert coord.live_view_active is True

    # 61s total → cleared
    coord.hass.loop.advance(2.0)
    coord._tick_auto_clear()
    assert coord.live_view_active is False
    assert coord.live_view_started_at is None


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_monitor_response_ok_starts_live_view_via_log_tail():
    coord = _make_coord()
    _run(coord._on_log_event({"type": "monitor_response", "response": "ok"}))

    assert coord.live_view_active is True
    assert coord.live_view_started_at == 1000.0
    # Event also fired
    assert any(et == const.EVENT_LIVE_VIEW_STARTED for et, _ in coord.hass.bus.fired)


def test_monitor_response_err_does_not_start_live_view():
    coord = _make_coord()
    _run(coord._on_log_event({"type": "monitor_response", "response": "err"}))

    assert coord.live_view_active is False
    assert all(et != const.EVENT_LIVE_VIEW_STARTED for et, _ in coord.hass.bus.fired)


def test_dedup_window_suppresses_duplicate_live_view_event():
    """If poll-path already fired LIVE_VIEW_ENDED, log-tail's follow-up is dropped."""
    coord = _make_coord()
    coord._fire(const.EVENT_LIVE_VIEW_STARTED, {"source": "first"})
    coord._fire(const.EVENT_LIVE_VIEW_STARTED, {"source": "second"})
    started = [p for et, p in coord.hass.bus.fired if et == const.EVENT_LIVE_VIEW_STARTED]
    assert started == [{"source": "first"}]
