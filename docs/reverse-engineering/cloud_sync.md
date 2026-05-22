# Villa GW V3.0 — Cloud Sync Reverse Engineering

Quelle: `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/`
Stand: 2026-05-22

Diese Analyse beantwortet die Frage, ob die iLifestyle-Cloud per DNS-Hijack durch einen lokalen
Replacement-Service ersetzt werden kann, um die MQTT-Events (Klingeldruck, Relay-State, etc.) direkt
und ohne Roundtrip über die Hersteller-Cloud lokal zu konsumieren.

---

## TL;DR — Goldweg ist plausibel

- Die Cloud-Konfiguration (Server-Hostname, MQTT-Server, SIP-Server, P2P-Server, Update-Server)
  steht komplett in der lokalen SQLite-DB `/customer/share/avl20.db` (Tabelle `config`).
  Sobald `cloud_account.token` einmal gesetzt ist, läuft das Gerät auch ohne erneuten Cloud-Kontakt
  weiter — Login wird nur dann erneut ausgeführt, wenn der Token leer ist.
- MQTT verbindet zwar gegen `de.ilifestyle-cloud.com` per **TLS 1.2** mit CA-Bundle
  `/customer/share/ca-certificates.crt`, aber **kein Cert-Pinning** (Mosquitto-`tls_set` ohne
  `tls_opts_set(SSL_VERIFY_PEER, ...)`-spezifische Constraints, nur Standard-CA-Path).
- Die Topics, Username und Password kommen aus der Cloud-`/api/device`-Response in `autoSync.lua`
  und werden in `config['sip']` (`mqtt_server`, `name`, `password`) gespeichert. Wir können diese
  Werte direkt per SQL setzen — **kein Cloud-Login nötig**.
- Damit: **DNS-Hijack auf `de.ilifestyle-cloud.com`** + eigener MQTT-Broker + eigene CA im
  Truststore = direkter Zugriff auf die Live-Events. Optional kann der lokale Broker das
  REST-Login + `/api/device`-GET so beantworten, dass das Gerät sich neu konfiguriert — dann braucht
  es nicht einmal einen Direkt-Eingriff in die SQLite.

Details siehe Abschnitt 6, 7 und 8.

---

## 1. Cloud-Endpoints

### Aktive Hostnames (aus DB-Default + Sync-Flow)

| Funktion        | Host                              | Protokoll / Port | Genutzt von |
|-----------------|-----------------------------------|------------------|-------------|
| Cloud-API       | `de.ilifestyle-cloud.com`         | HTTPS (443)      | `autoSync.lua` (alle `http.request`) |
| MQTT-Broker     | `de.ilifestyle-cloud.com`         | MQTTS (TLS 1.2)  | `avlink` Daemon |
| SIP-Server      | `de.ilifestyle-cloud.com`         | SIP (5060)       | `pjsua` / `avlink` |
| RTMP-Push       | `rtmp.de.ilifestyle-cloud.com`    | RTMP             | `mimedia` |
| P2P-Server      | `p2p.de.ilifestyle-cloud.com`     | proprietär       | `media-server` |
| Firmware-Update | `c1.ilifestyle-cloud.com`         | **HTTP (80)**    | `firmware_upgrade.lua` |

### Versteckte Hersteller-Fallbacks

In `customer/share/firmware_upgrade.lua` stehen **hardcoded Fallback-Hosts**, falls die DB leere
Werte hat:

```lua
server = "tj.systec-pbx.net"       -- post_firmware_version Fallback
server = "c1.systec-pbx.net"       -- get_download_url Fallback
-- Kommentar im Code:
-- download_url = "http://c1.systec-pbx.net/software/update.tar.bz2"
```

`de.ilifestyle-cloud.com` ist offensichtlich nur ein **gebrandeter Re-Skin** des chinesischen OEMs
**Systec PBX** (Tianjin). Beide Domains führen vermutlich auf dieselbe Infrastruktur. Es gibt keine
weiteren Backup-Hostnames im Lua-Code oder in den Binaries.

