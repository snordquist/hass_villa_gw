# Villa GW

Local-first Home Assistant integration for **HHG/EGB Villa GW (AVL20P)** intercom gateways. Bypasses the iLifestyle cloud — sub-100ms doorbell events, silent live-view from HA, door control via bus. iLifestyle smartphone app keeps working in parallel.

## Features

- 📹 Live camera (RTSP H.264)
- 🔔 Doorbell event with sub-100ms latency (event-driven via Telnet log tail, no polling)
- 👁 Silent live-view (no ringtone at the outdoor station)
- 🔓 Door lock (bus-side relay)
- 📞 Call accept / hangup
- 📊 Status sensors (uptime, memory, firmware, stream mode)

## Setup

After install via HACS: Settings → Devices & Services → Add Integration → **Villa GW**.

You need:
- Gateway IP
- Web admin password (default `admin`)
- Outdoor station bus address (usually `1`)

## Reverse-engineering notes

See the [protocol docs](https://github.com/snordquist/hass_vp_villa/blob/main/docs/protocol.md) — the integration talks directly to the `uart2d` daemon on TCP port 10087, no cloud, no MQTT, no app reverse-engineering.

⚠️ Security: this integration depends on the device's open Telnet root shell, which is a factory-default in this hardware. Read [`docs/security.md`](https://github.com/snordquist/hass_vp_villa/blob/main/docs/security.md) before deploying.
