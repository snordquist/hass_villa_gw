# Villa GW REST API (port 80)

Complete catalogue of the on-device REST API. All endpoints live under `/api/`. The Vue web admin SPA uses these.

Authentication: cookie `token=<JWT>` returned by `POST /api/login`. Most write endpoints also check `ngx.ctx.group == 0` (admin group).

For a higher-level overview see [`protocol.md`](protocol.md). This page is for when you need byte-exact request/response shapes.

## Authentication

### POST /api/login

```json
Request:
{"name": "admin", "password": "admin"}

Response:
{"status": 0, "token": "eyJhbGciOiJIUzI1NiIs…(truncated)"}

Cookies set:
token=<JWT>
```

Status codes:
- `0` = ok, token returned
- `2` = wrong credentials
- `3` = malformed request (wrong field name, etc. — note the field is `name`, not `username`)

### POST /api/logout

Invalidates the session cookie.

## Identity & Health

### GET /api/mac

```json
{"status": 0, "mac": "AA:BB:CC:DD:EE:FF"}
```

### GET /api/device

Returns time-zone, device-name, language, type, id.

```json
{
  "status": 0,
  "type": "AVL10",
  "id": "0010100ZZ",
  "name": "",
  "address": "",
  "language": "en",
  "timezone": "Europe/Berlin",
  "datetime": "2026-05-21 15:35:58",
  "auto": 1
}
```

### POST /api/device

Update device settings. AT+B `RELOAD` + `RELOAD NTP` triggered.

### GET /api/system/status

`AT+B SYSTEM` proxy. Live sensor data:

```json
{
  "status": 0,
  "version": "4.1.11",
  "uptime": 2812,                  // seconds since boot
  "loads": [12.16, 12.14, 11.4],   // load avg ×100
  "mem": [91560, 27596],           // [total, used] kB
  "flash": [9088, 0],              // [free, ?] kB
  "network": 1,                    // 1 = connected
  "door_in": 69,                   // ADC raw (doorbell input pin)
  "door_out": 83,                  // ADC raw (door relay sense)
  "io0_in": 51, "io0_out": 77,
  "man_in": 84, "factory_in": 53,
  "rs485": 46, "light_in": 49,
  "led": 46, "oled": 44
}
```

Useful for monitoring without polling the bus.

## Network

### GET /api/network

```json
{
  "status": 0,
  "ip": "192.0.2.10",
  "netmask": "255.255.255.0",
  "gateway": "192.168.1.1",
  "dns": "192.168.1.1",
  "mac": "AA:BB:CC:DD:EE:FF",
  "dhcp": true
}
```

### POST /api/network

Update LAN config. AT+B `RELOAD eth0 start/stop` + `RELOAD network`.

### GET /api/wifi

```json
{
  "status": 0,
  "enable": false,
  "ssid": "",
  "password": "",
  "connected": "DISABLED"
}
```

### POST /api/wifi

Configure WiFi. AT+B `RELOAD wifi`.

### GET /api/wifilist

Triggers a scan, returns nearby SSIDs.

## Audio / Video

### GET /api/avSetting

```json
{
  "status": 0,
  "video_format": "H.264",
  "audio_format": "G.711",
  "video_transmission": 1,
  "transmission_pto": 1
}
```

### POST /api/avSetting

Change codecs. AT+B `RELOAD`.

### GET /api/video

```json
{
  "status": 0,
  "enable": true,
  "rtsp": "rtsp://%s/live.sdp",       // %s = device IP
  "rtmp": "rtmp://rtmp.de.ilifestyle-cloud.com/live/<stream-key>",
  "p2p_server": "p2p.de.ilifestyle-cloud.com",
  "transfer": 1                        // 0=P2P, 1=RTMP, 2=local
}
```

### POST /api/video

Change stream URLs and transfer mode. AT+B `RELOAD` + `RELOAD himedia`.

## Call list & contacts

### POST /api/getCallList

```json
Request:
{"page": 1, "perPage": 20}

Response:
{
  "status": 0,
  "total": 1,
  "ret": [
    {"id":1, "callNo":"1", "name":"", "address":"", "key":"",
     "userType":2, "callType":0, "ipAddr":"", "serverId":null,
     "enable":1, "shareCode":""}
  ]
}
```

`callList.id` is the `key_index` used in `AT+B UART …` commands.

### POST /api/addCallList

```json
{"callNo": "2", "name": "Door 2", "address": "",
 "key": "3", "userType": 1, "callType": 0,
 "ipAddr": "", "enable": 1, "shareCode": ""}
```

⚠️ `key` is UNIQUE — duplicates return `status: 5`.

### POST /api/updateCallList

Same body shape + `id`. Updates existing.

### POST /api/delCallList

```json
{"id": <n>}
```

### POST /api/cleanCallList

No body — wipes everything.

