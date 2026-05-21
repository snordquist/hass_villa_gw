# Villa GW Protocol Reference

All known interfaces of the HHG/EGB Villa GW (AVL20P / ACP-03). Reverse-engineered from firmware 4.1.11.

## TL;DR — minimum to integrate

```bash
# Wake camera (silent live-view, no ringtone)
printf 'AT+B UART monitor 1 1 30\r\n' | nc <GW_IP> 10087

# Open door
printf 'AT+B UART unlock 1\r\n' | nc <GW_IP> 10087

# Get camera stream (RTSP, only delivers frames while live-view active)
rtsp://admin:admin@<GW_IP>/live.sdp
```

Replace `<GW_IP>` with your gateway's IP. The bus address `1` after `monitor`/`unlock` is the outdoor station — adjust if different (most installs use `1`).

---

## 1. uart2d — TCP port 10087 (primary control surface) ⭐

The simplest and most reliable control surface. `uart2d` is the daemon bridging the Villa-Bus (UART, `/dev/ttyS1`) to TCP. It listens on `0.0.0.0:10087` and accepts plain-text AT+B commands terminated with `\r\n`. **No authentication, no filtering** — anyone on the LAN can talk to it.

### Command format

```
AT+B <CATEGORY> <SUBCOMMAND> [args...]\r\n
```

### Bus / Outdoor station commands

| Command | Effect | Args |
|---|---|---|
| `AT+B UART call <key_index> <addr>` | **Calls outdoor station** (it rings!) | key_index = `callList.id` (usually `1`), addr = outdoor bus address |
| `AT+B UART monitor <state> <addr> <duration>` | **Silent live-view** (no ringtone) | state=`1` start / `0` stop; addr=outdoor bus address; duration in seconds |
| `AT+B UART hook <call_id>` | Accept call | call_id = active call ID (usually `1`) |
| `AT+B UART hang <call_id>` | End/decline call | call_id |
| `AT+B UART unlock <n>` | Trigger door relay via bus | n = `1` (relay index) |
| `AT+B UART intercom <from> <to>` | Intercom between two indoor monitors | from / to = bus addresses |
| `AT+B UART switchCamera` | Cycle through multiple outdoor stations | — |
| `AT+B UART ringback <addr>` | Send ringback tone | addr |

### Camera / RTSP commands

| Command | Effect |
|---|---|
| `AT+B MJPG Snap` | Generate single MJPG snapshot |
| `AT+B RTSP start` | Start RTSP server (port 554) |
| `AT+B RTSP stop` | Stop RTSP server |
| `AT+B VIDEO START` | Start frame encoder (Hisilicon `Mi_Common_Video_Start`) |
| `AT+B VIDEO STOP` | Stop encoder |

### Response

`uart2d` is fire-and-forget for most commands. Some return a short status string like `response=ok` synchronously. Verify via:

- `/customer/share/usr-log.log` — look for matching `AT_UART_<CMD>` lines
- RTSP stream byte-count (idle ~5 KB/s, live ~250 KB/s)

### Internal protocol (uart2d → /dev/ttyS1)

Once `uart2d` parses the AT+B command, it builds a binary bus frame and writes it to `/dev/ttyS1`. Format (from binary strings):

```
parse monitor param = %c, times = %d
userial_send_monitor_call_msg: src_addr = %d, dst_addr = %d
cmd : 0x%02x
```

The bus frame is a proprietary HHG/EGB format we did not fully reverse. We don't need to — uart2d takes care of it.

---

## 2. avlink — TCP port 10086 (filtered, generally avoid)

The central application daemon. Receives AT+B commands from Lua handlers (`/customer/lua/*.lua`), forwards bus-bound commands to `uart2d:10087`, and handles MQTT + SIP signalling. Listens on `0.0.0.0:10086`.

**Filter behaviour**: TCP commands from non-localhost / non-MQTT sources are rejected with `response=err`. So while you _can_ TCP-connect, `AT+B UART *` commands from external clients are dropped. **Use `uart2d:10087` instead.**

### Read-only commands (safe from any TCP source)

These work from outside and return JSON:

| Command | Response |
|---|---|
| `AT+B APPLICATION` | `{"state":1,"sip":0/1,"call":[0]}` |
| `AT+B SYSTEM` | `{"version":"4.1.11","uptime":...,"mem":[...],"door_in":...,...}` |
| `AT+B CONTACTS` | contact list (empty in single-station setups) |
| `AT+B CHECKSIP 2` | current SIP status text |
| `AT+B CHECKSIP 1 s=… u=… p=…` | test SIP connection with given creds |

