# Villa GW V3.0 — MQTT Protocol Reference

> Vollständiges Inventar aller MQTT-Topics + Payload-Schemas zwischen **Villa GW V3.0 (AVL20P)** und der iLifestyle-Cloud (`de.ilifestyle-cloud.com`).
>
> **Stand:** 2026-05-22 (Update nach 15-min-Live-Watcher mit globalem `#`-Sniff + 12× reproduziertem Klingelevent)

## Update 2026-05-22 — wichtige Korrekturen

1. **Klingel-Push NICHT via MQTT**: Doorbell-Ring (`call_btn_trigger`) erscheint **nicht** als MQTT-Message für die eigene MAC. Die App-Notification läuft über **SIP INVITE** (existierende `pjsua → de.ilifestyle-cloud.com:5061 TLS` Connection) → Cloud routet → **FCM (Android) / APNS (iOS) Push** zur App. Bestätigt durch netstat-Snapshot des GW: keine separate Push-Server-Connection, nur SIP+MQTT+RTMP.
2. **Action `OPEN DOOR` ist aktiv** — entgegen früherer Annahme „im Binary kompiliert aber nicht in Verwendung" → live in 3 fremden GW-Sessions beobachtet.
3. **`ctrl="N"`** ist ein weiterer ctrl-Wert (vermutlich Next-Camera-Switch).
4. **EMQ X hat NULL ACL** — globaler `#`-Subscribe liefert Türöffnungen + Monitor-Sessions fremder Haushalte. Privacy-relevant für Disclosure.
5. **ClientID-Format**: `<alias>|<MAC>` (alias VOR pipe). MQTTv5, `clean_start=False`, `SessionExpiryInterval=60`. Username = MAC, Password = JWT von `/api/account` (token-Feld).

> **Quellen:**
> - Binary: `villa_gw_dump/customer/app/sbin/avlink` (Mosquitto-Client, `mqtt-client.c` + `avlink-server.c`)
> - Lua-Sync: `villa_gw_dump/customer/lua/{autoSync,cloudDevice,updateCallList,updateCloudEnable}.lua`
> - Live-Logs: `/customer/share/usr-log.log` (via Telnet `203.0.113.10:23`)
> - SQLite-DB: `/customer/share/avl20.db` Tabelle `config` (`sip`, `video`, `cloud_account`)

---

## TL;DR

- **Genau ein Topic** in Gebrauch: `<MAC>` (z.B. `AA:BB:CC:DD:EE:FF`). Das Gerät **abonniert** sein eigenes MAC-Topic, und **publisht Antworten zurück auf dasselbe Topic des Senders** (= `from`-Feld der Inbound-Message).
- Es gibt **kein Pub/Sub-Hierarchie-Schema** à la `devices/<id>/cmd` — alles flach, einfach `<MAC>` ⇆ `<userSessionId>`.
- **Eine** in der Praxis beobachtete Action: `"monitor"` (= Kamera/Klingelmodus + Tür-Auf). Im Binary sind noch `CTRL` / `STATE` / `UPDATE` / `UPGRADE` definiert, werden aber im aktuellen Build nicht via MQTT genutzt (vermutlich Legacy oder Push-Notification-Pfad).
- **Goldweg-Replica:** Lokaler Mosquitto-Broker mit nur diesem einen Topic-Pattern reicht aus, damit das GW happy ist und Klingel-Events liefert.

---

## 1. MQTT Connection

