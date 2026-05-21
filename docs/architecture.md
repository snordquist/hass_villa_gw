# Villa GW Architecture

How the daemons fit together. Useful for debugging and for understanding why this integration takes the path it does.

## Hardware

- **SoC**: Hisilicon ARM 32-bit (ARMv7 / `armhf`, BusyBox 1.20.2, kernel 3.x)
- **UART**: `/dev/ttyS1` connects the SoC to the Villa-Bus controller chip
- **Network**: eth0 (LAN) + optional WiFi (wlan0, off by default in our setup)
- **Camera/Audio**: Hisilicon `Mi_Common_*` SDK, RTSP via `mimedia` daemon
- **Bus topology**: HHG/EGB Villa-Bus, 2-wire, multiple devices addressed by 1-byte address. PSU has two trunks (BUS 1 + BUS 2); one device exists per address per trunk.

## Process tree

```
linuxrc (init, PID 1)
├── /etc/init.d/discovery       — UPnP-style LAN discovery
├── /etc/init.d/networking      — interface setup
├── /etc/init.d/nginx           — nginx + OpenResty Lua (port 80, web admin)
├── /etc/init.d/monitor         — runs monitor.lua + custode2.lua + avlink
│   ├── monitor.lua (PID ?)     — heartbeat, log rotation
│   ├── custode2.lua (PID 725)  — config-reload service, port 127.0.0.1:60000
│   └── avlink                  — central app daemon (multi-threaded)
│       ├── avlink-master       — main thread, AT+B dispatcher (TCP 10086)
│       └── mqtt-client thread  — libmosquitto, talks to cloud MQTT
├── uart2d                      — UART/Bus bridge (TCP 10087, /dev/ttyS1)
├── mimedia                     — RTSP server (TCP 554, 10600), Hisilicon encoder
├── pjsua                       — SIP user-agent (TCP/UDP 5060, 5061, 33333)
├── telnetd                     — root shell on port 23 (NO AUTH ⚠️)
└── dropbear                    — SSH on port 22 (empty root password ⚠️)
```

`avlink`, `uart2d`, `mimedia`, `pjsua` are all C binaries in `/customer/app/sbin/`. The `monitor.lua` script supervises them and restarts on crash.

## Port map (LAN-reachable)

| Port | Daemon | Purpose | Auth |
|---|---|---|---|
| 22 | dropbear | SSH | empty password (ssh-dss only) |
| 23 | telnetd | Telnet | **none** |
| 80 | nginx+OpenResty | Web admin + REST API | session cookie / JWT |
| 554 | mimedia | RTSP video | Basic auth (`admin/admin`) |
| 1936 | mimedia | RTMPS | — |
| 5060/5061 | pjsua | SIP/SIPS | — |
| 10086 | avlink | AT+B command dispatcher | source-filtered |
| 10087 | uart2d | UART/Bus bridge | **none** (the magic door) |
| 10600 | mimedia | RTSP backend | — |
| 33333 | pjsua | SIP RTP relay | — |

`127.0.0.1:60000` (custode2 IPC) is localhost-only.

## Data-flow diagrams

### Outbound: HA wants to wake the camera

```
HA Custom Integration
        │
        │ TCP open + send "AT+B UART monitor 1 1 30\r\n"
        ▼
[ uart2d :10087 ]
        │ parse → userial_send_monitor_call_msg(src=2, dst=1)
        │ build bus frame
        ▼
[ /dev/ttyS1 ] → Villa-Bus → outdoor station → camera ON
        │
        │ ← video frames (encoded H.264) over bus
        ▼
[ mimedia (Hisilicon decoder) ]
        │
        ▼
[ RTSP :554/live.sdp ] → HA camera entity (ffmpeg)
```

### Inbound: doorbell pressed at outdoor station

```
[ outdoor station ] → bus frame → /dev/ttyS1
        │
        ▼
[ uart2d ] ← decodes, sends event to avlink via internal IPC
        │
        ▼
[ avlink ]
        │
        ├── publish MQTT message (Cloud)   → iLifestyle app receives push
        │
        ├── log "call_btn_trigger key_index=N"  → /customer/share/usr-log.log
        │
        └── update DB / call state
                │
                ▼
        HA Custom Integration ← parses log via Telnet tail → fires HA event
```

### Native: live-view via iLifestyle app

```
iLifestyle App
        │ MQTT publish → topic = GW MAC, payload = {"action":"monitor", "key_index":1, "duration":60}
        ▼
[ de.ilifestyle-cloud.com MQTT broker ]
        │
        ▼
[ GW's MQTT client thread (inside avlink) ]
        │ on_received_mqtt_message → on_receive_monitor
        ▼
[ avlink-master ]
        │ AT+B UART monitor 1 1 60 → :10087
        ▼
[ uart2d ] → bus → outdoor station camera ON
        │
        ▼
[ mimedia RTSP / RTMP-push to cloud ] → app receives stream
```

## Why we bypass MQTT and TCP-avlink

| Path | Tried | Result |
|---|---|---|
| Direct MQTT publish to cloud broker | yes | Auth requires Alibaba-IoT HMAC sign + ClientID=MAC collision with running GW |
| Direct AT+B to avlink:10086 from LAN | yes | Filter rejects non-MQTT/non-localhost source: `response=err` |
| MITM via DNS override (point `de.ilifestyle-cloud.com` to HA) | not tried | Possible but heavy: requires TLS cert + bridging back to real cloud |
| Override GW's `mqtt_server` to local mosquitto | not tried | Would break iLifestyle app, also not firmware-update-robust |
| **Direct AT+B to uart2d:10087 from LAN** | **yes** | **Works.** No filter, no auth, no cloud dependency. |

uart2d is the sweet spot — far enough down the stack to be free of filtering, high enough that we don't have to reverse the bus frame format.

## Why event-driven Telnet tail instead of polling

The DB-based call list is updated only AFTER the event is fully processed. Polling `/api/getCallList` every few seconds would miss short events and add 0–5 s of latency. The log file `/customer/share/usr-log.log` is written synchronously by every daemon — `tail -F` gives us events <100 ms after they happen. Telnet is open without auth, so we don't need credential management.

If Telnet gets disabled in a future firmware, fallback options:
- SSH-based tail (paramiko)
- SNMP-style polling via `AT+B SYSTEM` (less ideal — only counters, no events)
- DB-trigger polling (`SELECT * FROM callList ORDER BY id DESC LIMIT 1`)