### REST-API-Endpoints (alle gegen `https://<server>/...`)

| Methode | Pfad                                          | Body / Header                        | Zweck                                 |
|---------|-----------------------------------------------|--------------------------------------|----------------------------------------|
| POST    | `/api/login`                                  | `{user_id, password, device_type:6, device_model:"AVL20P", device_id, device_name?}` | Login → Token |
| GET     | `/api/device?id=<MAC>`                        | `Authorization: <token>`             | Konfig holen (sip, mqtt, p2p, video, update) |
| PUT     | `/api/device/<deviceId>`                      | `{video_transfer}`                   | Video-Transfer-Mode pushen |
| PUT     | `/api/device/<deviceId>/conf`                 | `{path:["device_button"], data:<n>}` | Button-Konfig pushen |
| POST    | `/api/v2/devices/<deviceId>/keys`             | `{key, single:true, bind_self}`      | Tastenbelegung registrieren |
| DELETE  | `/api/v2/devices/<deviceId>/keys/0?delete_all=1` | `Authorization: <token>`          | Alle Tasten-Bindings entfernen |
| POST    | `/api/device/<id>/info` (firmware)            | `version`-Body                       | Firmware-Version reporten |
| POST    | `/api/call/record`                            | mit `Host`-Header gesetzt            | Call-Records hochladen |
| GET     | `/download_path?product_name=AVL20P` (c1.*)   | Plain HTTP                           | Download-URL fürs Firmware-Update |

### Login-API-Format

Request (`autoSync.lua:60-83`):

```json
POST https://de.ilifestyle-cloud.com/api/login
Content-Type: application/json
{
  "user_id":     "REDACTED@example.com",
  "password":    "REDACTED_CLOUD_PW",
  "device_type": 6,
  "device_model":"AVL20P",
  "device_id":   "AA:BB:CC:DD:EE:FF",          // MAC ohne `:` (siehe getMac())
  "device_name": "Villa GW"               // optional
}
```

Response erwartet:

```json
{ "code": 0, "token": "<JWT>" }
```

Beispiel-Token (aus `avl20.dump.sql`) ist ein **HS256-JWT**:
```
header  = {"alg":"HS256","typ":"JWT"}
payload = {"app":1,"uid":"u00c0000000022cd","ugp":4,
           "did":"AA:BB:CC:DD:EE:FF","dmd":"AVL20P","dtp":6,"iat":1779368227}
```

→ Der Server signiert mit einem Secret, das wir bei einem Replica-Service einfach selbst wählen
können (wir verifizieren das JWT ja nicht — das Device sendet es nur als `Authorization`-Header
zurück; der Server muss es validieren, nicht das Device).

### Token-Refresh

**Es gibt keinen separaten Refresh-Endpoint.** `autoSync.lua` prüft nur, ob `token` leer ist:
```lua
if (token == '' or token == nil) then loginPost(...) else initData(...) end
```
Ist der Token gesetzt → kein Re-Login. Server kann mit HTTP-401 antworten, der Code prüft aber nur
`code.code ~= 0` ohne expliziten Refresh-Pfad — bei jedem Sync-Run werden alle Requests einfach mit
dem alten Token gefeuert. Falls die Cloud den Token invalidiert, würde das Device beim nächsten
Sync hängen, bis manuell `apk.lua` ein neues `cloud_account` schreibt.

---

## 2. Pairing- / Binding-Flow

### Initial-Pairing

Das Gerät registriert sich **nicht autonom**. Stattdessen wird in der iLifestyle-App ein Account
angelegt; die App ruft dann den lokalen Endpoint `apk.lua` auf (offen per HTTP auf dem GW, kein Login
nötig — siehe Kommentar `apk.lua:166 -- os.execute('echo ...apk api start...')`), und übergibt:

```json
POST /apk
{
  "ssid":     "<wifi-ssid>",       // optional
  "psk":      "<wifi-psk>",        // optional
  "server":   "de.ilifestyle-cloud.com",
  "account":  "<user-email>",
  "password": "<user-password>",
  "name":     "Villa GW",
  "button":   "2",
  "purpose":  0,                   // 0 = call-list-mode
  "bindSelf": 1,
  "readd":    0
}
```