| Property | Wert | Quelle |
|---|---|---|
| Broker-Host | `de.ilifestyle-cloud.com` (resolved → `198.51.100.50`) | DB `config.sip.mqtt_server`, live `netstat`: `tcp 203.0.113.10:55132 -> 198.51.100.50:8883 ESTABLISHED` |
| Broker-Port | **`8883`** (TLS) — der Log-String `mqtt port=1883 socket=%d` ist veralteter Code, faktisch wird `tls_set` + 8883 benutzt | `netstat`-Verifikation am Live-Device |
| TLS | TLS 1.2, CA-File `/customer/share/ca-certificates.crt`, **kein Cert-Pinning**, **kein Client-Cert** | `mosquitto_tls_set(ca_file, NULL, NULL, "tlsv1.2")` |
| Client-ID | `<MAC ohne :>`, uppercase, z.B. `AA:BB:CC:DD:EE:FF` | Log `mqtt connect async ... client_id=%s` |
| Username | `<MAC ohne :>` (identisch zu Client-ID) | Log `mqtt_client_config server=de.ilifestyle-cloud.com, username=AA:BB:CC:DD:EE:FF` |
| Password | **JWT-Token** aus `config.cloud_account.token` (HS256, ~210 Bytes) | `update_mqtt_discovery_client_config` loggt `self->config.token=eyJ…` direkt vor `mosquitto_username_pw_set`-Aufruf |
| Keepalive | Mosquitto-Default (60 s); kein expliziter Aufruf von `mosquitto_*_keepalive` im Binary sichtbar | — |
| QoS (sub/pub) | **QoS 0** (kein QoS-Argument in Wrapper-Calls; Mosquitto-Standardpfad) | Binary-Strings, keine `QOS`/`qos`-Konstanten |
| Will-Message | **keine** | keine `mosquitto_will_set`-Strings im Binary |
| Retain | **nein** für Publishes (Format-String enthält kein retain-Flag) | — |
| Clean-Session | true (Default) | — |
| Heartbeat (lokal) | Schreibt mtime auf `/var/run/avlink-mqtt.heartbeat` — **kein** MQTT-PINGREQ, sondern lokaler Liveness-File für `monitor.lua` | Binary + Live-FS |

### Token-Format

```
header  = {"alg":"HS256","typ":"JWT"}
payload = {"app":1,"uid":"u00c0000000022cd","ugp":4,
           "did":"AA:BB:CC:DD:EE:FF","dmd":"AVL20P","dtp":6,"iat":1779368227}
```

→ Token wird beim REST-Login (`POST /api/login`) ausgestellt und in `config.cloud_account.token` gespeichert. **Wird nicht refresht** — bleibt bis zum nächsten `loginPost` (= leerer Token in DB) konstant. Bei einem Replica-Broker können wir das Secret frei wählen, weil der **Broker** das Token validieren muss, nicht das Device.

### Reconnect-Verhalten

- `mqtt_client_connect_callback` wird bei jedem Verbindungserfolg geloggt. In den Live-Logs sieht man Reconnects ca. alle 5–15 min (vermutlich Cloud-seitige Idle-Disconnects oder Keepalive-Misses).
- Trigger für manuellen Reload: `AT+B RELOAD mqtt` via lokalem TCP-Port `10086` (siehe `notify_mqtt_client_start tcp-server.c:378`).
- `cloudDevice.lua` und `autoSync.lua` triggern beide nach DB-Update einen Reload via dieses TCP-Kommando + `AT+B RELOAD icloud` (Port 60000).

---

## 2. Topics Overview

| Topic-Pattern | Sub / Pub | Beispiel-Wert | Zweck |
|---|---|---|---|
| `<MAC>` | **subscribed** by GW | `AA:BB:CC:DD:EE:FF` | Eingehende Befehle (App → Cloud → GW) |
| `<userSessionId>` | **published** by GW | `23e2f5277dd64378bcc2b45d8a76386b` | Antwort/Status zurück an den App-Aufrufer (Topic = `from`-Feld der Inbound-Message) |

- Topic-Namen sind **case-sensitive** und werden **literal** als String benutzt (keine Wildcards, keine Hierarchie).
- Es gibt **kein** Broadcast-Topic für mehrere GWs zugleich — jede App-Instanz spricht 1:1 mit genau einem GW über das MAC-Topic.
- Die `userSessionId` (32-hex) ist offenbar pro App-Login eindeutig; sie kommt **aus der App** (Cloud generiert sie beim Login). Das GW lernt sie zur Laufzeit aus dem `from`-Feld einer Inbound-Message und benutzt sie für die Antwort.

> **Praktischer Hinweis für Replica:** Der lokale Broker muss keinen ACL haben; jeder Client kann auf jedem Topic publishen/subscriben. Falls man ACL will: GW darf `<MAC>` (sub) + `<userSessionId>` (pub); App darf umgekehrt.

---

## 3. Inbound (GW abonniert `<MAC>`)

### Beobachtete Live-Messages

Aus `/customer/share/usr-log.log` (Zeitraum 2026-05-21 15:05 – 23:32):