## SIP

### GET /api/sip

```json
{
  "status": 0,
  "server": "de.ilifestyle-cloud.com",
  "name": "s00cAAAAAAAAAAAA",       // SIP username
  "password": "XXXXXX",              // SIP password (cleartext!)
  "mqtt_server": "de.ilifestyle-cloud.com",
  "nickname": "Villa GW",
  "contact": "000000",
  "online": true
}
```

### POST /api/sip

Override SIP server. ⚠️ Cloud-bound; changing breaks the iLifestyle app.

### POST /api/getConnect

`AT+B CHECKSIP 2` — returns current SIP status.

```json
{"status": 0, "res": "AT+B CHECKSIP 3 0"}
```

The `res` is the raw AT response. The second-to-last digit is the SIP code (3 = registered, 0 = idle). See PJSIP docs.

### POST /api/testConnect

`AT+B CHECKSIP 1 s=… u=… p=…` — test SIP server with given creds. Useful before commit.

```json
{"server": "...", "account": "...", "password": "..."}
```

### POST /api/getServerList

```json
{"status": 0, "ret": []}    // additional SIP servers
```

## P2P

### GET /api/p2p

`AT+B CONTACTS` — list of P2P contacts (empty in single-station setups).

### POST /api/p2p

Edit P2P contact list. AT+B `RELOAD`.

## Door / Lock

### GET /api/elock/<action>

`AT+B ELOCK <action>` — triggers the door relay via avlink (not via uart2d bus). Common `<action>` values: `open`, `close` — try both, behaviour depends on relay wiring.

```
GET /api/elock/open
→ {"status": 0}
```

### GET/POST /api/relay

Configure relay durations.

```json
{"status": 0, "duration_1": 3, "duration_2": 3}    // seconds
```

## Cloud account

### GET /api/account

⚠️ Returns plaintext cloud password.

```json
{
  "status": 0,
  "account": "user@example.com",
  "password": "<PLAINTEXT PASSWORD>",
  "name": "Villa GW",
  "server": "de.ilifestyle-cloud.com",
  "token": "<JWT>"
}
```

### POST /api/account

Set new cloud credentials. AT+B `RELOAD`.

### POST /api/cloudDevice

Toggle iCloud binding (alternative cloud backend). AT+B `RELOAD` + `RELOAD icloud`.

### POST /api/updateCloudEnable

Enable/disable cloud sync.

## Provisioning / setup

### POST /api/apk

The full setup endpoint — used during initial bind. Sets WiFi, cloud account, purpose, button address, and clears callList. Triggered by the iLifestyle app during pairing.

```json
{
  "ssid": "...", "psk": "...",
  "server": "de.ilifestyle-cloud.com",
  "account": "...", "password": "...",
  "name": "Villa GW",
  "button": "2",                 // bus address of the GW itself
  "purpose": 0, "bindSelf": 1
}
```

⚠️ Don't call this from external code without understanding the side effects.

### POST /api/upload

Multipart upload (logo, etc.).

### POST /api/userKey

`AT+B RELOAD` — saves a `key_setting` JSON blob to DB.

## Miscellaneous

### GET /api/key

`AT+B KEY` — silent trigger, unknown effect. We didn't reverse it but it's a clean no-op call you can use for liveness checks.

### GET /api/test

Test endpoint, behaviour varies. Useful for sanity checking.

### GET /api/backup

Downloads a tarball of the config files.

### POST /api/sync / POST /api/autoSync

Trigger cloud sync (the GW logs into the iLifestyle cloud and refreshes its config). Implemented in `/customer/lua/autoSync.lua`.

### POST /api/address

`AT+B RELOAD address` — UART daemon restart.

### POST /api/purpose

Change operating mode (P2P vs RTMP vs local). AT+B `RELOAD`.

```json
{"state": 0, "purpose": 0, "bindSelf": 1}
```

## Status codes (Lua handlers)

Most handlers return:

```json
{"status": 0}   // ok
{"status": 1}   // generic error
{"status": 2}   // not authorized (not group 0)
{"status": 3}   // bad request / unhandled method
{"status": 4}   // duplicate (callNo)
{"status": 5}   // duplicate (key)
```

## Lua handler source

All handlers live at `/customer/lua/<name>.lua` on the device. They follow the same shape:

```lua
local sock = ngx.socket.tcp()
sock:connect("127.0.0.1", 10086)        -- avlink
sock:send('AT+B SOMECOMMAND\r\n')
local response = sock:receive()         -- or fire-and-forget
sock:close()
```

DB-only handlers use `lsqlite3` to talk to `/customer/share/avl20.db`.

If you need to add new endpoints, dropping a Lua file in `/customer/lua/` + adding the nginx route in the (currently empty for us) `/etc/nginx/conf.d/*.conf` is enough — but it's not firmware-update-robust.