`apk.lua` schreibt das in die SQLite (`wifi`, `cloud_account`, `av_link`, `purpose`) und triggert
dann via TCP-Socket `127.0.0.1:60000`:
1. `AT+B RELOAD wifi\r\n` → custode2.lua re-initialisiert das Netz
2. nach 2 s `AT+B RELOAD address\r\n` → triggert in `custode2.lua` (Zeile 606-611) **`check_sync()`**
   → führt `/var/www/lua/autoSync.lua` aus → der erstmalige Login geschieht hier.

### Binding-Code-Format

Das `shareCode`-Feld (`callList.shareCode`, gesetzt durch `updateShareCode.lua`) ist der QR-Code
für andere Nutzer, die sich mit dem Gateway verbinden wollen — kein Pairing-Code für das Device
selbst. Format wird vom Server vergeben (im Code wird er nur opaque weitergereicht und in SQLite
gespeichert). Im Beispiel-RTMP-Pfad findet sich `live/RTMP_STREAM_KEY_REDACTED` — das sieht nach
einem Stream-Key aus, der pro Gerät vergeben wird; vermutlich ist `shareCode` analog dazu.

### Device-Identity

- **device_id** = MAC-Adresse von `eth0` ohne `:`-Trennzeichen, uppercase (z.B. `AA:BB:CC:DD:EE:FF`)
- **device_model** = `"AVL20P"` (hardcoded)
- **device_type** = `6` (hardcoded)

Es gibt kein Pre-Shared-Secret oder Device-Cert in der Firmware (durchsucht: keine `.pem`/`.key`/`.crt`
außer dem System-CA-Bundle). Die einzige "Identität" gegenüber der Cloud ist die MAC + das
User-Account-Passwort. → MAC ist trivial zu spoofen, weshalb der Cloud-Login *zwingend* User+Passwort
braucht.

---

## 3. Sync-Frequenz und Datenumfang

### Wann wird gesynct?

Es gibt **keinen periodischen Cron-Sync**. `autoSync.lua` wird nur durch `check_sync()` in
`custode2.lua` getriggert, und zwar:

1. **Bei jedem Netzwerk-Übergang IDLE→RUNNING** (eth0 oder wlan0 bekommt eine IP) —
   `custode2.lua:715, 796, 896`.
2. **Bei explizitem `AT+B RELOAD address`** über den lokalen TCP-Port 60000 —
   `custode2.lua:606-611`. Das wird ausgelöst von:
   - `apk.lua` nach App-Pairing (siehe oben),
   - aus dem UI nach Adressänderungen.
3. **Bei Neustart** des Gateways (führt zwangsläufig zu Netz-Übergang).

→ Faktisch: **Sync nur bei Boot + Netzwerk-Reconnect**. Kein Polling-Intervall, kein Heartbeat
gegen die REST-API. Das Live-Update-Kanal ist **ausschließlich MQTT** (vom Daemon `avlink`).

### Was wird gesynct?

Pro `autoSync.lua`-Run werden in dieser Reihenfolge Daten ausgetauscht:

| Step | Richtung | Endpoint | Daten |
|------|----------|----------|-------|
| 1 | GW → Cloud | `POST /api/login` | Credentials + MAC, holt JWT (nur wenn Token leer) |
| 2 | GW → Cloud | `GET /api/device?id=<MAC>` | Holt: `dialplan`, `mqtt_server`, `sip_id`, `name`, `password`, `sip_server`, `video_url` (RTMP), `video_transfer`, `update_server`, `p2p_server` |
| 3 | GW → SQLite | UPDATE `config.sip`, `config.video`, `config.device_update` | persistiert die Cloud-Antwort |
| 4 | GW → Daemon | `AT+B RELOAD` (Port 10086), `AT+B RELOAD icloud` (Port 60000) | triggert avlink-Reload + p2p-Reload |
| 5 | GW → Cloud | `PUT /api/device/<id>` | sendet `video_transfer` zurück (Echo) |
| 6 | GW → Cloud | `PUT /api/device/<id>/conf` | sendet `device_button` |
| 7 | GW → Cloud | `POST /api/v2/devices/<id>/keys` | sendet Button-Belegung (nur bei `purpose==0`) |
| 8 | GW → Cloud | `DELETE /api/v2/devices/<id>/keys/0?delete_all=1` | nur falls Purpose geändert (cleanup) |