```json
// "Klingeln + Live-Stream starten"
topic=AA:BB:CC:DD:EE:FF
{"action":"monitor","from":"23e2f5277dd64378bcc2b45d8a76386b","tag":"1779368751290","ctrl":"1","key_index":1,"duration":60}

// "Live-Stream beenden"
topic=AA:BB:CC:DD:EE:FF
{"action":"monitor","from":"23e2f5277dd64378bcc2b45d8a76386b","tag":"1779368754022","ctrl":"F","key_index":1,"duration":60}
```

### Inbound-Schema (`action: "monitor"`)

| Feld | Typ | Werte | Bedeutung |
|---|---|---|---|
| `action` | string | `"monitor"` \| `"OPEN DOOR"` | Befehls-Verb. `"OPEN DOOR"` (Großschreibung!) ist eine separate Action für Türöffnung **innerhalb** einer aktiven Monitor-Session — live in 2026-05-22-Sniff von 3 fremden GWs bestätigt. Geht durch denselben `on_receive_monitor`-Handler. |
| `from` | string (16-hex \| 32-hex) | z.B. `23e2f5277dd64378bcc2b45d8a76386b` ODER `0c5f70c25cb6eee3` | Sender-Session, wird als Antwort-Topic verwendet. **Beide Längen live beobachtet** — vermutlich verschiedene App-Versionen / Builds. |
| `tag` | string (ms-Timestamp) | z.B. `"1779368751290"` | Request-Korrelations-ID — wird in **jeder** Response 1:1 echo'd. **NICHT** in `OPEN DOOR`-Payload — die hat nur `action`+`from`. |
| `ctrl` | string | `"1"` = start; `"0"` = idle/init; `"F"` = finish/hangup; **`"N"` = Next (Cam-Switch in laufender Session)** | Steuer-Sub-Verb. `"N"` 2026-05-22 live entdeckt. |
| `key_index` | int | `1`, `2`, … | Welche Klingeltaste / welches Türrelais |
| `duration` | int (Sekunden) | typ. `60` | Wie lange Monitor/Türöffner aktiv |

#### Beispiel: `OPEN DOOR` Action (live observed 2026-05-22)

```json
topic = <GW_MAC>
{"action": "OPEN DOOR", "from": "<App_UUID>"}
```

Wird typischerweise mitten in einer aktiven Monitor-Session gesendet (App-User klickt Tür-öffnen-Button während Live-Stream läuft). **Kein `tag` nötig** — die Response geht trotzdem an `from` mit dem `tag` des aktuellen Monitor-Cycles.

### Im Binary deklarierte (aber im aktuellen Build NICHT aktiv) Actions

`avlink-server.c`-Strings:

| Action | Inbound-Schema | Handler-Funktion | Status 2026-05-22 |
|---|---|---|---|
| `OPEN DOOR` | `{"action":"OPEN DOOR","from":"<uuid>"}` | `on_receive_monitor` (open-door-shortcut) | **✅ AKTIV** — live in 3 fremden GW-Sessions beobachtet (15min globaler `#`-Sniff) |
| `CTRL` | `{"action":"CTRL","event":{"relay":"<id>"}}` | `on_receive_ctrl_relay_state` — schaltet Relay `<id>` für `relay.duration_a`/`relay.duration_b` Sekunden | ❌ Inbound nicht beobachtet (1× CTRL-OUT-Sync von fremder UID gesehen — `{"timestamp":..,"action":"CTRL","event":{"switch":"off"}}`) |
| `STATE` | (Query, kein Body) | `on_receive_query_relay_state` — antwortet mit `STATE`-Publish | ❌ nicht beobachtet |
| `UPGRADE` | Trigger-Payload (Format nicht im Binary sichtbar) | Führt `AT+B UPGRADE %d` aus → startet `lua /customer/share/firmware_upgrade.lua` | ❌ nicht beobachtet |
| `UPDATE` | (von Cloud → GW erwartet, Format nicht beobachtet) | `on_receive_device_update` | ❌ nicht beobachtet |
| `SYNC` | `{"action":"SYNC"}` auf `<MAC>/ctrl` | (Heartbeat — vermutl. von Companion-Apps oder GW selbst) | **✅ AKTIV** — Heartbeats von ~Hundert fremden GWs alle paar Sekunden beobachtet |

### Dispatch-Logik

