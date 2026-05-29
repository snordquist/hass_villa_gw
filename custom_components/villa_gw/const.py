"""Constants for the Villa GW integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "villa_gw"

# Config entry keys
CONF_HOST = "host"
CONF_WEB_USERNAME = "web_username"
CONF_WEB_PASSWORD = "web_password"
CONF_OUTDOOR_ADDRESS = "outdoor_address"
CONF_LIVE_VIEW_DURATION = "live_view_duration"
CONF_ENABLE_LOG_TAIL = "enable_log_tail"
CONF_POLL_INTERVAL_MS = "poll_interval_ms"
CONF_ENABLE_MQTT_BRIDGE = "enable_mqtt_bridge"
CONF_ENABLE_MQTT_DISCOVERY = "enable_mqtt_discovery"
CONF_MQTT_BASE_TOPIC = "mqtt_base_topic"

# Cloud-side configuration — Phase 1 of integration extension
# (see villa_gw/cloud_fcm/INTEGRATION_PLAN.md)
CONF_ENABLE_CLOUD = "enable_cloud"
CONF_ENABLE_SIP_RINGING = "enable_sip_ringing"  # experiment: 100+180 vs silent
CONF_CLOUD_EMAIL = "cloud_email"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_HA_DEVICE_ID = "ha_device_id"             # generated once, persistent
CONF_BINDING_CODE = "binding_code"              # optional, formal bind
# Cached cloud-side credentials (auto-refreshed by coordinator on auth-fail)
CONF_CACHED_SIP_ID = "cached_sip_id"
CONF_CACHED_SIP_PASSWORD = "cached_sip_password"
CONF_CACHED_SIP_SERVER = "cached_sip_server"
CONF_CACHED_CLOUD_UID = "cached_cloud_uid"
CONF_CACHED_CITY_ID = "cached_city_id"

# Defaults
DEFAULT_WEB_USERNAME = "admin"
DEFAULT_WEB_PASSWORD = "admin"
DEFAULT_OUTDOOR_ADDRESS = 1
DEFAULT_LIVE_VIEW_DURATION = 30
DEFAULT_KEY_INDEX = 1  # callList.id, almost always 1
DEFAULT_ENABLE_LOG_TAIL = False  # opt-in fast-path (needs telnet open)
DEFAULT_POLL_INTERVAL_MS = 1000  # 1 s — main event-detection cadence
DEFAULT_ENABLE_MQTT_BRIDGE = False  # opt-in: publish events to HA-Mosquitto
DEFAULT_ENABLE_MQTT_DISCOVERY = False  # off → only events, no auto-create
DEFAULT_MQTT_BASE_TOPIC = "villa_gw"

# Cloud defaults
DEFAULT_ENABLE_CLOUD = False  # opt-in: Cloud-SIP listener for ring events
# Experiment: when the Cloud listener is on, reply 100 Trying + 180 Ringing to
# the forked INVITE (behave like a real phone) instead of staying fully silent.
# Default OFF → SilentStrategy (current production behaviour).
DEFAULT_ENABLE_SIP_RINGING = False

# Capped exponential backoff for any persistent connection (telnet tail, poll loop)
BACKOFF_INITIAL_S = 2.0     # first retry after 2s
BACKOFF_FACTOR = 2.0        # double on each consecutive failure
BACKOFF_MAX_S = 300.0       # cap at 5 minutes
BACKOFF_JITTER = 0.25       # ±25% jitter to avoid thundering herd

# Authoritative GW↔Cloud link-status cadence. The 1 Hz main poll exists for
# ring-detection; re-reading /api/sip that fast would hammer the web login,
# so cloud-link status is refreshed on this slower beat. The log-tail
# `cloud_connect` event still updates it instantly between these polls.
CLOUD_STATUS_INTERVAL_S = 30.0

# Doorbell-ringing-pulse — how long the binary_sensor stays ON after a ring
DOORBELL_PULSE_SECONDS = 10
# Counter reset hour (midnight in HA's local timezone)
COUNTER_RESET_HOUR = 0

# Network ports
PORT_TELNET = 23
PORT_WEB = 80
PORT_RTSP = 554
PORT_AVLINK = 10086
PORT_UART2D = 10087
PORT_MIMEDIA = 10600

# Endpoints
RTSP_PATH = "/live.sdp"

# Sensor refresh — slower than the state poll because uptime/mem doesn't change fast
SENSOR_REFRESH_INTERVAL = timedelta(seconds=60)

# ──────────────────────────────────────────────────────── Events

# Bus / call lifecycle
EVENT_STATE_CHANGED      = "villa_gw_state_changed"       # any state transition
EVENT_DOORBELL_RINGING   = "villa_gw_doorbell_ringing"    # outdoor button pressed
EVENT_RINGBACK           = "villa_gw_ringback"            # outdoor station's own ringback tone
EVENT_LIVE_VIEW_STARTED  = "villa_gw_live_view_started"
EVENT_LIVE_VIEW_ENDED    = "villa_gw_live_view_ended"
EVENT_CALL_INCOMING      = "villa_gw_call_incoming"
EVENT_CALL_ACCEPTED      = "villa_gw_call_accepted"
EVENT_CALL_ENDED         = "villa_gw_call_ended"
EVENT_STATE_TIMEOUT      = "villa_gw_state_timeout"       # internal timeout fired

# Door / lock
EVENT_DOOR_UNLOCKED      = "villa_gw_door_unlocked"

# Cloud-MQTT passively observed (we read the GW's MQTT traffic from the log)
EVENT_CLOUD_MQTT_IN      = "villa_gw_cloud_mqtt_in"       # GW received MQTT message
EVENT_CLOUD_MQTT_OUT     = "villa_gw_cloud_mqtt_out"      # GW sent MQTT message
EVENT_CLOUD_CONNECT      = "villa_gw_cloud_connect"       # GW's MQTT connection state

# ──────────────────────────────────────────────────────── State machine

# avlink internal state names (from binary strings). Numeric values are inferred
# at runtime from the first poll responses + transitions. The integration treats
# state IDs as opaque integers and uses transitions for event firing, so even
# unknown / new states behave correctly — they just get logged.
KNOWN_STATE_NAMES = (
    "IDLE",
    "REGISTER_SUCCESS",
    "REGISTER_FAIL",
    "CALLING",
    "RINGING",
    "RINGBACK",
    "TALKING",
    "TALKING_BUS",
    "MONITOR",
)

# Heuristic classification — used as fallback if we don't have a numeric→name
# mapping yet. Returns the "kind" of state from the JSON shape.
def state_classify(application: dict) -> str:
    """Classify the current state from an `AT+B APPLICATION` response.

    Returns one of: 'idle', 'ringing', 'monitor', 'talking', 'calling', 'unknown'.
    Uses both the `state` integer and the `call` array to disambiguate.
    """
    state = application.get("state", 0)
    call = application.get("call", [0])
    has_call = bool(call) and any(c for c in call)
    # Initial guesses — refined empirically:
    # state=1 + call=[0]  → idle
    # state=1 + call=[>0] → call active (don't know which kind yet)
    # state>1             → something is happening
    if state <= 1 and not has_call:
        return "idle"
    if has_call:
        return "active"  # ringing / talking / monitoring — refine on transition
    return "transition"


# ──────────────────────────────────────────────────────── Log-tail patterns
# Optional fast-path; only used when CONF_ENABLE_LOG_TAIL is true.
# Note: anchor with the function tag in square brackets to reduce false matches.

# Doorbell / call lifecycle
LOG_PATTERN_RING             = r"\bcall_btn_trigger key_index=(\d+)"
LOG_PATTERN_INCOMING_CALL    = (
    r"on_incoming_call state=(\d+), callID=(\d+), local_addr=([^,]+), remote_addr=([^,\s]+)"
)
LOG_PATTERN_RINGBACK         = r"AT_UART_RINGBACK state=(\d+),\s*response=(\w+)"
LOG_PATTERN_HANG             = r"AT_UART_HANG state=(\d+) self->key_index=(\d+)"

# Live-view (silent monitor)
LOG_PATTERN_MONITOR_RECV     = (
    r"on_receive_monitor: state=(\d+), from=([^,]+), key_index=(\d+)"
)
LOG_PATTERN_MONITOR_RESP     = r"AT_UART_MONITOR response=(\w+)"
LOG_PATTERN_STATE_TIMEOUT    = (
    r"STATE_(MONITOR|RINGING|CALLING|TALKING_BUS|TALKING|RINGBACK)\s*(\d+)?\s*timeout"
)

# Door / lock
LOG_PATTERN_UNLOCK           = r"AT_UART_UNLOCK response=(\w+)"

# Cloud-MQTT observation — every message in/out is visible here.
# Bonus side-effect: gives us full insight into the cloud-side push channel
# WITHOUT having to authenticate against the cloud MQTT broker ourselves.
LOG_PATTERN_MQTT_IN          = (
    r"mqtt_client_message_callback[^\]]*\].*?topic=(\S+),\s*payload=(\{.+\})"
)
LOG_PATTERN_MQTT_OUT         = (
    r"response_mqtt_message[^\]]*\].*?remote_topic=(\S+),\s*message=(\{.+\})"
)
LOG_PATTERN_MQTT_CONNECT     = r"mqtt connect (ok|fail|err)"

# SIP call-initiation phases (avlink → pjsua)
LOG_PATTERN_MAKE_CALL        = r"make_(sip|local|calls)_call|make_calls "


# ──────────────────────────────────────────────────────── MQTT-Bridge topics
# Used when CONF_ENABLE_MQTT_BRIDGE is on. <base> defaults to "villa_gw",
# <did> is the device identifier (lowercased MAC without colons).
#
# Conventions:
#   - retained: long-lived state (availability, current state, system)
#   - non-retained: transient events (doorbell ringing, etc.)
#   - availability: published online/offline on start/stop. NOT a true LWT —
#     if HA crashes the topic stays "online" until a fresh start cleans it up.

def topic_availability(base: str, did: str) -> str:
    return f"{base}/{did}/availability"

def topic_state(base: str, did: str) -> str:
    return f"{base}/{did}/state"

def topic_system(base: str, did: str) -> str:
    return f"{base}/{did}/system"

def topic_event(base: str, did: str, event: str) -> str:
    return f"{base}/{did}/event/{event}"

def topic_cloud(base: str, did: str, direction: str) -> str:
    """direction = 'in' | 'out' | 'connect'"""
    return f"{base}/{did}/cloud/{direction}"

def topic_cmd(base: str, did: str, cmd: str) -> str:
    return f"{base}/{did}/cmd/{cmd}"

# Mapping log-event-types → MQTT event slugs (used by mqtt_bridge).
# `cloud_*` events get their own subtree (cloud/in, cloud/out, cloud/connect)
# rather than being mirrored under event/* — keeps the topic tree tidy.
MQTT_EVENT_SLUGS = {
    "doorbell_ringing":  "doorbell",
    "call_incoming":     "call_incoming",
    "call_ended":        "call_ended",
    "ringback":          "ringback",
    "live_view_started": "live_view_started",
    "live_view_ended":   "live_view_ended",
    "live_view_state":   "live_view_state",
    "state_timeout":     "state_timeout",
    "door_unlocked":     "door_unlocked",
    "monitor_response":  "monitor_response",
}
# Log-event types that bypass the event/* topic and only go to cloud/*
MQTT_CLOUD_ONLY_TYPES = ("cloud_mqtt_in", "cloud_mqtt_out", "cloud_connect")

# Commands accepted on cmd/* topics → uart2d AT+B-strings
MQTT_COMMANDS = (
    "wake",          # payload: optional {"duration": 30}
    "stop_live",
    "door",          # alias: unlock
    "unlock",        # explicit alias
    "hook",          # accept call
    "hangup",
    "switch_camera",
    "snapshot",
    "at_raw",        # payload: raw AT+B-command string (power-user)
)
