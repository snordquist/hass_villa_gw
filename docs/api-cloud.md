# iLifestyle Cloud API

The HHG/EGB Villa GW phones home to `de.ilifestyle-cloud.com` (Alibaba Cloud, IP `198.51.100.50`). This page documents the cloud HTTP endpoints we observed, mostly for reference — **this integration does not need them**. The local `uart2d:10087` path is preferred.

If you _want_ to do something cloud-side (e.g. trigger the camera via the smartphone app's MQTT topic from a different network), this is your starting point.

## Endpoints

All paths are HTTPS. Self-signed certs in the chain might require `tls_insecure_set(True)` in some clients.

### POST /api/login

The device's own bootstrap login.

```json
Request:
{
  "user_id": "<email>",           // iLifestyle account email
  "password": "<password>",       // cleartext
  "device_type": 6,               // 6 = AVL20P (and the only value that returned 0)
  "device_model": "AVL20P",       // must match server-side whitelist
  "device_id": "<MAC>"            // 12-char hex, uppercase, no colons
}

Response (HTTP 200):
{
  "code": 0, "message": "OK",
  "id": "u00cAAAAAAAAAAAA",       // assigned uid
  "city_id": "de",
  "token": "<JWT>"
}
```

The JWT (HS256, signed with a key we don't know) has this payload:

```json
{
  "app": 1,                       // probably tenant ID
  "uid": "u00cAAAAAAAAAAAA",
  "ugp": 4,                       // user group (4 = device)
  "did": "AABBCCDDEEFF",          // device id (MAC), exact match required for MQTT
  "dmd": "AVL20P",
  "dtp": 6,
  "iat": 1779368227               // issue time
}
```

Tokens expire — the device's `autoSync.lua` re-runs this login periodically.

#### Error responses

```json
{"code": 104, "message": "Data Error", "error": {"target": "device_model"}}
// → device_model not in whitelist; only "AVL20P" was accepted for device_type=6
```

HTTP 429 = rate limited. The login endpoint is aggressive — wait 60s+ between attempts when probing.

### GET /api/device?id=<MAC>

Fetch device record. Returns the SIP/MQTT/RTMP credentials the device should use.

```json
Headers:
Authorization: <JWT>

Response:
{
  "code": 0, "message": "OK",
  "id": "AABBCCDDEEFF",
  "sip_id": "s00cAAAAAAAAAAAA",
  "name": "Villa GW",
  "video_url": "rtmp://rtmp.de.ilifestyle-cloud.com/live/<stream-key>",
  "video_transfer": 1,
  "type": 6,
  "model": "AVL20P",
  "app": 1,
  "info": {
    "firmware": "4.1.11",
    "hardware": "2.0",
    "online": true,
    "sip": "online",
    "mqtt": "online",
    "rtmp": "online"
  },
  "conf": {"device_button": "2"},
  "password": "XXXXXX",               // device-token, NOT user pw
  "binding_id": 12345,
  "binding_type": 1,
  "binding_key": "",
  "binding_lock": 0,
  "user_id": "u00cAAAAAAAAAAAA",
  "dialplan": "000000",
  "mqtt_server": "de.ilifestyle-cloud.com",
  "p2p_server": "p2p.de.ilifestyle-cloud.com",
  "sip_server": "de.ilifestyle-cloud.com",
  "rtmp_server": "rtmp.de.ilifestyle-cloud.com",
  "rtmps_server": "rtmp.de.ilifestyle-cloud.com:1936",
  "update_server": "c1.ilifestyle-cloud.com"
}
```

`info.sip` / `info.mqtt` / `info.rtmp` is the cloud's view of the connection state — handy as a remote health check.

### GET /api/v2/devices/<MAC>/keys

List all device-key records. Each `key` is one bind-slot (i.e. one outdoor station address).

```json
{
  "code": 0, "message": "OK", "total": 1,
  "keys": [
    {
      "id": 42,
      "device_id": "",
      "key": "2",                          // bus address
      "binding_code": "42.examplecodeXYZ",   // the QR code text
      "multi_user": 1,
      "conf": {},
      "create_time": "2026-05-21T14:57:07+02:00"
    }
  ]
}
```

### GET /api/v2/devices/<MAC>/keys/<id>

Detail one key. Adds `user_count`, `user_limit`.

```json
{
  "code": 0, "message": "OK",
  "id": 42, "device_id": "", "key": "2",
  "binding_code": "42.examplecodeXYZ",
  "user_limit": 8, "user_count": 1,
  "multi_user": null, "conf": {},
  "update_time": "2026-05-21T..."
}
```

### GET /api/v2/devices/<MAC>/keys/<id>/users

List bound app users for this key.

```json
{
  "code": 0, "message": "OK", "total": 1,
  "users": [
    {
      "key_id": 42,
      "user_id": "u00cAAAAAAAAAAAA",
      "state": 1,                        // 1 = active
      "role": 1,                         // 1 = admin
      "email": "user@example.com",
      "create_time": "2026-05-21T..."
    }
  ]
}
```

### PUT /api/v2/devices/<MAC>/keys/<id>/users/<n>?delete_all=1

Refresh the binding code (regenerates the QR). Drops all users when `delete_all=1`.

```
HTTP 200 → {"code": 0}
```

## MQTT (cloud broker)

Broker hostnames:
- `de.ilifestyle-cloud.com:1883` (plain MQTT)
- `de.ilifestyle-cloud.com:8883` (MQTT-over-TLS)

### Device authentication (works for us)

```
ClientID:   <MAC>           (e.g. "AABBCCDDEEFF")
Username:   <MAC>            (any string passes if ClientID matches; convention is MAC)
Password:   <JWT>            (from /api/login)
KeepAlive:  10–60 s
```

The broker validates JWT signature server-side and checks `JWT.did == ClientID`. **Connecting kicks the real GW** (MQTT spec: one client per ClientID).

### App authentication (we didn't fully reverse)

The app uses a different `device_type` + `device_model` combination (likely sniffed from Android-side or HMAC-signed) → different JWT → can run alongside the GW. Reverse-engineering the APK is the next step if a true cloud-side parallel client is needed.

### Topics

```
Subscribe (GW listens here):
  <GW_MAC>                                    e.g. "AABBCCDDEEFF"

Subscribe (app listens here):
  <App_UUID>                                  e.g. "0123456789abcdef0123456789abcdef"

Publish for wake (app → GW):
  topic = <GW_MAC>
  payload:
    {
      "action": "monitor",
      "from":   "<App_UUID>",       // your client ID for response routing
      "tag":    "<unique_id>",      // any unique string, returned in responses
      "ctrl":   "1",                 // "1" = start, "0" = stop
      "key_index": 1,                // callList.id (= 1 for default setup)
      "duration": 60                 // seconds; the indoor monitor shows ~15-30s max
    }

Response (GW → topic = <App_UUID>):
  {
    "tag":      "<unique_id>",
    "action":   "monitor",
    "response": "ok" | "err" | "finish"
  }
```

Other observed actions: `unlock`, `call`, `hook`, `hang`, `switchCamera` — same envelope, different `action` field.

## RTMP

The GW pushes video to:

```
rtmp://rtmp.de.ilifestyle-cloud.com/live/<stream-key>
rtmps://rtmp.de.ilifestyle-cloud.com:1936/live/<stream-key>
```

The cloud relays to whoever asks (the app, presumably). For local integration this stream is irrelevant — we pull RTSP directly from the GW.

## P2P

For low-latency video the GW can also do P2P via `p2p.de.ilifestyle-cloud.com`. Signalling is over MQTT (topic prefix `WDTRGRJUFHNK<MAC>` in the logs), media via ICE/STUN/TURN to `8.209.83.57` (cloud TURN). Visible in `/customer/share/gst-p2p-log.log`.

Same conclusion: not needed for local HA integration.