`mqtt_client_message_callback → mqtt_client_action_handler_filter` parsed `action` und routet:

```
"monitor" → on_receive_monitor()      → AT+B UART monitor <key_index> <ctrl> <duration>  → Bus/SIP/Relay
"CTRL"    → on_receive_ctrl_relay_state() → AT+B UART unlock <key_index>                  → Relay
"STATE"   → on_receive_query_relay_state() → Publish STATE-Response
"UPDATE"  → on_receive_device_update()
```

---

## 4. Outbound (GW publisht auf `<userSessionId>`)

### Beobachtete Live-Messages

```json
// Sofort-ACK auf "monitor start"
topic=23e2f5277dd64378bcc2b45d8a76386b
{"tag":"1779368751290","action":"monitor","response":"ok"}

// Nach Ablauf der duration / nach erfolgreichem Türöffnen
topic=23e2f5277dd64378bcc2b45d8a76386b
{"tag":"1779376366590","action":"monitor","response":"finish"}

// Fehler (Bus-Timeout, SIP nicht registriert, Hardware-NACK)
topic=23e2f5277dd64378bcc2b45d8a76386b
{"tag":"1779369469738","action":"monitor","response":"err"}
```

### Outbound-Schema (Monitor-Response)

| Feld | Typ | Werte | Bedeutung |
|---|---|---|---|
| `tag` | string | echo aus Inbound | Korrelations-ID |
| `action` | string | `"monitor"` | echo aus Inbound |
| `response` | string | `"ok"` \| `"finish"` \| `"err"` | Status |

**Response-Lebenszyklus (aus den Logs rekonstruiert):**

```
T+0     INBOUND  action=monitor ctrl=1            (App will Live-Stream + ggf. Tür auf)
T+0.3s  OUTBOUND response=ok                      (GW hat Bus-Befehl abgesetzt)
T+60s   OUTBOUND response=finish                  (duration abgelaufen, GW schließt Stream)

oder Fehler-Pfad:
T+0     INBOUND  action=monitor ctrl=1
T+5s    OUTBOUND response=err                     (alle ~5s retry-Bursts während timeout-Phase)
```

> Auffällig in den Logs: Bei Fehler wird der **gleiche** `tag` mehrfach (alle ~1–5 s) mit `response=err` republished. Vermutlich Retry-Loop im `STATE_MONITOR`-State-Machine bis Timeout (siehe `STATE_MONITOR %d timeout.`-Log-String). Das ist für eine HA-Integration relevant: **Inbound-Events müssen dedupliziert werden über `tag`**.

### Empty-Tag-Response

In den Logs findet sich exakt einmal:
```
topic=, message={"tag":"","action":"monitor","response":"ok"}
```
Das ist ein Edge-Case bei dem die `from`-Session der vorigen Message bereits "verloren" war (vermutlich Reconnect) → GW publisht ins leere Topic. Replica-Broker sollte solche Publishes still droppen.

### Im Binary deklarierte Outbound-Formats

`avlink-server.c`-Format-Strings:

| Format | Action | Trigger | Beobachtet? |
|---|---|---|---|
| `{"tag":"%s","action":"%s","response":"%s"}` | beliebige (echo) | Jede Inbound-Action-Verarbeitung | **JA** (monitor) |
| `{"action":"STATE","event":{"relay":"%d%d"}}` | `STATE` | Antwort auf Relay-Query | nein (im aktuellen Build) |
| `{"action":"CTRL","event":{"relay":"%s"}}` | `CTRL` | (eigentlich Inbound, aber Format ist auch im Outbound-Path?) | nein |
| `{"action":"UPDATE","event":{"deviceID":"%s","state":%d}}` | `UPDATE` | Status-Push (online, lock-state change) | nein |

---

## 5. Action-Reference (komplett)

