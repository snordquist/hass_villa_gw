"""Optional log-tail surface (telnet, port 23).

Persistent telnet `:23` → `tail -F` of the GW's usr-log.log → regex
event parser. Fast-path for sub-100 ms event delivery, only used if the
user opts in (and accepts that telnet is open). Contains the
`LOG_PATTERN_*`-driven `_parse_log_line` parser used downstream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable

from ._backoff import Backoff
from .const import (
    BACKOFF_FACTOR,
    BACKOFF_INITIAL_S,
    BACKOFF_JITTER,
    BACKOFF_MAX_S,
    LOG_PATTERN_HANG,
    LOG_PATTERN_INCOMING_CALL,
    LOG_PATTERN_MONITOR_RECV,
    LOG_PATTERN_MONITOR_RESP,
    LOG_PATTERN_MQTT_CONNECT,
    LOG_PATTERN_MQTT_IN,
    LOG_PATTERN_MQTT_OUT,
    LOG_PATTERN_RING,
    LOG_PATTERN_RINGBACK,
    LOG_PATTERN_STATE_TIMEOUT,
    LOG_PATTERN_UNLOCK,
    PORT_TELNET,
)

_LOGGER = logging.getLogger(__name__)

LOG_FILE = "/customer/share/usr-log.log"
# Cap on a single log line before we drop the in-progress buffer. The Villa
# GW emits well-bounded log lines; anything over 64 KiB is almost certainly
# binary spew or a stuck connection.
LOG_LINE_MAX_BYTES = 65536


class VillaGwLogTailMixin:
    """Telnet log-tail + log-line parser. Mixed into VillaGwClient."""

    async def stream_log_events(
        self, handler: Callable[[dict], Awaitable[None]]
    ) -> None:
        """Persistent telnet → tail -F → regex events.

        Reconnects on failure with capped exponential backoff (see Backoff).
        A clean session (graceful EOF from server, no exception) is treated as
        a connection drop — we still re-attempt, but reset backoff so it
        doesn't look like a sustained failure.
        """
        bo = Backoff(
            initial=BACKOFF_INITIAL_S,
            factor=BACKOFF_FACTOR,
            cap=BACKOFF_MAX_S,
            jitter=BACKOFF_JITTER,
        )
        while True:
            try:
                await self._tail_once(handler)
                # Graceful EOF — session ended without error.
                # Treat as a transient drop; reset and retry quickly.
                bo.reset()
                delay = BACKOFF_INITIAL_S
                _LOGGER.info("Telnet tail session ended — reconnecting in %.1fs", delay)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                delay = bo.next_delay()
                _LOGGER.warning(
                    "Telnet tail failed (attempt %d): %s — retry in %.1fs",
                    bo.failure_count, err, delay,
                )
            await asyncio.sleep(delay)

    async def _tail_once(self, handler: Callable[[dict], Awaitable[None]]) -> None:
        reader, writer = await asyncio.open_connection(self._host, PORT_TELNET)
        _LOGGER.debug("Telnet tail connected to %s", self._host)
        try:
            await asyncio.sleep(0.5)
            try:
                await asyncio.wait_for(reader.read(512), timeout=1)
            except asyncio.TimeoutError:
                pass
            writer.write(f"tail -F {LOG_FILE}\n".encode())
            await writer.drain()
            async for line in self._iter_lines(reader):
                event = self._parse_log_line(line)
                if event:
                    try:
                        await handler(event)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.exception("Log event handler failed: %s", err)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _iter_lines(reader: asyncio.StreamReader) -> AsyncIterator[str]:
        """Yield log lines, dropping anything pathologically oversized.

        Defensive against a device that emits a stream without newlines or
        starts spewing binary; without this the buffer would grow unbounded.
        """
        buf = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                return
            buf += chunk
            if len(buf) > LOG_LINE_MAX_BYTES:
                _LOGGER.warning(
                    "log tail: dropping oversized buffer (%d bytes, no newline)",
                    len(buf),
                )
                buf = b""
                continue
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                yield line.decode("utf-8", errors="replace").rstrip("\r")

    # Pre-compiled patterns for speed (the tail loop runs every line)
    _RE_RING = re.compile(LOG_PATTERN_RING)
    _RE_INCOMING_CALL = re.compile(LOG_PATTERN_INCOMING_CALL)
    _RE_RINGBACK = re.compile(LOG_PATTERN_RINGBACK)
    _RE_HANG = re.compile(LOG_PATTERN_HANG)
    _RE_MONITOR_RECV = re.compile(LOG_PATTERN_MONITOR_RECV)
    _RE_MONITOR_RESP = re.compile(LOG_PATTERN_MONITOR_RESP)
    _RE_STATE_TIMEOUT = re.compile(LOG_PATTERN_STATE_TIMEOUT)
    _RE_UNLOCK = re.compile(LOG_PATTERN_UNLOCK)
    _RE_MQTT_IN = re.compile(LOG_PATTERN_MQTT_IN)
    _RE_MQTT_OUT = re.compile(LOG_PATTERN_MQTT_OUT)
    _RE_MQTT_CONNECT = re.compile(LOG_PATTERN_MQTT_CONNECT)

    @classmethod
    def _parse_log_line(cls, line: str) -> dict | None:
        """Map a usr-log.log line to a structured event dict.

        Order is important: more specific matches first. Returns None for
        lines that aren't actionable (REG_NOMATCH, config_reload, etc.) so
        the coordinator only sees relevant events.
        """
        # ── Bus / call lifecycle ────────────────────────────────────
        if "call_btn_trigger" in line:
            m = cls._RE_RING.search(line)
            if m:
                return {"type": "doorbell_ringing", "key_index": int(m.group(1))}
        if "on_incoming_call" in line:
            m = cls._RE_INCOMING_CALL.search(line)
            if m:
                return {
                    "type": "call_incoming",
                    "state": int(m.group(1)),
                    "call_id": int(m.group(2)),
                    "local_addr": m.group(3).strip(),
                    "remote_addr": m.group(4),
                }
        if "AT_UART_RINGBACK" in line:
            m = cls._RE_RINGBACK.search(line)
            if m:
                return {
                    "type": "ringback",
                    "state": int(m.group(1)),
                    "response": m.group(2),
                }
        if "AT_UART_HANG" in line:
            m = cls._RE_HANG.search(line)
            if m:
                return {
                    "type": "call_ended",
                    "state": int(m.group(1)),
                    "key_index": int(m.group(2)),
                }
        if "AT_UART_UNLOCK" in line:
            m = cls._RE_UNLOCK.search(line)
            if m:
                return {"type": "door_unlocked", "response": m.group(1)}

        # ── Live-view (silent monitor) ──────────────────────────────
        if "on_receive_monitor" in line:
            m = cls._RE_MONITOR_RECV.search(line)
            if m:
                state = int(m.group(1))
                # state=1 = start; state=0 = stop; other values = transitions
                ev_type = "live_view_started" if state == 1 else (
                    "live_view_ended" if state == 0 else "live_view_state"
                )
                return {
                    "type": ev_type,
                    "state": state,
                    "from": m.group(2),
                    "key_index": int(m.group(3)),
                }
        if "AT_UART_MONITOR" in line:
            m = cls._RE_MONITOR_RESP.search(line)
            if m:
                return {"type": "monitor_response", "response": m.group(1)}

        # ── State-machine timeouts ──────────────────────────────────
        if "timeout" in line and "STATE_" in line:
            m = cls._RE_STATE_TIMEOUT.search(line)
            if m:
                return {
                    "type": "state_timeout",
                    "state_name": m.group(1),
                    "value": int(m.group(2)) if m.group(2) else None,
                }

        # ── Cloud-MQTT observation (incoming + outgoing) ────────────
        if "mqtt_client_message_callback" in line:
            m = cls._RE_MQTT_IN.search(line)
            if m:
                payload = m.group(2)
                return {
                    "type": "cloud_mqtt_in",
                    "topic": m.group(1).rstrip(","),
                    "payload_raw": payload,
                    "payload": _safe_json_parse(payload),
                }
        if "response_mqtt_message" in line:
            m = cls._RE_MQTT_OUT.search(line)
            if m:
                payload = m.group(2)
                return {
                    "type": "cloud_mqtt_out",
                    "topic": m.group(1).rstrip(","),
                    "payload_raw": payload,
                    "payload": _safe_json_parse(payload),
                }
        if "mqtt connect" in line:
            m = cls._RE_MQTT_CONNECT.search(line)
            if m:
                return {"type": "cloud_connect", "status": m.group(1)}

        return None


def _safe_json_parse(s: str) -> dict | None:
    """Tolerant JSON parser — payloads in the log may be slightly malformed."""
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