`callList` und `shareCode` werden **nicht** gesynct — die hängen nur lokal in der SQLite und
werden via `updateCallList.lua` / `updateShareCode.lua` durch lokale UI-Requests gesetzt
(`updateCloudEnable.lua` toggelt nur das `enable`-Flag pro Listeneintrag, auch lokal).

→ Die "Cloud" liefert also primär die **SIP/MQTT/RTMP/P2P-Server-Adressen und Credentials**, sonst
nichts. Alle laufenden Anrufe / Klingelevents laufen danach **ausschließlich über MQTT + SIP**, ohne
weitere HTTP-Roundtrips.

---

## 4. MQTT-Verwendung

### Verbindungsaufbau (aus `avlink`-Strings rekonstruiert)

```
mosquitto_new(client_id = <MAC, z.B. "AA:BB:CC:DD:EE:FF">)
mosquitto_tls_set(ca_file="/customer/share/ca-certificates.crt",
                  cert_file=NULL, key_file=NULL,
                  tls_version="tlsv1.2")
mosquitto_username_pw_set(username = <sip_name = "s00c0000DEVICE_ID">,
                          password = <sip_password = "REDACTED_SIP_PW">)
mosquitto_connect_async(host = <mqtt_server = "de.ilifestyle-cloud.com">,
                        port = 1883 oder 8883 (siehe unten),
                        keepalive=…)
```

> **Port-Ambiguität:** Die Log-Strings zeigen sowohl `mqtt port=1883 socket=%d` als auch
> Verwendung von `mosquitto_tls_set`. Wahrscheinlich ist das Log-String veraltet (Copy-Paste vom
> Vorgänger) und tatsächlich wird `8883` benutzt — kann nur durch tcpdump am Live-Device verifiziert
> werden. Für unseren Replica-Broker sollten wir auf **beide Ports lauschen**.

### Topics (rekonstruiert aus Format-Strings + JSON-Payloads)

Direkte Topic-Strings sind in `avlink` nicht zu finden — sie werden zur Laufzeit aus `device_id`
(MAC) und/oder `user_id` zusammengebaut. Aus den Log-Strings und JSON-Patterns wissen wir, dass das
Gerät folgende Nachrichten austauscht:

**Subscribed (eingehend, Cloud → Device):**

| Action  | Payload-Schema                                    | Reaktion im Daemon |
|---------|---------------------------------------------------|---------------------|
| `CTRL`  | `{"action":"CTRL","event":{"relay":"<id>"}}`      | Schaltet das benannte Relais (Türöffner) für `relay.duration_x` Sekunden |
| `STATE` | (Query) | Antwortet mit `{"action":"STATE","event":{"relay":"%d%d"}}` |
| `UPGRADE` | irgendein Trigger-Payload | Startet `firmware_upgrade.lua` via `AT+B UPGRADE %d` |

**Published (ausgehend, Device → Cloud):**

| Action   | Payload-Schema                                                  | Trigger |
|----------|-----------------------------------------------------------------|---------|
| `UPDATE` | `{"action":"UPDATE","event":{"deviceID":"<id>","state":<n>}}`   | Statuswechsel (online, Tür-Auf-Bestätigung) |
| `STATE`  | `{"action":"STATE","event":{"relay":"%d%d"}}`                   | Antwort auf Query |

### Topic-Konvention