| Verb | sub/pub | Inbound-Felder | Outbound-Felder | AT+B-Mapping | HA-Equivalent |
|---|---|---|---|---|---|
| `monitor` ctrl=`1` | sub | `from`,`tag`,`key_index`,`duration` | `tag`,`action=monitor`,`response=ok\|finish\|err` | `AT+B UART monitor <key_index> 1 <duration>` (öffnet Cam-Stream + Bus-Ringback) | trigger: **Klingel-Ruf gestartet** |
| `monitor` ctrl=`F` | sub | dito | dito | `AT+B UART monitor <key_index> F <duration>` (hangup) | trigger: **Klingel-Ruf beendet (App-seitig)** |
| `monitor` ctrl=`0` | sub | dito | dito | `AT+B UART monitor <key_index> 0 <duration>` (idle) | (selten, vmtl. Pre-Init) |
| `unlock` (impl.) | (via monitor-handler) | über `on_receive_monitor` mit ctrl=spez. Wert oder über `CTRL` | dito | `AT+B UART unlock <key_index>` | **service: open door relay** |
| `CTRL` (legacy) | sub | `event.relay`=`"<id>"` | `tag`,`response=ok\|err` | `AT+B UART unlock <relay_id>` | service: open door relay |
| `STATE` (legacy) | sub | (query) | `event.relay="<2-bit-state>"` | `AT+B B RELAY` (get state) | sensor: door-relay state |
| `UPDATE` (legacy) | pub | — | `event.deviceID="<MAC>",event.state=<n>` | (kein Bus-Befehl, reines Pub) | binary_sensor: online/offline |
| `UPGRADE` | sub | (Trigger-Payload, vermutl. `{"action":"UPGRADE","version":"…"}`) | (kein Response beobachtet) | `AT+B UPGRADE <n>` → `lua firmware_upgrade.lua` | update entity (read-only) |

**Wichtig:** `monitor` ist **kein** reines "Kamera einschalten". Aus dem Bus-Verhalten (siehe `bus-protocol-notes.md`):
- `ctrl=1` mit `key_index=1` löst gleichzeitig **Bus-Ringback an die Sprechstelle** UND **Türöffner-Vorbereitung** UND **Stream-Aktivierung** aus. Es ist effektiv das "App klingelt rein"-Sammelkommando.
- Ein expliziter Türöffner-Befehl aus der App sendet `monitor` mit `ctrl=1` + nach Verbindung dann **separat** `AT+B UART unlock` lokal (oder via SIP-DTMF). MQTT sieht nur das `monitor`-Event.

---

## 6. HA-Integration-Schema

### Empfohlene HA-Sensors / Triggers

