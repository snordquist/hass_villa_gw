"""Tests for `RingDeduper` — cross-source ring-event deduplication.

The coordinator gets ring signals from up to three parallel paths
(SIP-INVITE, telnet log-tail, AT+B poll). We want exactly ONE
`EVENT_DOORBELL_RINGING` per real button press. RingDeduper is the
pure decider: given (source, monotonic_now) → True if HA should fire.
"""

from __future__ import annotations

import importlib.util
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


pkg = types.ModuleType("villa_gw_test_dedup")
pkg.__path__ = [str(PKG)]
sys.modules["villa_gw_test_dedup"] = pkg
mod = _load_module("villa_gw_test_dedup.ring_deduper", PKG / "ring_deduper.py")
RingDeduper = mod.RingDeduper


def test_first_signal_fires() -> None:
    d = RingDeduper(window_s=3.0)
    assert d.should_fire("sip", now=100.0) is True


def test_second_signal_within_window_suppressed() -> None:
    """A second source within the window must NOT fire — duplicate ring."""
    d = RingDeduper(window_s=3.0)
    d.should_fire("sip", now=100.0)
    assert d.should_fire("log", now=101.5) is False
    assert d.should_fire("poll", now=102.99) is False


def test_signal_after_window_fires_again() -> None:
    """A real second press, > window after the first, must fire."""
    d = RingDeduper(window_s=3.0)
    d.should_fire("sip", now=100.0)
    assert d.should_fire("sip", now=103.01) is True


def test_window_boundary_inclusive_lower_exclusive_upper() -> None:
    """`now == last + window` should be allowed (>= window has elapsed)."""
    d = RingDeduper(window_s=3.0)
    d.should_fire("sip", now=100.0)
    # Exactly at boundary — fire (window elapsed)
    assert d.should_fire("sip", now=103.0) is True
    # 1 microsecond before — suppress
    d2 = RingDeduper(window_s=3.0)
    d2.should_fire("sip", now=100.0)
    assert d2.should_fire("sip", now=102.9999) is False


def test_last_source_attribute_tracks_winner() -> None:
    """Exposed for diagnostic sensors (`sensor.villa_gw_last_ring_source`)."""
    d = RingDeduper(window_s=3.0)
    assert d.last_source is None
    d.should_fire("sip", now=100.0)
    assert d.last_source == "sip"
    # Suppressed sources do NOT overwrite — diagnostic must show what fired
    d.should_fire("log", now=100.5)
    assert d.last_source == "sip"


def test_last_fired_at_tracks_fire_time() -> None:
    d = RingDeduper(window_s=3.0)
    assert d.last_fired_at is None
    d.should_fire("sip", now=100.0)
    assert d.last_fired_at == 100.0