Aus `avlink` bekannt sind nur die generischen Variablen `remote_topic`. Da der MQTT-Client-ID die
MAC ist und der Username die `sip_id`, ist die plausibelste Topic-Struktur (zu verifizieren via
mosquitto-Wildcard-Sub `+/+/+/+/+/#` auf Replica-Broker):
```
devices/<MAC>/cmd       (sub)
devices/<MAC>/state     (pub)
users/<uid>/devices/<MAC>/event   (pub)
```
oder ähnlich. **Praktischer Weg:** Replica-Broker startet, lauscht ohne ACL auf `#`, loggt alles
beim ersten Verbindungsversuch des GW — dann sehen wir die echten Topic-Strings live.

### Heartbeat

Daemon schreibt periodisch `/var/run/avlink-mqtt.heartbeat`; das ist nur ein lokales File
(Liveness-Check für `monitor.lua`/`uptime_damemon`), nicht MQTT-PINGREQ. Echte MQTT-Keepalives
gehen über das Standard-Protokoll.

---

## 5. TLS-Setup

- **CA-Bundle:** `/customer/share/ca-certificates.crt` (Standard-Pfad, kein vendor-spezifisches
  Pinning).
- **TLS-Version:** `tlsv1.2` (explizit gesetzt).
- **Client-Cert:** Keine (kein `cert_file`/`key_file` in den Mosquitto-Aufrufen sichtbar).
- **Hostname-Verification:** Standard mosquitto-Default (= AN, prüft CN gegen Hostname).
- **Cert-Pinning:** **Nein.** Der Code nutzt das vollständige System-CA-Bundle. Eine
  Replacement-CA kann einfach hinzugefügt werden.

`socket.http` (für REST-Calls) hingegen — schwacher Punkt: das ist `lua-socket` mit
`socket.http`-Modul, das **per Default** keine TLS-Verifikation macht (es ist die *unverschlüsselte*
HTTP-Bibliothek). Aber die URLs verwenden `https://...` — das geht **nur** wenn das Modul mit
`luasec` gewrappt ist. Ohne `luasec` würden die Requests fehlschlagen. Wahrscheinlich ist im
Build doch `luasec` ausgeführt, aber **ohne expliziten Cert-Check** → das wäre der zweite Hebel
(MITM-Proxy mit beliebigem Cert würde durchgehen). Zu verifizieren am Live-Device.

→ **Für DNS-Hijack:** Wir müssen unsere eigene CA in `/customer/share/ca-certificates.crt`
einbauen ODER das CA-Bundle komplett austauschen ODER die `socket.http`/`luasec`-Schwäche nutzen.
Das CA-Bundle liegt im `customer`-Partition-Mount, ist beschreibbar (rcS mountet als rw).

---

## 6. Auswirkungen eines DNS-Hijacks auf `de.ilifestyle-cloud.com`

### Was bricht, was bleibt

| Funktion | Wenn Domain → leer DNS | Wenn Domain → eigener Server, der ALLES (HTTPS+MQTT) annimmt |
|----------|------------------------|-------------------------------------------------------------|
| Erst-Pairing | bricht (Login `/api/login` schlägt fehl) | funktioniert wenn `/api/login` korrekt antwortet |
| Re-Boot Sync | bricht; `autoSync.lua` failed, aber **Device läuft mit alter Config weiter** | OK |
| MQTT-Live-Events (Klingel/Relay) | **bricht** — kein Broker erreichbar; SIP-Klingeln läuft aber weiter | **funktioniert direkt lokal** (Goldweg) |
| SIP-Calls | bricht (SIP-Register fehlt) | funktioniert mit lokalem SIP-Server (`asterisk`/`kamailio`) |
| RTMP-Push (Cloud-Live-Stream) | bricht | funktioniert mit lokalem RTMP-Server (`nginx-rtmp`/`mediamtx`) |
| RTSP `rtsp://<IP>/live.sdp` (LAN) | **bleibt** (komplett lokal, P2P/Cloud-unabhängig) | bleibt |
| Firmware-Update | bricht (Plain-HTTP auf `c1.ilifestyle-cloud.com`) | trivial fakebar (HTTP, kein TLS) |
| iLifestyle-App Login | bricht (App geht selbst gegen die Cloud) | erfordert Replica-API mit App-Endpoints, deutlich mehr Aufwand |