| HA-Entity | Quelle | Update-Trigger | Notes |
|---|---|---|---|
| `binary_sensor.villa_gw_doorbell_pressed` | sub `<MAC>` topic, filter `action=monitor` AND `ctrl=1` | edge-trigger ON für 2 s, dann auto-OFF | Trigger Doorbell-Push |
| `binary_sensor.villa_gw_call_active` | sub `<MAC>`, ctrl=1 → ON / ctrl=F → OFF | tracked über `tag` als active-call-ID | Anzeige im Dashboard |
| `sensor.villa_gw_last_call_session` | extract `from`-Feld | bei jeder neuen Session | Debugging |
| `sensor.villa_gw_last_response` | sub `<userSessionId>` (own MAC oder wildcard `#`) | filter on `response` | Status der letzten Aktion (`ok\|finish\|err`) |
| `switch.villa_gw_door_relay_<n>` | pub auf `<MAC>` action `CTRL` mit `event.relay="<n>"` (oder via Bus AT-Cmd lokal) | service-call | Türöffner (legacy CTRL-action) |
| `binary_sensor.villa_gw_online` | MQTT LWT (gibt's nicht!) → alternativ: Heartbeat-File `/var/run/avlink-mqtt.heartbeat` mtime | poll 30 s | siehe note unten |
| `update.villa_gw_firmware` | sub `<MAC>` action `UPGRADE` | passive | informativ |

### MQTT-Discovery-Config-Beispiel (für HA-Replica)

```yaml
mqtt:
  binary_sensor:
    - name: "Villa GW Doorbell"
      state_topic: "AA:BB:CC:DD:EE:FF"
      value_template: >-
        {% set p = value_json %}
        {% if p.action == 'monitor' and p.ctrl == '1' %}ON{% else %}OFF{% endif %}
      payload_on: "ON"
      payload_off: "OFF"
      off_delay: 5
      qos: 0
    - name: "Villa GW Call Active"
      state_topic: "AA:BB:CC:DD:EE:FF"
      value_template: >-
        {% set p = value_json %}
        {% if p.action == 'monitor' and p.ctrl == 'F' %}OFF
        {% elif p.action == 'monitor' and p.ctrl == '1' %}ON
        {% endif %}

  sensor:
    - name: "Villa GW Last Response"
      state_topic: "+"                # alle Topics, da Response-Topic dynamisch
      value_template: >-
        {% if value_json.response is defined %}{{ value_json.response }}{% endif %}
      json_attributes_topic: "+"
```

> Achtung: HA-MQTT-Integration erlaubt nur `+`/`#`-Wildcards, kein dynamisches Topic-Substitution. Für den **Response-Channel** (`<userSessionId>`) ist ein Catch-All `#` oder ein Statisches Topic per Session nötig. In der Praxis lohnt sich das nur, wenn man Open-Door-Status braucht — der Klingel-Trigger selbst kommt schon aus Inbound `<MAC>`.

### Was geht NICHT direkt via MQTT

- Türöffner aktiv vom HA aus triggern: Das GW erwartet eine `monitor`/`CTRL`-Inbound auf `<MAC>` **mit einer gültigen `from`-Session**. Ohne valides Session-Mapping verarbeitet das GW die Action zwar, kann aber keine Response zurückrouten (Empty-Tag-Edge-Case). Funktional klappt's trotzdem (Bus-Cmd wird ausgeführt) — aber feedback bleibt aus. **Workaround:** HA generiert eine eigene 32-hex-Session-ID und subscribed darauf.
- Live-Stream starten: Kein MQTT-Trigger, sondern RTSP-Pull direkt vom GW (`rtsp://<gw-ip>/live.sdp`) — siehe `cloud_sync.md` §8.

---

## 7. Cloud-Replica Plan

### Minimum Viable Broker

Für **rein lokales Klingel-Eventing** in HA reicht:

```conf
# /etc/mosquitto/conf.d/villa-gw.conf
listener 8883
cafile     /etc/mosquitto/ca.crt
certfile   /etc/mosquitto/de.ilifestyle-cloud.com.crt   # CN/SAN = de.ilifestyle-cloud.com
keyfile    /etc/mosquitto/de.ilifestyle-cloud.com.key
require_certificate false
allow_anonymous true              # GW-Token wird vom Broker nicht geprüft (oder JWT-Plugin)
```

Plus DNS-Override `de.ilifestyle-cloud.com → <local-broker-ip>` (Pi-Hole/AdGuard) und CA in `/customer/share/ca-certificates.crt` auf dem GW appended.

### Was der Broker tun muss

| Funktion | Pflicht? | Notes |
|---|---|---|
| TCP 8883, TLS 1.2 | **JA** | sonst kein Connect |
| Self-Signed Cert für `de.ilifestyle-cloud.com` | **JA** | CN muss matchen (hostname-verify ist an) |
| Username/Password validieren | NEIN | Mosquitto kann `allow_anonymous true`; das GW liefert MAC + JWT, beides wird vom Broker akzeptiert wenn anonymous erlaubt |
| Topic-Subscribe auf `<MAC>` zulassen | JA | Default ja, kein ACL |
| Publish auf `<userSessionId>` zulassen | JA | Default ja |
| MQTT-Discovery / HA-Bridge | optional | nur wenn HA das Topic nicht direkt subscribt — siehe oben |

### Was am GW gemacht werden muss

1. **CA installieren:**
   ```bash
   scp local-ca.crt root@203.0.113.10:/tmp/
   ssh root@203.0.113.10 'cat /tmp/local-ca.crt >> /customer/share/ca-certificates.crt'
   ```
2. **Optional: `mqtt_server` in DB überschreiben** (falls DNS-Override nicht möglich):
   ```bash
   sqlite3 /customer/share/avl20.db "UPDATE config SET item='{...,\"mqtt_server\":\"203.0.113.140\",...}' WHERE name='sip';"
   ```
3. **Reload triggern:**
   ```bash
   echo 'AT+B RELOAD mqtt' | nc 127.0.0.1 10086
   ```
4. **Verifikation:** Im Broker-Log sollte erscheinen:
   ```
   New client connected from 203.0.113.10 as AA:BB:CC:DD:EE:FF (c1, k60, u'AA:BB:CC:DD:EE:FF')
   ```

### Risiken / Constraints

| Risiko | Mitigation |
|---|---|
| Cloud invalidiert JWT bei nächstem Reconnect → Broker müsste neuen Token akzeptieren | Broker akzeptiert beliebigen Token (`allow_anonymous` oder Always-Accept-Plugin) |
| MAC ändert sich nie, ABER nach Factory-Reset ist der MQTT-Server-String leer → autoSync.lua würde versuchen, echte Cloud zu kontaktieren | Replica muss auch `/api/login` + `/api/device` beantworten — siehe `cloud_sync.md` §7 |
| App spricht weiterhin echte Cloud → Klingeln über App geht nicht mehr | Bewusst akzeptieren (Goldweg). Alternative: HA Companion App + RTSP-Card statt iLifestyle-App. |
| GW-Reconnect-Burst (sehe `mqtt connect ok` mehrfach hintereinander) verursacht doppelte Events | HA-Side: `tag`-basierte Deduplication |

---

## 8. Live-Log-Auszug zur Verifikation

Aus `/customer/share/usr-log.log`, alle Events vom 2026-05-21 nach `mqtt`-Filter (gekürzt auf relevante Zeilen):

```
14:57:08.951  mqtt connect ok
15:05:51.352  topic=AA:BB:CC:DD:EE:FF payload={"action":"monitor","from":"23e2f5277dd64378bcc2b45d8a76386b","tag":"1779368751290","ctrl":"1","key_index":1,"duration":60}
15:05:51.357  on_receive_monitor: state=1, from=23e2f5277dd64378bcc2b45d8a76386b, key_index=1
15:05:54.092  topic=AA:BB:CC:DD:EE:FF payload={...ctrl:"F", tag:1779368754022...}
15:05:54.096  on_receive_monitor: state=8

17:12:46.954  topic=AA:BB:CC:DD:EE:FF payload={...ctrl:"1", tag:1779376366590...}
17:12:47.266  send mqtt message: remote_topic=23e2f5277dd64378bcc2b45d8a76386b, message={"tag":"1779376366590","action":"monitor","response":"ok"}
17:13:47.778  send mqtt message: remote_topic=23e2f5277dd64378bcc2b45d8a76386b, message={"tag":"1779376366590","action":"monitor","response":"finish"}

# Error-Pfad mit Retry-Burst (kein "ok" zwischendurch):
17:11:55.793  remote_topic=23e2f5277dd64378bcc2b45d8a76386b, message={"tag":"1779369469738","action":"monitor","response":"err"}
17:11:57.124  remote_topic=23e2f5277dd64378bcc2b45d8a76386b, message={"tag":"1779369469738","action":"monitor","response":"err"}
17:12:01.896  remote_topic=23e2f5277dd64378bcc2b45d8a76386b, message={"tag":"1779369469738","action":"monitor","response":"err"}
...  (alle ~1-5s wiederholt bis Timeout)
```

### Beobachtete State-Werte (`on_receive_monitor: state=N`)

| state | Bedeutung (aus binary state-machine `STATE_*`-Strings) |
|---|---|
| `0` | IDLE / pre-init |
| `1` | CALLING (Bus-Ring outbound) |
| `7` | RINGING / RINGBACK (Sprechstelle klingelt) |
| `8` | TALKING_BUS / finished |

---

## Anhang A — Vollständige Binary-String-Liste (MQTT-bezogen)

Aus `strings /customer/app/sbin/avlink | grep -i mqtt`:

```
mosquitto_publish, mosquitto_subscribe, mosquitto_subscribe_callback_set,
mosquitto_tls_set, mosquitto_username_pw_set, mosquitto_lib_init,
mosquitto_loop_write, mosquitto_loop_start

Format-Strings (Payloads):
  {"tag":"%s","action":"%s","response":"%s"}
  {"action":"STATE","event":{"relay":"%d%d"}}
  {"action":"CTRL","event":{"relay":"%s"}}
  {"action":"UPDATE","event":{"deviceID":"%s","state":%d}}

Handler-Funktionen:
  mqtt_client_message_callback
  mqtt_client_connect_callback
  mqtt_client_subscribe_callback
  mqtt_client_log_callback
  mqtt_client_action_handler_filter
  on_received_mqtt_message
  on_receive_monitor
  on_receive_ctrl_delay
  on_receive_query_relay_state
  on_receive_ctrl_relay_state
  on_receive_device_update
  response_device_update_state
  notify_receive_mqtt_message
  notify_mqtt_client_start
  update_mqtt_discovery_client_config
  response_mqtt_message
  check_mqtt_alive

Action-Verben (im Binary kompiliert):
  monitor, CTRL, STATE, UPDATE, UPGRADE, MONITOR, unlock

Filesystem:
  /customer/share/mqtt-client.socket   (Unix-Socket für lokale IPC zur Daemon)
  /var/run/avlink-mqtt.heartbeat       (Liveness-File)
  /customer/share/ca-certificates.crt  (CA-Bundle für TLS)

Control:
  AT+B RELOAD mqtt    (TCP port 10086, triggert mqtt_client_config-Reload)
```

## Anhang B — Quick-Recipe: Replica-Broker in 5 min

```bash
# 1. Mosquitto auf HA-Host installieren (oder als HA-Add-on)
sudo apt install -y mosquitto

# 2. Self-Signed Cert für de.ilifestyle-cloud.com
openssl req -x509 -newkey rsa:4096 -nodes -days 3650 \
  -keyout /etc/mosquitto/server.key -out /etc/mosquitto/server.crt \
  -subj "/CN=de.ilifestyle-cloud.com" \
  -addext "subjectAltName=DNS:de.ilifestyle-cloud.com"

# 3. CA = self-signed cert selbst (für simpel)
cp /etc/mosquitto/server.crt /etc/mosquitto/ca.crt

# 4. Conf
cat > /etc/mosquitto/conf.d/villa-gw.conf <<EOF
listener 8883
cafile     /etc/mosquitto/ca.crt
certfile   /etc/mosquitto/server.crt
keyfile    /etc/mosquitto/server.key
allow_anonymous true
log_type all
EOF

sudo systemctl restart mosquitto

# 5. DNS-Override (Pi-Hole / AdGuard / Router)
#    de.ilifestyle-cloud.com -> <ha-host-ip>

# 6. CA aufs GW kopieren
scp /etc/mosquitto/ca.crt root@203.0.113.10:/tmp/
ssh root@203.0.113.10 'cat /tmp/ca.crt >> /customer/share/ca-certificates.crt; \
                        echo "AT+B RELOAD mqtt" | nc 127.0.0.1 10086'

# 7. Live-Tap
mosquitto_sub -h localhost -p 8883 --cafile /etc/mosquitto/ca.crt -t '#' -v
# -> Beim ersten Türklingeln solltest du sehen:
# AA:BB:CC:DD:EE:FF {"action":"monitor","from":"...","tag":"...","ctrl":"1","key_index":1,"duration":60}
```

Sobald die Subscription `#` Events liefert → HA via `mqtt:` integrieren wie in §6.

---

## Quellen-Cross-Reference

| Erkenntnis | Quelle |
|---|---|
| Topic-Name = MAC | Live-Log `topic=AA:BB:CC:DD:EE:FF` |
| Username = MAC, nicht sip_id | Live-Log `mqtt_client_config server=de.ilifestyle-cloud.com, username=AA:BB:CC:DD:EE:FF` (widerspricht früherer Annahme in `cloud_sync.md` §4!) |
| Password = JWT | Log `update_mqtt_discovery_client_config self->config.token=eyJ…` direkt vor `mosquitto_username_pw_set`-Call |
| Port 8883 (nicht 1883) | `netstat -an` zeigt aktive `203.0.113.10:55132 -> 198.51.100.50:8883 ESTABLISHED` |
| Single Action `monitor` in Praxis | 9 h Live-Log, ~30 Inbound-Messages, alle `action=monitor` |
| Response-Topic = `from`-Feld | Live-Log Pairs: Inbound `from=23e2…` → Outbound `remote_topic=23e2…` |
| Tag-Echo + Multi-Response (ok/finish/err) | Pairs in Live-Log mit identischem `tag` 60 s auseinander (ok→finish) |
| Reconnects ~5-15 min | Mehrere `mqtt connect ok` pro Stunde im Log |
| Cloud-IP = 198.51.100.50 (Alibaba EU) | `netstat`-Output |

> Korrektur an `cloud_sync.md` §4 nötig: dort steht "username = sip_id"; faktisch ist es **die MAC**. Die `sip_id` (z.B. `s00c0000DEVICE_ID`) ist ausschließlich der SIP-Account-Name beim SIP-Server, nicht der MQTT-Username.