### Filtered commands (need internal call / MQTT)

| Command | Triggered by | Notes |
|---|---|---|
| `AT+B UART monitor / call / hook / hang / unlock / …` | Lua → avlink → uart2d, or MQTT-handler → avlink | TCP-external is rejected |
| `AT+B RELOAD [target]` | Lua handlers after DB changes | works from external TCP — useful for config refresh |
| `AT+B KEY` | `/api/key` GET | silent trigger, unknown purpose |
| `AT+B ELOCK <action>` | `/api/elock/<action>` GET | door-relay control (alternate path to `UART unlock`) |
| `AT+B RECORD <n>` | recording start/stop | |
| `AT+B UPGRADE <n>` | firmware upgrade | |
| `AT+B check ip` | network check | |

---

## 3. mimedia — TCP port 10600 (RTSP server backend)

Owns RTSP on port 554 (Hisilicon-backed encoder). Internal-only AT+B (`AT+B VIDEO …`, `AT+B RTSP …`, `AT+B MJPG …`) — these go from avlink to mimedia, not from outside. **Read-only access**: pull RTSP from `rtsp://admin:admin@<IP>/live.sdp` (H.264 Baseline, 640×480, 25 fps, no audio).

The stream only contains real frames while a `monitor` or `call` session is active on the bus — otherwise the encoder is idle and you get a blue "no signal" frame.

---

## 4. pjsua — SIP stack (TCP/UDP 5060/5061/33333)

Build is HHG/EGB-customised PJSIP, identified by User-Agent `VCP01` and binary tag `"Systec outdoor and management central"`.

- Responds to OPTIONS from any source (sanity check: `printf "OPTIONS sip:probe@<IP> SIP/2.0\r\n…" | nc -u <IP> 5060`)
- Incoming INVITEs go through `check_outdoor_incoming_call` which validates caller IP and address — non-registered callers are immediately hung up (`on_incoming_call hangup call.`)
- The GW is registered with `de.ilifestyle-cloud.com` as `sip_id` (in our test setup: `s00cAAAAAAAAAAAA`)

For local integration we don't talk SIP — `uart2d:10087` is much simpler.

---

## 5. Web Admin REST API — HTTP port 80

Vue-based SPA. All `/api/*` endpoints are OpenResty Lua handlers (`/customer/lua/<name>.lua`) that translate REST calls into AT+B commands sent to `avlink:10086`.

### Authentication

```
POST /api/login
Content-Type: application/json
Body: {"name": "admin", "password": "admin"}

Response sets cookie `token=<jwt>` and returns:
{"status": 0, "token": "<jwt>"}
```

⚠️ Body field is **`name`** (not `username`). JWT is HS256-signed; payload contains MAC, model, user-group.

Subsequent calls need the cookie or `Cookie: token=<jwt>` header.

### Endpoint catalogue

| Method | Endpoint | Description | Internal AT+B |
|---|---|---|---|
| POST | `/api/login` | login | (DB lookup) |
| POST | `/api/logout` | logout | — |
| GET, POST | `/api/account` | iLifestyle cloud account | `RELOAD` |
| GET, POST | `/api/avSetting` | A/V codec settings | `RELOAD` |
| GET, POST | `/api/video` | stream URLs (rtsp / rtmp / p2p_server / transfer mode) | `RELOAD` + `RELOAD himedia` |
| GET, POST | `/api/sip` | SIP server + `online` flag | `RELOAD` + `APPLICATION` |
| GET, POST | `/api/p2p` | contacts list | `CONTACTS` |
| GET, POST | `/api/relay` | relay durations | `RELOAD` |
| GET | `/api/elock/<action>` | door-relay trigger | `ELOCK <action>` |
| GET | `/api/key` | silent trigger | `KEY` |
| GET | `/api/system/status` | sensor values (uptime, mem, IO pins) | `SYSTEM` |
| GET, POST | `/api/device` | device info / time zone | `RELOAD` + `RELOAD NTP` |
| GET, POST | `/api/network` | eth0 + DNS config | `RELOAD eth0 …` |
| GET, POST | `/api/wifi` | WiFi credentials | `RELOAD wifi` |
| GET | `/api/wifilist` | available APs | `RELOAD wifi` |
| GET, POST | `/api/parameter` | ringtime, volumes, elock hold-time | `RELOAD` |
| GET, POST | `/api/purpose` | operating mode | `RELOAD` |
| POST | `/api/cloudDevice` | iCloud binding | `RELOAD` + `RELOAD icloud` |
| POST | `/api/getCallList` | call/contact list query | DB read |
| POST | `/api/addCallList` | add contact | `RELOAD callList` |
| POST | `/api/updateCallList` | update contact | `RELOAD callList` |
| POST | `/api/delCallList` | remove contact | `RELOAD callList` |
| POST | `/api/cleanCallList` | clear all contacts | `RELOAD callList` |
| POST | `/api/getConnect` | current SIP status | `CHECKSIP 2` |
| POST | `/api/testConnect` | test SIP creds | `CHECKSIP 1` |
| POST | `/api/getServerList` | SIP servers list | DB read |
| GET | `/api/mac` | device MAC | — |
| GET | `/api/backup` | config backup download | — |
| POST | `/api/apk` | provisioning / setup | `RELOAD wifi` + `RELOAD address` |
| POST | `/api/userKey` | user-key settings | `RELOAD` |
| POST | `/api/upload` | file upload (logo, etc.) | — |