### Konkretes Szenario "Nur MQTT lokal"

Minimales Setup für den Goldweg:
1. Pi-Hole/AdGuard/DNS-Override → `de.ilifestyle-cloud.com → 192.168.x.y`.
2. Auf `192.168.x.y` läuft **Mosquitto** mit:
   - Port 1883+8883 offen,
   - Self-Signed-Cert für `de.ilifestyle-cloud.com`,
   - User+Password = `<sip_id>:<sip_password>` (steht im Device unter `config.sip.name/password`),
   - ACL allow für Client-ID = MAC.
3. CA-Cert von Mosquitto wird via SSH+`scp` ins GW kopiert nach
   `/customer/share/ca-certificates.crt` (entweder ersetzen oder anhängen).
4. Optional: Falls dasselbe Replacement-Host auch HTTPS auf 443 anbietet, kann ein Reverse-Proxy
   `/api/*` durchreichen (an die echte Cloud oder an einen Fake-Endpoint) — wenn man nur MQTT
   will, kann man `autoSync.lua` aber auch einfach scheitern lassen; das Device verwendet dann
   die bestehende SIP-/MQTT-Config aus der DB und bootet trotzdem in den Operating-State.
5. Trigger Reboot oder `AT+B RELOAD` → Device verbindet sich mit dem Replica-MQTT-Broker.
6. Auf dem Broker abonnieren: `#` → alle Klingel-/Relay-Events live in Home Assistant
   (via MQTT-Integration).

→ **Kein Code im Device wird modifiziert**, nur DB-Werte und das CA-Bundle.

### Risiken / Constraints

- Falls das Cloud-JWT vom Device gegen einen Server-seitig variablen Wert validiert wird (z.B.
  Re-Login bei MQTT-CONNACK-Reject), könnten wir es nicht via DB-Patch lösen. Aktuell sieht der
  Code aber so aus, dass nur User+Password und sip_id+sip_password genutzt werden — die kennen
  wir aus der DB.
- Wenn der iLifestyle-App weiterhin gegen die Hersteller-Cloud spricht (App-Login geht ja nicht
  übers GW-Netz, sondern direkt), funktioniert **die App nicht mehr** sobald die Cloud im
  GW-LAN überschrieben ist — aber das ist genau das gewünschte Goldweg-Szenario.
- Falls die Hersteller-Cloud das JWT regelmäßig invalidiert (siehe §1 Token-Refresh), müsste der
  Replica-Server eine `/api/login`-Antwort liefern, die einen "frischen" Fake-Token zurückgibt.

---

## 7. Replizierbarkeit der Cloud

### Minimum Viable Replica

Wenn wir die **vollständige** Cloud lokal nachbauen wollen (so dass das Device einen Factory-Reset
überstehen kann ohne je die Originalcloud zu sehen), brauchen wir:

| Komponente | Implementierung |
|------------|-----------------|
| HTTPS-Endpoint | nginx/Caddy mit eigenem Cert (CA muss im Device installiert sein) |
| `POST /api/login` | Liefert `{"code":0, "token":"<beliebiges HS256-JWT>"}` |
| `GET /api/device?id=<MAC>` | Liefert komplettes Config-Bundle (siehe unten) |
| `PUT /api/device/<id>` | `{"code":0}` |
| `PUT /api/device/<id>/conf` | `{"code":0}` |
| `POST /api/v2/devices/<id>/keys` | `{"code":0}` |
| `DELETE /api/v2/devices/<id>/keys/0` | `{"code":0}` |
| `POST /api/device/<id>/info` (firmware) | `{"code":0}` |
| `POST /api/call/record` | `{"code":0}` |
| `GET /download_path?product_name=AVL20P` (HTTP!) | `{"code":0,"download_path":"http://…/no-upgrade.tar.bz2"}` |
| MQTT-Broker | Mosquitto, port 8883 + 1883, TLS mit obiger CA |
| SIP-Server | Asterisk oder Kamailio, mit Account = `sip_name:sip_password` aus Response |
| RTMP-Server | nginx-rtmp / mediamtx, akzeptiert beliebige Stream-Keys |
| P2P-Server | proprietäres Protokoll → vermutlich nicht replizierbar, kann weggelassen werden (Video läuft lokal über RTSP) |

