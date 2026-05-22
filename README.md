# Villa GW — Home Assistant Custom Integration

[![hacs_custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![GitHub Release](https://img.shields.io/github/v/release/snordquist/hass_vp_villa)](https://github.com/snordquist/hass_vp_villa/releases)
[![License](https://img.shields.io/github/license/snordquist/hass_vp_villa)](LICENSE)

Home Assistant integration for **HHG/EGB Villa GW** intercom gateways (model AVL20P, internal name ACP-03). Bypasses the iLifestyle cloud completely — runs fully local, sub-100ms response, no MQTT credentials needed.

> ## ⚠️ Coordinated security disclosure in progress
>
> Several severe security defaults in the Villa GW firmware (4.x) were
> identified during the reverse-engineering work that made this integration
> possible. The vendor (**HHG GmbH**) was notified on **2026-05-22**. Detailed
> reproduction steps have been temporarily withdrawn from this repository.
>
> - Disclosure status & summary: [`docs/security.md`](docs/security.md)
> - Planned full public disclosure: **2026-08-20** (90 days), unless an
>   agreement with the vendor is reached.
> - **Deployment advice:** treat the Villa GW as an untrusted IoT device.
>   Put it on a dedicated VLAN, firewall it from the rest of your LAN, change
>   any default credentials, and read the disclosure summary before exposing
>   the device to a shared network.

## Why this exists

The HHG/EGB Villa GW lets you bridge a Villa-Bus intercom (door station + indoor monitor) to IP. Out of the box it requires the iLifestyle cloud (Alibaba-IoT-Pattern HMAC auth, no local API documented). After reverse-engineering the firmware, this integration talks **directly to the internal Bus daemon** (`uart2d`) for control and **tails the local management shell** for live events — no cloud, no MQTT-Bridge.

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
- HHG/EGB Villa GW V3.0 (AVL20P) reachable on the LAN by the Home Assistant host
- Default firmware (4.x). Please read [Security](docs/security.md) before deploying.

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
- **Web admin username / password** (configured on the device's web UI)
- **Outdoor station Bus address** (default `1` — see [protocol docs](docs/protocol.md))

The integration auto-discovers the camera RTSP URL, opens a persistent local event channel, and registers all entities.

## How it works

```
  ┌───────────────────────────────────────────────────────────────┐
  │  Home Assistant                                                │
  │                                                                │
  │  ┌──────────────────┐    persistent     ┌──────────────────┐ │
  │  │  Event Listener  │ ◄────── tail ─────┤  Villa GW logs   │ │
  │  │  (parse logs)    │                   │                  │ │
  │  └────────┬─────────┘                   └──────────────────┘ │
  │           │                                                   │
  │           ▼                                                    │
  │  ┌──────────────────┐    on-demand      ┌──────────────────┐ │
  │  │  HA Entities     │ ──── TCP ────────►│  Villa GW        │ │
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

This gateway ships with multiple severe defaults. The detailed audit has been
temporarily withdrawn while we coordinate disclosure with the vendor (see
banner at the top of this README). The short version:

- Do NOT expose the Villa GW to the internet.
- Treat it as **untrusted** even within your LAN — place it on a dedicated
  VLAN or IoT subnet, firewalled away from other devices.
- Change the web admin password.
- Do not reuse the iLifestyle cloud password anywhere else.

See [`docs/security.md`](docs/security.md) for the disclosure summary and
the recommended hardening steps.

## License

MIT. The reverse-engineering work is original; the protocol details are documented for interoperability only. No HHG/EGB/Systec firmware code is included.

## Credits

- HHG Elektrotechnik GmbH — manufacturer of the Villa GW
- Reverse-engineering: see [`docs/reverse-engineering.md`](docs/reverse-engineering.md)
