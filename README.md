# Villa GW — Home Assistant Custom Integration

[![hacs_custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![GitHub Release](https://img.shields.io/github/v/release/snordquist/hass_vp_villa)](https://github.com/snordquist/hass_vp_villa/releases)
[![License](https://img.shields.io/github/license/snordquist/hass_vp_villa)](LICENSE)

Home Assistant integration for **HHG/EGB Villa GW** intercom gateways (model AVL20P, internal name ACP-03). Bypasses the iLifestyle cloud completely — runs fully local, sub-100ms response, no MQTT credentials needed.

## Why this exists

The HHG/EGB Villa GW lets you bridge a Villa-Bus intercom (door station + indoor monitor) to IP. Out of the box it requires the iLifestyle cloud (Alibaba-IoT-Pattern HMAC auth, no local API documented). After reverse-engineering the firmware, this integration talks **directly to the internal Bus daemon** (`uart2d`, TCP port 10087) and **tails the unauthenticated Telnet root shell** for live events — no cloud, no MQTT-Bridge.

The iLifestyle app stays fully functional alongside.

## Features

- 📹 **Live camera** (RTSP) — H.264 Baseline, 640×480, 25 fps
- 🔔 **Doorbell event** (binary sensor) — sub-100ms latency, event-driven (no polling)
- 👁 **Live-View on demand** (button) — silent "monitor" mode, wakes outdoor station's camera without ringing
- 🔓 **Door lock** (lock entity) — opens the door relay via Bus
- 📞 **Call control** (buttons) — hook (accept), hang (decline / end)
- 🔄 **Camera switch** (button) — cycle through outdoor stations
- 📸 **Snapshot** (button + sensor) — single MJPG snapshot
- 📊 **System status** (sensors) — uptime, memory, SIP/MQTT/RTMP cloud status, door-pin ADC

## Requirements

- Home Assistant 2024.1 or newer
- HHG/EGB Villa GW V3.0 (AVL20P) on the LAN, reachable via TCP:23 (telnet) and TCP:10087 (uart2d)
- Default firmware (4.x) with Telnet root shell open (factory default, see [Security](docs/security.md))

## Installation

### HACS (recommended)

1. HACS → Integrations → ⋮ → Custom Repositories
2. Add repo URL: `https://github.com/snordquist/hass_vp_villa`, category **Integration**
3. Install **Villa GW**, restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → search "Villa GW"

### Manual

```bash
cd /config
git clone https://github.com/snordquist/hass_vp_villa.git
cp -r homeassistant-villa-gw/custom_components/villa_gw custom_components/
# Restart Home Assistant
```

## Configuration

UI-driven via Config Flow. You will be asked for:

- **Host** (IP or hostname of the Villa GW, e.g. `192.0.2.10`)
- **Web username / password** (default `admin / admin`)
- **Outdoor station Bus address** (default `1` — see [protocol docs](docs/protocol.md))

The integration auto-discovers the camera RTSP URL, opens a persistent Telnet tail for event delivery, and registers all entities.

## How it works

```
  ┌───────────────────────────────────────────────────────────────┐
  │  Home Assistant                                                │
  │                                                                │
  │  ┌──────────────────┐    persistent     ┌──────────────────┐ │
  │  │  Event Listener  │ ◄── telnet tail ──┤  Villa GW :23    │ │
  │  │  (parse logs)    │                   │  /customer/share/│ │
  │  └────────┬─────────┘                   │  usr-log.log     │ │
  │           │                              └──────────────────┘ │
  │           ▼                                                    │
  │  ┌──────────────────┐    on-demand      ┌──────────────────┐ │
  │  │  HA Entities     │ ──── TCP ────────►│  Villa GW :10087 │ │
  │  │  (button/lock)   │   AT+B UART ...   │  (uart2d)        │ │
  │  └──────────────────┘                   └──────────────────┘ │
  │                                                                │
  │  ┌──────────────────┐    RTSP-pull      ┌──────────────────┐ │
  │  │  Camera Entity   │ ◄─────────────────┤  Villa GW :554   │ │
  │  │  (ffmpeg)        │                   │  (mimedia)       │ │
  │  └──────────────────┘                   └──────────────────┘ │
  └───────────────────────────────────────────────────────────────┘
```

See [`docs/`](docs/) for protocol details, reverse-engineering notes, and a security review.

## Compatibility

Tested on:
- **AVL20P** (HHG / EGB Villa GW V3.0), firmware 4.1.11, internal model `ACP-03`

The gateway is sold under different brand labels (HHG, EGB, Systec). If your device is rebranded but model number `AVL20P` matches, this integration should work. If you have other firmware or hardware revisions, please open an issue.

## Security warning

This gateway ships with multiple severe defaults that this integration relies on:

- Telnet (port 23) is open with **root shell and no authentication** in the LAN
- SSH (port 22, dropbear) accepts empty password for `root`
- Web admin uses default `admin / admin`
- Cloud account password (iLifestyle) is stored in cleartext on the device

Do NOT expose the Villa GW to the internet. Treat it as untrusted on shared networks. See [`docs/security.md`](docs/security.md) for the full audit and hardening recommendations.

## License

MIT. The reverse-engineering work is original; the protocol details are documented for interoperability only. No HHG/EGB/Systec firmware code is included.

## Credits

- HHG Elektrotechnik GmbH — manufacturer of the Villa GW
- Reverse-engineering: see [`docs/reverse-engineering.md`](docs/reverse-engineering.md)