### Pflicht-Felder der `/api/device`-Response

Aus `autoSync.lua:147-189` rekonstruiert:

```json
{
  "code": 0,
  "dialplan":      "<contact-string>",
  "mqtt_server":   "de.ilifestyle-cloud.com",
  "sip_id":        "s00c0000DEVICE_ID",
  "name":          "Villa GW",
  "password":      "REDACTED_SIP_PW",
  "sip_server":    "de.ilifestyle-cloud.com",
  "video_url":     "rtmp://rtmp.de.ilifestyle-cloud.com/live/<stream-key>",
  "video_transfer":1,
  "update_server": "c1.ilifestyle-cloud.com",
  "p2p_server":    "p2p.de.ilifestyle-cloud.com"
}
```

Alle Werte können in unserer Replica beliebig gewählt sein (z.B. auf die LAN-IPs zeigen).

→ **Fazit:** Lokale Replica ist mit moderatem Aufwand machbar (~2 Tage). MVP für reinen
MQTT-Tap ist deutlich einfacher (~2 Stunden) — siehe §6.

---

## 8. iLifestyle-App ohne Cloud

### LAN-Funktionalität

`apk.lua` ist der App-Pairing-Endpoint, **er läuft auf dem GW selbst** auf dem nginx-HTTP-Server
(Port 80, ohne Auth — siehe Code, keine Cookie/JWT-Verifikation für POST!). Das heißt:

- Erst-Pairing der App geht in jedem Fall via LAN auf das GW.
- Danach: Die App spricht via MQTT (`mqtt_server`-Wert aus DB) — diese Verbindung geht aber an
  *die Cloud*, nicht ans GW direkt. **Ohne Cloud kann die App also nicht klingeln/öffnen**, es
  sei denn sie spricht den lokalen MQTT-Broker.

### Was bleibt LAN-only verfügbar

| Feature | LAN-only möglich? |
|---------|-------------------|
| RTSP-Live-Stream `rtsp://<gw-ip>/live.sdp` | **JA**, völlig Cloud-unabhängig |
| SIP-Call ins LAN (z.B. Linphone gegen lokalen Asterisk) | JA, sofern lokaler SIP-Server konfiguriert wird |
| Türöffner via lokaler REST `/relay` o.ä. | nicht direkt im GW exponiert, aber via SIP-DTMF oder lokalem MQTT-Broker steuerbar |
| iLifestyle-App live | NEIN ohne Cloud-Replica (App pingt eigene Backend-Server) |
| Klingel-Event-Detection | JA via lokalen MQTT-Broker (Goldweg) |

### Empfehlung

- Für **reines Home-Assistant-Eventing**: §6-Goldweg implementieren, App ignorieren (App kann
  parallel weiter mit der Hersteller-Cloud sprechen, wenn man DNS-Hijack nur netzwerkseitig auf
  das GW beschränkt — z.B. statisches DNS-Override nur für die GW-MAC im Router/Pi-Hole).
- Für **App-Replacement**: HA-Companion-App + Lovelace-Card mit RTSP-Stream + MQTT-Buttons. Damit
  ist die iLifestyle-App überflüssig.

---

## Anhang A — Datei-Übersicht (gelesene Lua-Skripte)