### Useful payloads

```json
// GET /api/system/status response
{
  "version": "4.1.11",
  "uptime": 12345,
  "mem": [91560, 27596],
  "loads": [12.16, 12.14, 11.4],
  "door_in": 69,    // analog ADC value of doorbell button pin
  "door_out": 83,
  "io0_in": 51,
  "io0_out": 77,
  ...
}

// GET /api/video response
{
  "enable": true,
  "rtsp": "rtsp://%s/live.sdp",
  "rtmp": "rtmp://rtmp.de.ilifestyle-cloud.com/live/<stream-key>",
  "p2p_server": "p2p.de.ilifestyle-cloud.com",
  "transfer": 1                  // 0=P2P, 1=RTMP, 2=local
}

// POST /api/elock/open — opens the door
// No body needed
```

---

## 6. iLifestyle Cloud API — HTTPS

Server `de.ilifestyle-cloud.com` (Alibaba Cloud, IP `198.51.100.50`). Used by the device and the official app. Not required for this integration — documented for reference.

### Login

```
POST https://de.ilifestyle-cloud.com/api/login
Body: {
  "user_id": "<email>",
  "password": "<password>",
  "device_type": 6,
  "device_model": "AVL20P",
  "device_id": "<MAC>"
}
Response: {"code": 0, "token": "<jwt>", "id": "<user_id>", "city_id": "de"}
```

### Device info

```
GET https://de.ilifestyle-cloud.com/api/device?id=<MAC>
Header: Authorization: <jwt>
Response: { "sip_id", "password" (= device-token, not user password!),
            "mqtt_server", "sip_server", "rtmp_server", "p2p_server",
            "binding_id", "info": {"firmware", "online", "sip", "mqtt"} … }
```

### Device key (binding code = QR for app)

```
GET    https://de.ilifestyle-cloud.com/api/v2/devices/<MAC>/keys
GET    https://de.ilifestyle-cloud.com/api/v2/devices/<MAC>/keys/<id>
GET    https://de.ilifestyle-cloud.com/api/v2/devices/<MAC>/keys/<id>/users
PUT    https://de.ilifestyle-cloud.com/api/v2/devices/<MAC>/keys/<id>/users/0?delete_all=1
```

Each device has a `key` record (id, bus-address, `binding_code` like `42.examplecodeXYZ`, `user_limit: 8`, `user_count`). Users register with this code through the app.

### MQTT (cloud)

- Brokers: `de.ilifestyle-cloud.com:1883` (plain) and `:8883` (TLS)
- ClientID = device MAC for the gateway
- Username = device MAC, Password = JWT from `/api/login` (strict match: JWT `did` field must equal ClientID)
- Subscribe topic for the GW: its own MAC (e.g. `AABBCCDDEEFF`)
- Wake payload published by the app:
  ```json
  {"action": "monitor", "from": "<client_uuid>", "tag": "<id>",
   "ctrl": "1", "key_index": 1, "duration": 60}
  ```
- GW response published to `topic = from-uuid`:
  ```json
  {"tag": "<id>", "action": "monitor", "response": "ok" | "err" | "finish"}
  ```

App authentication uses a different (HMAC-signed Alibaba-IoT-style) flow that we did not reverse-engineer — irrelevant since the local uart2d path makes cloud integration unnecessary.

---

## 7. Telnet — port 23 (no authentication ⚠️)

Open BusyBox root shell. **Anyone on the LAN gets `root@/`.** Used by this integration for log tailing:

```bash
nc <GW_IP> 23
# → /  #
tail -F /customer/share/usr-log.log
```

`/customer/share/usr-log.log` is updated in real-time by all daemons. Key event patterns:

| Pattern | Meaning |
|---|---|
| `[on_receive_monitor avlink-server.c:818]: on_receive_monitor: state=<n>, from=<uuid>, key_index=<id>` | Live-view started (MQTT-triggered) |
| `AT_UART_MONITOR response=ok` | Monitor command accepted |
| `AT_UART_MONITOR response=err` | Monitor command rejected (wrong source / params) |
| `AT_UART_HANG state=<n> self->key_index=<id>` | Call/monitor ended |
| `AT_UART_CALL key_index=<n>, outdoor_addr=<n>` | Outgoing call to outdoor station |
| `AT_UART_RINGBACK state=<n>, response=ok` | Ringback received from outdoor station |
| `call_btn_trigger key_index=<n>` | Doorbell button pressed at outdoor station! |
| `on_incoming_call state=<n>, callID=<n>, …` | Incoming SIP call (from cloud/app) |
| `[mqtt_client_message_callback]: topic=<MAC>, payload={…}` | Inbound MQTT message |

Other log files at `/customer/share/`:
- `gst-p2p-log.log` — GStreamer P2P streaming details (cloud-side)
- `firmware_upgrade.lua.log` — firmware update activity

---

## 8. SSH — port 22 (dropbear, empty password)

```bash
ssh -oHostKeyAlgorithms=+ssh-dss \
    -oKexAlgorithms=+diffie-hellman-group1-sha1 \
    root@<GW_IP>
# password: empty (just hit enter)
```

Modern OpenSSH (e.g. macOS 12+) refuses `ssh-dss` outright. Use `paramiko` from Python, or the Telnet path.

---

## 9. Filesystem layout (for reference)

```
/customer/
├── app/sbin/
│   ├── avlink           80 KB    — central app daemon, MQTT client, AT+B dispatcher
│   ├── uart2d          734 KB    — UART/Bus bridge daemon (port 10087)
│   ├── mimedia         859 KB    — Hisilicon RTSP server
│   ├── pjsua           922 KB    — PJSIP user-agent
│   ├── discovery        26 KB    — UPnP-style device discovery
│   └── custode2.lua     30 KB    — Lua service daemon (port 60000), config reloads
├── lua/                          — 35 OpenResty Lua handlers for /api/*.lua
└── share/
    ├── avl20.db                  — SQLite (config, user, sipServer, callList)
    ├── avl20.sql                 — DB factory-reset template
    ├── usr-log.log     128 KB+   — primary log file (real-time)
    ├── gst-p2p-log.log           — P2P GStreamer log
    ├── mqtt-client.socket        — Unix domain socket (avlink intra-thread IPC)
    ├── config.lua                — Lua config defaults
    └── firmware_upgrade.lua      — firmware update logic
```

---

## Database schema (`/customer/share/avl20.db`)

```sql
CREATE TABLE config(name text, item text);
-- key/value config (sip, video, wifi, av_link, purpose, …)
-- av_link.item = {"button":"2"}  ← GW's own bus address (2 in our test setup)

CREATE TABLE user(name text, password text, grp text);
-- 'admin'/'admin'/0, 'superadmin'/'super1314'/0, 'device'/'device'/1

CREATE TABLE sipServer(id, server, account, password, callee);
-- additional SIP server definitions

CREATE TABLE callList(
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    callNo    TEXT,
    name      TEXT,
    address   TEXT,
    key       TEXT UNIQUE,   -- bus address (总线地址)
    userType  INTEGER,       -- 0=user, 1=门口机 outdoor, 2=门卫机 guard
    callType  INTEGER,       -- 0=bus, 1=ip, 2=sip
    ipAddr    TEXT,
    serverId  TEXT,
    enable    INTEGER,
    shareCode TEXT
);
-- default entry: id=1, callNo='1', key='', userType=2, callType=0
```

The `callList.id` is what gets used as `key_index` in `AT+B UART …` commands. Each entry is one outdoor station / pair-able peer.

---

## Cross-references

- [`architecture.md`](architecture.md) — daemon process map and IPC
- [`api-rest.md`](api-rest.md) — detail on each `/api/*` REST handler
- [`api-cloud.md`](api-cloud.md) — iLifestyle cloud endpoints
- [`security.md`](security.md) — full security audit
- [`reverse-engineering.md`](reverse-engineering.md) — how we discovered all this
