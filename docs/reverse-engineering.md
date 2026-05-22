# Reverse Engineering Notes

How this integration came together. Useful if you need to port to a different firmware version or troubleshoot.

## Tools used

- `nc` (BusyBox netcat) — TCP send/receive, no Unix-socket support
- `base64` (on the GW) — dumping binaries via Telnet pipe
- `strings`, `objdump` (host) — analysing the ARM binaries
- `paho-mqtt` (Python) — testing cloud MQTT auth variations
- Chrome DevTools (browser MCP) — inspecting the Vue web UI's network calls

## Methodology

### Step 1 — reconnaissance from the LAN

```bash
nmap -p- 192.0.2.10
# → 22 (ssh), 23 (telnet), 80 (http), 554 (rtsp), 5060/5061 (sip),
#   10010 (rxapi), 10086 (avlink), 10087 (uart2d), 10600 (mimedia),
#   33333 (sip-rtp)
```

The combination of 554 + custom 10086/10087 ports made it clear this was an embedded IP intercom. Web UI title `AVL20P` and the iobroker forum thread [#46995](https://forum.iobroker.net/topic/46995/einbindung-sprechanlage-hgg-villa) confirmed: HHG/EGB Villa GW, intercom gateway.

### Step 2 — web admin

The Vue SPA's JS bundles (e.g. `/js/app.*.js`) revealed all the `/api/*` endpoints — they're literal string constants. Most endpoints just translate to `AT+B …` commands sent to `127.0.0.1:10086`. *Login-flow details and credentials are part of the withdrawn security disclosure — see [`security.md`](security.md).*

### Step 3 — local shell

A management shell on the device is reachable in the LAN segment. *Specific access details are withheld pending coordinated disclosure — see [`security.md`](security.md).* Once on the device, the relevant artefacts are:

- `ls /customer/app/sbin/` → daemons (`avlink`, `uart2d`, `mimedia`, `pjsua`, `custode2.lua`)
- `cat /customer/lua/<endpoint>.lua` → REST handlers (they call `AT+B` over TCP)
- `tail /customer/share/usr-log.log` → live events

### Step 4 — collect Lua + binaries for analysis

Standard offline-RE workflow against the firmware tarball (vendor publishes update packages on hhg-elektro.de). Combined with the on-device sources above, we get all 35 REST handlers + the custode2 daemon + the 4 native binaries (avlink, uart2d, mimedia, pjsua) + the SQLite DB.

### Step 5 — strings analysis

```bash
strings uart2d | grep -E '^AT\+B'
# → AT+B UART monitor %s
#   AT+B UART call %d %d
#   AT+B UART hook %d
#   AT+B UART unlock %d
#   …
```

Combined with format-string log lines like `userial_send_monitor_call_msg: src_addr = %d, dst_addr = %d`, we mapped each AT+B variant to its bus action.

### Step 6 — the dead-end: MQTT

Initial hypothesis: wake the camera by replaying the MQTT message the iLifestyle app sends.

```
# In usr-log.log when the app triggers live view:
[mqtt_client_message_callback]: topic=AABBCCDDEEFF,
  payload={"action":"monitor", "from":"<uuid>", "key_index":1, "duration":60}
```

Tried connecting to `de.ilifestyle-cloud.com:1883` with the GW's credentials. Auth pattern looked Alibaba-IoT-style (HMAC-signed username/password). With combinations of ClientID/User/Password we found:

- ClientID = MAC + Password = JWT (from `/api/login`) → **accepted** (but kicks the real GW; MQTT spec disallows duplicate ClientIDs)
- Any other ClientID + same Password → rejected

So the broker validates that JWT's `did` field matches the ClientID. The app must use a different login flow (probably with a different `device_type` mapped to a different device_model whitelist on the server — we couldn't find the accepted values without further rate-limited probing).

### Step 7 — the win: uart2d as the local control channel

After the user pointed out that "the GW just receives a signal, then must talk hardware to a /dev/...", we re-read the strings dump of `uart2d`. The relevant format strings are:

```
AT+B UART monitor %s
parse monitor param = %c, times = %d
userial_send_monitor_call_msg: src_addr = %d, dst_addr = %d
```

uart2d accepts AT+B-style commands and is what the integration uses to wake the camera on demand. *Exact network exposure and command vectors that touch the bus relays are part of the withdrawn security disclosure — see [`security.md`](security.md).*

### Step 8 — event tail

While reading the binary strings we noticed `/customer/share/usr-log.log` was 128 KB and growing. Every daemon writes to it synchronously. Tailing it via Telnet (`tail -F /customer/share/usr-log.log`) gives us:

```
2026-05-21 17:54:28.933 INFO  [on_receive_monitor avlink-server.c:818]: \
  on_receive_monitor: state=1, from=23e2…, key_index=1
2026-05-21 17:54:28.934 INFO  [response_mqtt_message …]: \
  send mqtt message: ret=0, remote_topic=…, message={"action":"monitor","response":"ok"}
```

Pattern-matching these lines gives us doorbell-rang / live-view-started / call-ended events with <100 ms latency. No polling needed.

## What we did NOT reverse

- The HHG/EGB Villa-Bus wire protocol (uart2d → /dev/ttyS1). We let uart2d handle it.
- The Alibaba-IoT MQTT auth HMAC algorithm. We don't need cloud MQTT.
- The iLifestyle app's HTTPS login flow (app-side device_type / device_model whitelist). Outside scope.
- Anything cryptographic (JWT secret, AES keys). The integration doesn't need to forge any signed payload.

## Tips for future maintainers

- Firmware version is in `AT+B SYSTEM` response — branch on it if HHG changes log formats
- `parse monitor param = %c, times = %d` (uart2d) suggests param could be other chars; we only tested '0' and '1'. Might encode camera selection in multi-station setups.
- `monitor.lua` (in `/usr/sbin/`, not the bus daemon) restarts crashed services every ~10 s — kill it if you're patching live; it'll bring services back
- The DB factory reset is destructive: `sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql` wipes config

## Related work

- iobroker forum thread #46995 — original community discovery of local-management interfaces
- HHG VILLA GW product PDF — confirms RTMP-to-cloud architecture officially
- Eclipse Mosquitto C library — the `avlink` MQTT thread uses libmosquitto symbols (`mosquitto_publish`, `mosquitto_subscribe_callback_set`)