| Datei | Zweck | Auslöser |
|-------|-------|----------|
| `customer/lua/sync.lua` | **NICHT Cloud-Sync** — sondern Upload-Handler für `backup.tar.bz2` (Config-Restore). Schreibt nach `/customer/share/backup.tar.bz2`, entpackt, rebootet. Trotz des Namens nichts mit Cloud zu tun. |
| `customer/lua/autoSync.lua` | Hauptsync-Skript. Login + GET /api/device + Config-Persist + Reload. |
| `customer/lua/commonSync.lua` | Mini-Helper, nur `dbExeReturn` (Retry-Wrapper für SQLite-Exec). |
| `customer/lua/updateCloudEnable.lua` | Lokaler API-Endpoint: toggelt `callList.enable`. Nur lokal, kein Cloud-Call. |
| `customer/lua/updateCallList.lua` | Lokaler API-Endpoint: editiert callList-Eintrag inkl. SIP-Server. Trigger `AT+B RELOAD callList`. |
| `customer/lua/updateShareCode.lua` | Lokaler API-Endpoint: setzt `callList.shareCode`. Nur lokal. |
| `customer/lua/getServerList.lua` | Lokaler API-Endpoint: liefert SIP-Server-Liste aus SQLite. |
| `customer/lua/testConnect.lua` | Lokaler API-Endpoint: triggert `AT+B CHECKSIP` Test gegen avlink. |
| `customer/lua/cloudDevice.lua` | Lokaler API-Endpoint: identisch zu `autoSync.initData()` aber **ohne Cloud-Call** — empfängt das Config-Bundle als POST-Body und persistiert es. **Wichtig für Replica-Setup: über diesen Endpoint kann man die GW-Cloud-Config manuell injecten ohne je die Cloud zu sprechen!** |
| `customer/lua/login.lua` | UI-Login (lokal), JWT-Erzeugung mit hardcoded Key `'hard to guess string device'` |
| `customer/lua/account.lua` | UI-CRUD für `cloud_account` (Server, Account, Passwort). |
| `customer/lua/apk.lua` | **App-Pairing-Endpoint** — schreibt Cloud-Credentials + Wifi-Config + Tastenbelegung. |
| `customer/lua/jwt.lua` | JWT-Wrapper. Key = `'hard to guess string device'` (sic!). |
| `customer/lua/firmware_upgrade.lua` (in share/) | Firmware-Download via plain HTTP von `update_server`. |
| `customer/app/sbin/custode2.lua` | Hauptdaemon (TCP-Server :60000), triggert `autoSync.lua` bei Netz-Reconnect via `check_sync()`. |
| `usr/sbin/monitor.lua` | Watchdog für nginx + voip-server; kein Cloud-Bezug. |

## Anhang B — Goldweg Quick-Recipe

```bash
# 1. Auf einem RPi (oder im HA-Add-on): Mosquitto installieren mit TLS
sudo apt install mosquitto mosquitto-clients
# /etc/mosquitto/conf.d/villa-gw.conf:
listener 8883
cafile     /etc/mosquitto/ca.crt
certfile   /etc/mosquitto/server.crt
keyfile    /etc/mosquitto/server.key
require_certificate false
allow_anonymous false
password_file /etc/mosquitto/passwd

listener 1883      # Fallback ohne TLS, falls Port-Annahme stimmt

# 2. Passwort des GW aus dessen DB nutzen
mosquitto_passwd -b /etc/mosquitto/passwd s00c0000DEVICE_ID REDACTED_SIP_PW

# 3. Self-Signed Cert für de.ilifestyle-cloud.com erzeugen
#    (CN = de.ilifestyle-cloud.com, SAN = de.ilifestyle-cloud.com)

# 4. DNS-Override im LAN (Pi-Hole / AdGuard / Router)
#    de.ilifestyle-cloud.com -> 192.168.1.X (Mosquitto-Host)

# 5. Auf das GW per SSH (root/<gw-pw>) und CA installieren
scp /etc/mosquitto/ca.crt root@<gw-ip>:/tmp/ca.crt
ssh root@<gw-ip> 'cat /tmp/ca.crt >> /customer/share/ca-certificates.crt; reboot'

# 6. Auf Mosquitto-Host: alle Topics live mitlesen
mosquitto_sub -h localhost -p 8883 --cafile /etc/mosquitto/ca.crt \
  -u s00c0000DEVICE_ID -P REDACTED_SIP_PW -t '#' -v
# -> hier siehst du jetzt die echten Topic-Namen
```

Sobald die Topics bekannt sind, in HA via `mqtt:`-Integration einbinden — fertig.
