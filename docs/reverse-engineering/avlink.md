# Reverse Engineering: `avlink`

> **Binary:** `/customer/app/sbin/avlink` (ARM 32-bit EABI5, dyn linked, stripped, 80 056 Byte, BuildID `cece8370…`)
> **Build:** GCC 9.1.0, Source-File-Banner `version: 20240729`, build timestamp `May 9 2025 05:12:51`
> **Banner:** `avlink-server`, advertised version `1.0.0` (separate from FW)
> **Rolle:** Zentraler Gateway-Daemon. Konsumiert AT+B-Befehle (TCP 10086), übersetzt sie für uart2d / pjsua, hält MQTT-Cloud-Anbindung, ist die Bus-Call-Statemachine.
>
> **Confidence-Level:** `H` = aus Strings/Symbolen direkt belegt · `M` = stark plausibel, indirekte Belege · `L` = Vermutung, Verifikation empfohlen.

---

## 0. Methodik & Werkzeuge

Quelle der Erkenntnisse:

| Werkzeug                          | Yield                                                                 |
|-----------------------------------|-----------------------------------------------------------------------|
| `file`                            | armv7l ELF, EABI5, stripped, dyn linked                               |
| `nm -D avlink`                    | 128 dyn. Symbole (libc, libpthread, libjansson, libmosquitto, liblua5.1) |
| `nm avlink`                       | `no symbols` — vollständig gestrippt (keine lokalen Symbole)          |
| `strings -a -n 4`                 | 985 Strings (562 ≥8 Zeichen)                                          |
| Cross-Ref zu `uart2d/pjsua/discovery/mimedia` | Port-/Socket-Zuordnung; UDP 9527 **nicht** in avlink     |

Da das Binary gestrippt ist, beruht **die gesamte Funktions-Identifikation auf den `%s %s:%d`-Logging-Patterns** — der Source-Filename und die Funktion stehen wörtlich in jedem Log-Statement (`avlink-master.c`, `avlink-server.c`, `mqtt-client.c`, `pjsip-handler.c`, `tcp-server.c`, `usr-config.c`, `discovery.c`, `usr-log.c`). Daraus rekonstruiert sich die C-Modul-Struktur sauber.

---

## 1. Modul-Übersicht (Source-File-Map)

Aus den Logging-Bannern ableitbar (H):

| `.c`-Datei                | Verantwortung                                                                                                |
|---------------------------|--------------------------------------------------------------------------------------------------------------|
| `avlink-master.c`         | Master-Prozess. Forkt `avlink-server` und `mqtt-client`; Heartbeat-Watchdog (`/var/run/avlink-mqtt.heartbeat`); SIGCHLD/INT/TERM-Handling. |
| `avlink-server.c`         | TCP-10086-Listener (`AT+B`-Befehle), Bus-State-Machine, GPIO-LED-Steuerung, Relay-Logik, MQTT-RX-Handler.    |
| `mqtt-client.c`           | Eigener Prozess (`prctl PR_SET_NAME = "mqtt-client"`). libmosquitto-Cloud-Client + Unix-Domain-Socket-Server. |
| `pjsip-handler.c`         | TCP-Bridge zu `pjsua` auf `127.0.0.1` (Account-Lifecycle, Call-State-Translation, DTMF-OPEN-DOOR).            |
| `tcp-server.c`            | Outbound-TCP-Clients zu `uart2d:10087`, eigener Loopback `127.0.0.1:10086` (self-talk), Firmware-Upgrade-Listener `127.0.0.1:10010`, undokumentierter `127.0.0.1:60000`-Port. |
| `usr-config.c`            | Lua-Loader für `/customer/share/config.lua` (alle Settings → Globals).                                       |
| `discovery.c`             | Client zu `discovery`-Daemon via `/var/run/discovery.socket`, JSON `discovery_config`.                       |
| `usr-log.c`               | Strukturierter Logger (`/var/run/usr-log.socket` + lokale Datei `/customer/share/usr-log.log`).              |

Drei Prozesse zur Laufzeit (H):

```
avlink (avlink-master)
 ├── child  → avlink-server   (TCP 10086 listener)
 └── child  → mqtt-client     (libmosquitto + IPC socket)
```

Heartbeat-Mechanismus (H): `check_mqtt_alive()` im Master prüft `stat()` auf
`/var/run/avlink-mqtt.heartbeat` (mtime-Diff via `difftime`). Wenn `mqtt-client` zu lange nicht geschrieben hat → `kill` und Re-Fork.

---

## 2. Konfigurations-Quelle: `/customer/share/config.lua`

`usr_config_init()` ruft `luaL_dofile("/customer/share/config.lua")` und liest folgende Globals (H, jeder Key direkt im Binary):

```
local_username           sip_nickname           sip_username        sip_password
sip_server               rtp_port_audio         sip_log_level       sip_expires_reg
timeout_calling          timeout_ringing        timeout_talking     timeout_active
timeout_active_a         timeout_call_except    timeout_meshing     timeout_register
oled_label               elock_holdtime         use_mode            man_distance
video_enable             video_rtsp             video_rtmp          version
mac                      birthday               module_oled_enable  local_contacts_mode
mqtt_server              token                  device_id
delay_a   delay_b                                    -- Relay-Haltedauern
button_a  button_b  button_c  button_d                -- 4 Tasten
```

Zusätzlich Lua-Funktion `Get_callee(key)` → liefert für einen Tasten-Index eine Tabelle `{callType, ipAddr, enable, server, account, password, callee}` (H).

Reload-Pfad: `usr_config_reload()` → Hot-Reload bei `AT+B RELOAD ...` (`callList` lädt nur `Reload_call_list()` neu; `mqtt` rebootet die MQTT-Verbindung; `config` macht Full-Reload).

---

## 3. TCP-/UDP-Listener und Sockets

| Endpoint                                | Rolle                                                                                                      | Confidence |
|-----------------------------------------|------------------------------------------------------------------------------------------------------------|-----------|
| TCP `0.0.0.0:10086`                     | **AT+B-Command-Channel** (epoll, primärer Listener). Quelle: `tcp_server fail` + `tcp accept %d %s:%d`.    | H |
| TCP-Client → `127.0.0.1:10086`          | Self-talk: Master schickt nach Re-Init `AT+B RELOAD …` an eigenen Listener.                                | H |
| TCP-Client → `127.0.0.1:10087`          | Outbound zu `uart2d` (Bus-Kommandos). Strings: `AT+B UART unlock %d`, `AT+B UART hook %d` etc.             | H |
| TCP-Client → `127.0.0.1:10010`          | Outbound für Firmware-Upgrade (`tcp_to_firmware_upgrade` → schickt `AT+B UPGRADE %d`, `AT+B RECORD %d`).   | H |
| TCP-Client → `127.0.0.1:60000`          | Undokumentiert: `tcp 60000 connect error`, `tcp 60000 write %d: %s`. Vermutlich zu `mimedia` für Audio-Cmds. | M |
| TCP-Client → `127.0.0.1:?` (pjsua)      | `pjsip_tcp_client` IPC zu pjsua. Server-Port aus config (`pjsip_tcp server_addr=%s, port=%d`).             | H |
| Unix-DGRAM/STREAM `/var/run/usr-log.socket` | Logger-Sink (von Master + Server + mqtt-client geschrieben).                                          | H |
| Unix-STREAM   `/var/run/discovery.socket`   | Client zu `discovery`-Daemon. `discovery_config` / `discovery_ctl` schicken/empfangen JSON-Frames.    | H |
| Unix-STREAM   `/customer/share/mqtt-client.socket` | **IPC-Socket** im `mqtt-client`-Prozess (Server-Seite). Andere Prozesse pushen hier Mqtt-Outgoing-Frames rein. | H |
| Heartbeat-File `/var/run/avlink-mqtt.heartbeat` | mtime-Update als Liveness-Signal (von mqtt-client geschrieben, von Master gelesen).               | H |

**UDP 9527: existiert in avlink NICHT** (kein `recvfrom`/`bind` auf 9527, keine Strings). Der LAN-Discovery-Listener ist `discovery` (UDP-Multicast `239.255.255.240:6210`), siehe `docs/reverse-engineering/small_daemons.md`. (H)

---

## 4. AT+B-Befehle (TCP 10086)

Parser-Regex (literal im Binary, H):

```
AT\+B (\w+) *(.*)
```

In `tcp_cmd_handler` wird das Subcommand (\w+) gegen einen Dispatcher gematcht. **Alle im Binary belegten Subcommands:**

| Subcommand    | Args                                          | Antwort / Side-Effect                                                                                                                                                  | C |
|---------------|-----------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---|
| `UART`        | sub-cmd + args (zweite Regex `(\w+) *(.*)`)   | Bridge zum Bus (siehe 4.1).                                                                                                                                            | H |
| `RELOAD`      | `callList` \| `mqtt` \| `config` \| `network` | Hot-Reload-Befehle. `network` triggert ggf. `discovery restart`.                                                                                                       | H |
| `CHECKSIP`    | `3 %d`                                        | SIP-Account-Status-Anfrage (`AT+B CHECKSIP 3 %d` an pjsua via TCP-Helper).                                                                                              | H |
| `APPLICATION` | Status-Query                                  | Liefert JSON: `{"state":%d, "sip":%d, "call":[%d]}` (H).                                                                                                                | H |
| `SYSTEM`      | Status-Query                                  | Liefert großes JSON mit `version`, `light_in`, `network`, `door_in`, `door_out`, `man_in`, `oled`, `io0_out`, `io0_in`, `rs485`, `factory_in`, `led`, `flash`, `mem`, `uptime`, `loads` (Format-String in §6). | H |
| `UPGRADE`     | `%d`                                          | Forwarded an `127.0.0.1:10010` als `AT+B UPGRADE %d`. Startet zusätzlich `lua /customer/share/firmware_upgrade.lua &` (Lua-Stage-2-Upgrader).                          | H |
| `RECORD`      | `%d`                                          | Forwarded an Port 10010 als `AT+B RECORD %d`. Sprachansagen / Recording-Trigger.                                                                                       | M |
| `MUSIC`       | path                                          | Format `%d %s`, route `type/path`. Audio-Playback via mimedia-Port (60000?).                                                                                           | M |
| `ELOCK`       | —                                             | E-Lock-Befehl (Türschloss). Hängt zusammen mit `elock_holdtime` aus config.                                                                                            | M |
| `KEY`         | —                                             | Tasten-Event (vermutlich Hardware-Test); kein Logging-String, aber Symbol-Hinweis in `pjsip-handler` (`call_btn_trigger`).                                              | L |
| `MONITOR`     | siehe `MONITOR`-Pfad                          | Wird auch via MQTT erreicht (`on_receive_monitor`); AT-Variante liefert Kamera-Switch.                                                                                  | H |
| `WIFI`        | `connect %s`                                  | `AT_WIFI=%s` → System-Call `wpa_supplicant`-Reconfig + `/etc/init.d/discovery restart` (H).                                                                              | H |
| `check`       | `ip`                                          | `AT+B check ip` — Diagnose-Print; selten benutzt.                                                                                                                       | L |

### 4.1 `AT+B UART …` (Bus-Subcommands)

In `voip_echo_handler` werden Bus-Commands gebaut und an `uart2d` (TCP 10087) gesendet (alle H, Format-Strings 1:1 belegt):

| Cmd                                      | Format-String an uart2d                     | Notes                                                |
|------------------------------------------|---------------------------------------------|------------------------------------------------------|
| `AT+B UART unlock %d`                    | `AT+B UART unlock %d`                       | Türöffner-Relay-Trigger.                            |
| `AT+B UART hook %d`                      | `AT+B UART hook %d`                         | "Off-hook" / "annehmen".                            |
| `AT+B UART hang %d`                      | `AT+B UART hang %d`                         | Auflegen.                                            |
| `AT+B UART monitor %d %s %d`             | …                                           | Kamera-Anforderung (Index, RTSP/IP, Port).          |
| `AT+B UART switchCamera`                 | …                                           | Quelle umschalten.                                  |
| `AT+B UART intercom %d %d`               | Anrufer-/Adressat-Index → `0100000%04d00` Datagramm | Intercom Indoor-zu-Indoor.                   |
| `AT+B UART call %d %d`                   | `0200000%04d00`                              | Bus-Call initiieren.                                |
| `AT+B UART ringback`                     | (über state)                                | "Es klingelt"-Ack.                                  |

Antworten von uart2d werden in den State-Machine-Strings als `response=ok` / `response=err` / `response=finish` ausgewertet (H — siehe `AT_UART_MONITOR response=%s`, `AT_UART_HOOK response=%s` etc.). Genau dies erklärt die Memory-Note `project_villa_gw_ttys1_readers_break_commands` (parallele cat-Reader stehlen den uart2d-RX-Stream).

### 4.2 Response-Format an TCP-10086-Client

JSON (H, exakter Format-String im Binary):

```c
"{\"tag\":\"%s\",\"action\":\"%s\",\"response\":\"%s\"}"
```

`response`-Werte: `ok`, `err`, `none`, `deny`, `allow`, `finish`. Status `1422933` taucht als Magic-Wert auf (M — vermutlich Vendor-Code "wakeup"/"granted").

---

## 5. MQTT-Cloud-Client (`mqtt-client.c`)

Eigener Prozess, `prctl PR_SET_NAME "mqtt-client"`, libmosquitto-basiert.

### 5.1 Mosquitto-API-Nutzung (H, direkt aus `nm -D`)

```
mosquitto_lib_init / mosquitto_new
mosquitto_connect_async
mosquitto_connect_callback_set   → mqtt_client_connect_callback
mosquitto_message_callback_set   → mqtt_client_message_callback
mosquitto_subscribe_callback_set → mqtt_client_subscribe_callback
mosquitto_log_callback_set       → mqtt_client_log_callback
mosquitto_username_pw_set
mosquitto_tls_set                ← Cert: /customer/share/ca-certificates.crt
mosquitto_tls_opts_set           ← tls-Version: "tlsv1.2"
mosquitto_loop_start / read / write / misc / stop
mosquitto_publish / subscribe
mosquitto_socket / destroy
```

### 5.2 TLS / Cert-Pinning (kritisch)

**Beleg im Binary (H):**

```
/customer/share/ca-certificates.crt        ← CA-Bundle
tlsv1.2                                    ← min TLS version
"Could not set TLS configuration: %d"
```

**Ergebnis:** `mosquitto_tls_set(mosq, "/customer/share/ca-certificates.crt", NULL, NULL, NULL, NULL)` — d.h. CA-Bundle wird gesetzt, **kein Client-Cert**, **kein** expliziter `verify`-Callback, **kein** TLS-PSK, **kein** `pinned_cert`, **kein** Fingerprint-Vergleich.

* `mosquitto_tls_opts_set` wird aufgerufen — Optionen-Werte aber nicht direkt sichtbar. **Default** ist `cert_reqs=SSL_VERIFY_PEER` mit Host-Name-Check, d.h. **Standard-CA-Validation gegen das Bundle.** Kein zusätzliches Pinning. (H für "kein Pinning", M für "Hostname-Check aktiv")
* **MITM-Risiko:** Wer den Inhalt von `/customer/share/ca-certificates.crt` ersetzen oder eine eigene CA injizieren kann (z.B. via Firmware-Update oder R/W-Mount), kann den MQTT-Traffic abfangen. Pinning müsste über `mosquitto_tls_opts_set(MOSQ_OPT_TLS_ALPN/…)` oder eigene Callback ergänzt werden — passiert hier **nicht**.
* Zusätzlich: `mqtt port=1883 socket=%d` ist im Binary — **es existiert ein Code-Pfad für unverschlüsseltes 1883!** D.h. wenn der Server-Port aus `mqtt_server` (config.lua) `1883` ist, wird TLS evtl. nicht aufgesetzt. Hier ist ein Downgrade möglich falls man `mqtt_server` modifiziert. (M — Logik ist ohne Disassembly nicht 100% klärbar, aber 1883 ist hartcodiert in einem Log-Statement).

### 5.3 MQTT-Server-Config

* `mqtt_server` aus `config.lua` (H — Logstring: `mqtt_client_config server=%s, username=%s`).
* `username` und `password` (vermutlich aus `mqtt_server`-String mit Format `%d s=%s u=%s p=%s` oder per `mac` + `token`-Pair) → siehe Logging.
* `client_id` = `mac` (M, aus `self->config.mac` in mqtt-connect-Log).

Der mqtt-Connect-Log-String:
```
mqtt connect async ret=%d, server=%s, client_id=%s
```

### 5.4 Topic-Patterns

**Wichtig:** Konkrete Topic-Strings sind **dynamisch aufgebaut** (per `%s%s%s%s` im Format-Builder, H) und tauchen daher nicht statisch im Binary auf. Was statisch belegt ist:

| Wert / Symbol           | Bedeutung                                                                                  | C |
|-------------------------|--------------------------------------------------------------------------------------------|---|
| `cloud.Account`         | Topic-Komponente / Subscription-Root (vermutet — wird gegen `action`-Wert geprüft).        | H |
| `local`                 | Quellen-Discriminator (lokaler Aufruf vs. Cloud).                                          | H |
| Logs: `topic=%s, payload=%s` | Universeller Receive-Logger.                                                          | H |
| Logs: `send mqtt message: ret=%d, remote_topic=%s, message=%s` | Universeller Publish-Logger.                          | H |
| Logs: `publish payloadlen=%d, payload=%s` | Publish-Pfad innerhalb mqtt-client.                                            | H |
| Logs: `upgrade message: …` | Separater Topic-Pfad für Firmware-Upgrade-Push-Nachrichten.                            | H |

Belegte **Action-Verben** (im JSON-Payload, Schlüssel `action`, H):

```
OPEN DOOR        → on_receive_monitor / open-door-shortcut
MONITOR (monitor)→ Kamera-Stream anfordern
UPDATE           → response_device_update_state
STATE            → on_receive_query_device_state
CTRL             → on_receive_ctrl_relay_state / on_receive_ctrl_delay
```

Belegte **JSON-Felder** (Payload-Keys, H):

```
action, from, key_index, ctrl, duration, index, relay, event,
request, receive, callType, ipAddr, enable, server, account,
password, callee, response, tag
```

### 5.5 Outgoing-Payload-Templates (exakt im Binary, H)

```
{"action":"STATE","event":{"relay":"%d%d"}}
{"action":"CTRL","event":{"relay":"%s"}}
{"action":"UPDATE","event":{"deviceID":"%s","state":%d}}
{"tag":"%s","action":"%s","response":"%s"}
```

### 5.6 IPC-Socket `/customer/share/mqtt-client.socket`

Im `mqtt-client`-Prozess geöffnet als Unix-STREAM-Listener (`bind` + `listen(sfd, 1)`).
Andere Komponenten (`avlink-server`, vermutlich auch `pjsua` über `mqtt-client.socket` literal in pjsua-Binary) schreiben Frames zum Outgoing-Publish hierein.

Frame-Format (M):

```
struct {
    uint32 dataBuf_len;   // "dataBuf len=%d, type=%d"
    uint32 type;          // Mqtt-Action-Tag
    uint8  payload[len];  // JSON-Body
}
```

Receive-Pfad: `mqtt_client_action_handler_filter` filtert dann nach `type`, baut Topic + Payload, ruft `mosquitto_publish`. (M)

### 5.7 JWT-Token-Generation

**Ergebnis: NEIN. avlink generiert oder verifiziert KEIN JWT.** (H)

* Keine `json_*`-Symbole außer jansson-basics, keine `hmac`/`SHA`/`base64`-Bibliothek dyn. gelinkt.
* `token` aus `config.lua` wird wörtlich verwendet — entweder als **MQTT-Password** (in `mosquitto_username_pw_set`) oder im Mqtt-Server-Connect-String (`%d s=%s u=%s p=%s` — Format `port=%d server=%s user=%s pass=%s`).
* JWT-Validation passiert **nicht** in avlink. Falls eine Cloud-Komponente JWTs verwendet, wäre der Code in `pjsua` oder einem Companion-Tool — nicht hier.

---

## 6. Bus-Logic / State-Machine

In `avlink-server.c` (H, alle `STATE_*`-Namen statisch belegt):

```
STATE_REGISTER_SUCCESS         ← SIP-Account erfolgreich registriert
STATE_REGISTER_FAIL
STATE_CALLING        timeout → "STATE_CALLING timeout."
STATE_RINGING        timeout → "STATE_RINGING timeout."
STATE_RINGBACK       timeout → "STATE_RINGBACK timeout."
STATE_MONITOR  %d    timeout
STATE_TALKING_BUS    timeout → "STATE_TALKING_BUS timeout."
STATE_TALKING                  (sip-call)
```

Timeouts werden aus `config.lua`-Werten gespeist (`timeout_calling`, `timeout_ringing`, `timeout_talking`, `timeout_active`, `timeout_active_a`, `timeout_call_except`, `timeout_meshing`, `timeout_register`, `sip_expires_reg`).

Übergangs-Trigger:

| Trigger                              | Quelle / Effekt                                                  |
|--------------------------------------|------------------------------------------------------------------|
| `call_btn_trigger(key_index)`        | Hardware-Taste → Lua-`Get_callee` → `make_calls` (sip/local).    |
| `make_sip_call()`                    | Nur wenn `state_sip == STATE_REGISTER_SUCCESS`.                  |
| `make_local_call()`                  | Bus-/lokaler Call ohne SIP.                                       |
| `make_call_third_party_server()`     | Dritte SIP-Domain pro Taste (`callType==…`).                     |
| `check_third_party_server()`         | Pre-Check vor 3rd-party-SIP.                                     |
| `on_incoming_call()`                 | Eingehender SIP-Call von pjsua → Bus-State holen, ggf. Hangup.   |
| `on_third_sip_call_disconnected()`   | Pjsua-disconnect-Event, räumt Call-Map auf.                      |
| `cancel_unnecessary_calls()`         | Forking-Cleanup (mehrere Indoors klingeln → einer nimmt ab).     |
| `cancel_unavailable_calls()`         | Forking-Cleanup wenn Stationen nicht antworten.                  |
| `clear_calling_list()` / `remove_in_call_data()` | Map-Maintenance.                                     |

Im Binary belegte pjsua-State-Codes (H, vom IPC empfangen):

```
PJSUA_STATUS_CALLING    PJSUA_STATUS_EARLY        PJSUA_STATUS_CONNECTING
PJSUA_STATUS_CONFIRMED  PJSUA_STATUS_DISCONNECTED
IPC_SIP_RE_REGISTER     IPC_SIP_STATUS_REGISTER   IPC_SIP_ACCOUNT_ADD
IPC_SIP_ACCOUNT_DEL     IPC_SIP_UNREGISTER        IPC_SIP_MAKE_CALL
IPC_SIP_STATUS_CALL     IPC_SIP_STATUS_MESSAGE    IPC_SIP_DTMF_OPEN_DOOR
IPC_SIP_STATUS_MEDIA    IPC_SIP_STATUS_MWI
```

Sonderfall **DTMF-Türöffner**: `IPC_SIP_DTMF_OPEN_DOOR=%c` — beim Empfang eines DTMF während eines Bus-Calls wird das Relay direkt geöffnet. Magic-Tokens dafür im Binary: `&TCS_CONTROL_OPENDOOR1`, `&TCS_CONTROL_OPENDOOR2` (H — Vendor-Bus-Codes für die zwei Türen).

`userial_*` / `utcp_*` Symbole sind **nicht** im Binary — die UART-Bus-Logik selbst sitzt in `uart2d`; avlink redet nur AT+B-textbasiert mit ihm (siehe 4.1).

### 6.1 GPIOs (H, alle aus shell-Calls)

| GPIO        | Bedeutung                                | Set-Strings                                                    |
|-------------|------------------------------------------|----------------------------------------------------------------|
| `/sys/class/gpio/gpio7/value`  | LED-Status (read)            | `led_fd error!` — Read-only check.                              |
| `/sys/class/gpio/gpio8/value`  | Factory-Reset-Button-Read    | `resetfd=%d`, `cur_val: %d` (in `detect_factory_reset`).        |
| `/sys/class/gpio/gpio9/value`  | LED-Trouble / Bus-LED        | `echo 1 > … gpio9/value` & `echo 0 > … gpio9/value`.            |
| `/sys/class/gpio/gpio11/value` | LED                          | dito.                                                          |
| `/sys/class/gpio/gpio12/value` | LED                          | dito.                                                          |

`ctrl_sip_led_trouble=%d` Logging-String → eine LED visualisiert den SIP-Registrier-Status.

### 6.2 Factory-Reset (H)

GPIO 8 wird in `detect_factory_reset` gelesen; bei Trigger:

```bash
killall uart2d; killall lua; sleep 0.5
sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql
cp /customer/share/zoneinfo/Europe/Berlin /customer/localtime
cp /customer/backup/interfaces /customer/interfaces
sync; sync; sync; sync; sync; sync
reboot
```

D.h. Reset stellt DB aus SQL-Dump wieder her, **resettet Timezone hart auf Europe/Berlin**, restored Netzwerk-Config, rebootet.

---

## 7. Funktions-Index (rekonstruiert aus Log-Bannern)

> Alle Einträge **H**: Funktionsname taucht wörtlich in einem `prctl`-Call oder Logging-Statement auf — d.h. der Compiler hat die Funktion in `__func__` oder als `prctl(PR_SET_NAME)`-Arg eingebettet.

### avlink-master.c
| Funktion                        | Aufgabe                                                                |
|---------------------------------|-------------------------------------------------------------------------|
| `avlink_master_main`            | Entry. Fork von server + mqtt-client. SIGCHLD-Reaper.                  |
| `sig_handler`                   | SIGINT/TERM/CHLD/DEFAULT-Handler (Re-Fork toter Kinder).              |
| `check_mqtt_alive`              | Watchdog: stat() auf Heartbeat-File, kill+restart bei Stale.          |

### avlink-server.c
| Funktion                        | Aufgabe                                                                 |
|---------------------------------|-------------------------------------------------------------------------|
| `avlink_server_main`            | Entry. epoll + TCP-10086-Accept-Loop.                                  |
| `ctrl_led_trouble`              | LED-Status-Setter (GPIO via shell).                                     |
| `get_relay_state`               | Liest Relais-Status (bit-flag, returned `0x%x`).                       |
| `voip_echo_handler`             | Bus-Subcommand-Dispatch zu uart2d-Frames.                              |
| `make_sip_call` / `make_local_call` / `make_calls` | Call-Pfade.                                       |
| `make_sip_call_third_party` / `make_call_third_party_server` / `check_third_party_server` | 3rd-party SIP. |
| `call_btn_trigger`              | Hardware-Tasten-Event → Lua-Lookup → make_calls.                       |
| `update_mqtt_discovery_client_config` | Neue Cloud-Config übernehmen.                                    |
| `response_mqtt_message`         | Outbound-Helper (baut `{tag/action/response}`).                        |
| `on_receive_monitor`            | MQTT → Kamera-Switch / Stream-Start.                                   |
| `on_receive_ctrl_delay`         | MQTT → Relay-Open mit Dauer (`relay[%d] open, duration=%d`).            |
| `on_receive_query_relay_state`  | MQTT → STATE-Antwort `{"action":"STATE","event":{"relay":"%d%d"}}`.    |
| `on_receive_ctrl_relay_state`   | MQTT → CTRL-Apply.                                                     |
| `on_receive_device_update`      | MQTT → UPDATE-Trigger (Firmware/Config).                                |
| `response_device_update_state`  | Outbound-Status nach Update.                                            |
| `on_received_mqtt_message`      | Master-Dispatcher MQTT-RX.                                              |
| `detect_factory_reset`          | GPIO-8-Poller.                                                          |

### mqtt-client.c
| Funktion                              | Aufgabe                                                          |
|---------------------------------------|------------------------------------------------------------------|
| `mqtt_client_main`                    | Entry des forked Process (`prctl=mqtt-client`).                 |
| `mqtt_client_config`                  | Liest Cloud-Server/Username/Token; logs `server=%s, username=%s`. |
| `mqtt_client_connect`                 | tls_set + connect_async.                                         |
| `mqtt_client_run`                     | epoll-Loop (mosquitto-socket + IPC-socket).                      |
| `mqtt_client_ctl`                     | Steuer-Frames vom IPC-Socket.                                    |
| `mqtt_client_action_handler_filter`   | Filter+Route Frames vor Publish.                                 |
| `mqtt_client_echo_handler`            | Test/Echo-Pfad.                                                   |
| `write_timestamp_to_file`             | Schreibt Heartbeat-File.                                          |
| Callbacks: `_connect_/_message_/_subscribe_/_log_callback` | Mosquitto-Hooks.                            |

### pjsip-handler.c
| Funktion                          | Aufgabe                                                              |
|-----------------------------------|----------------------------------------------------------------------|
| `pjsip_tcp_client`                | TCP-Connect zu pjsua (localhost).                                     |
| `pjsip_tcp_action`                | Sendet IPC-Frames an pjsua.                                          |
| `pjsip_add_account_common` / `pjsip_add_account2` | SIP-Account anlegen.                                |
| `remove_in_call_data` / `clear_calling_list` | Call-Map-Maintenance.                                    |
| `cancel_unnecessary_calls` / `cancel_unavailable_calls` | Forking-Cleanup.                              |
| `on_third_sip_call_disconnected`  | 3rd-party-SIP-Disconnect.                                            |
| `check_outdoor_incoming_call`     | Bus-Türsprechstelle-Identifizierung (Regex `<sip:bus%d@…>`).         |
| `on_incoming_call`                | Eingehender SIP-Call → State-Update.                                  |
| `pjsip_resopnse_handler` *(sic)*  | Antwort-Parser. **Typo im Binary** ("resopnse" statt "response"). |
| `Get_bus_addr`                    | Extrahiert Bus-Adresse aus SIP-URI (Regex `<sip:bus%d@`).            |

### tcp-server.c
| Funktion                          | Aufgabe                                                              |
|-----------------------------------|----------------------------------------------------------------------|
| `tcp_server_ctl`                  | Hauptfunktion AT+B-Parser.                                           |
| `tcp_cmd_handler`                 | Regex-Dispatcher.                                                    |
| `notify_receive_mqtt_message`     | TCP→MQTT-Inject (`AT+B RELOAD mqtt`).                                |
| `notify_mqtt_client_start`        | Re-Start des MQTT-Client.                                            |
| `check_network_state`             | `AT+B check ip`.                                                     |
| `tcp_to_firmware_upgrade`         | Port-10010-Bridge.                                                   |

### usr-config.c
| Funktion                          | Aufgabe                                                              |
|-----------------------------------|----------------------------------------------------------------------|
| `usr_config_init`                 | `luaL_newstate + dofile(config.lua)`.                                |
| `usr_config_reload`               | Full-Reload.                                                          |
| `usr_config_reload_call_list`     | Nur Lua-`Get_callee`-Table neu laden.                                |

### usr-log.c (own internal logger module)
| Funktion (inferred)               | Aufgabe                                                              |
|-----------------------------------|----------------------------------------------------------------------|
| Logger: schreibt `/customer/share/usr-log.log` mit Rotation. Levels: `FATAL / ERROR / WARN / INFO / DEBUG / TRACE`. Sendet außerdem via Unix-DGRAM zu `/var/run/usr-log.socket` (Multicast an andere Konsumenten). | Pure Sink. |

---

## 8. Hidden Debug / Backdoors / Side-Channels

| Beleg                                          | Bedeutung                                                                 | C |
|------------------------------------------------|---------------------------------------------------------------------------|---|
| `Hello World.`                                 | Vermutlich SIGCHLD- oder Default-Signal-Branch ("dead code"); harmlos.    | H |
| `Usage: avlink [function [arguments]...]`      | CLI hilft mehrere Funktionen aufzurufen (busybox-Style applet).            | H |
| `Currently defined functions: avlink`          | Multiplexer-Mode existiert.                                               | H |
| `--help`                                       | `getopt`-Argument.                                                        | H |
| `version: 20240729; %s %s`                     | `__DATE__ __TIME__` im Build → "May 9 2025 05:12:51".                     | H |
| Watchdog: `/dev/watchdog`, `watchdog_get_timeout=%d`, `set after watchdog timeout=%d` | Master pflegt Hardware-WDT.        | H |
| `kill -- pids`                                  | Standard, kein hidden.                                                    | H |
| `getenv` → **nicht** in `nm -D`.                | **avlink liest KEINE Umgebungsvariablen.** Keine `DEBUG=1`-Backdoor.       | H |
| `/tmp/debug` etc. → **keine Treffer.**          | Keine Dotfile-Trigger.                                                    | H |
| `Hello World.` plus `1422933` Magic            | Vendor-Code / Test-Branch. Vermutlich ungenutzt im Produktiv-Pfad.        | L |
| `signal` ist gelinkt, aber nur SIGCHLD/INT/TERM behandelt | Keine SIGUSR1/USR2-Debug-Hooks.                                | M |
| `system()` calls (alle bekannt — siehe oben)   | Shell-Escape-Pfade nur für Factory-Reset, WiFi-Connect, GPIO, lua-restart. Kein Generic-Eval. | H |

**Keine** der typischen Backdoor-Signale gefunden: kein hardcoded Passwort, kein `telnetd`-Trigger, kein `nc -e`, kein `/bin/sh`-Spawn auf TCP-Port. Die TCP-10086-AT-Schnittstelle ist **selbst die Backdoor** (offen `0.0.0.0`, ohne Authentifizierung) — siehe §10.

---

## 9. SIP-Account-Daten & 3rd-Party-Calls

`pjsip_add_account_common` baut SIP-URI `<sip:%s@%s>` (H) aus:
- `account` / `user_id` (config.lua: `sip_username`)
- `passwd` (config.lua: `sip_password`)
- `sip_server` (z.B. `de.ilifestyle-cloud.com`)
- `alias` (config.lua: `sip_nickname`)

Bus-internes Adress-Schema: `<sip:bus%d@…>` (H) — d.h. jede Bus-Teilnehmer-Adresse ist `bus<N>@<sip-server>`.

3rd-Party-Account-Add wird über `pjsip_add_account2` mit Daten aus Lua-`Get_callee()` aufgerufen (`server`, `account`, `password`, `callee`).

---

## 10. Sicherheits-Schnellanalyse

| Aspekt                                       | Befund                                                                                                                                                              | C |
|----------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|---|
| TCP `0.0.0.0:10086` unauthenticated         | **AT+B-Befehle aus dem LAN ohne jegliche Auth/PSK/Token-Check.** Alle Bus-Aktionen (Türöffner via `AT+B UART unlock`) sind durch ein einfaches TCP-Connect aus dem LAN auslösbar. | H |
| `AT+B UPGRADE %d`                            | Stage-2: ruft `lua /customer/share/firmware_upgrade.lua` — der Lua-Code entscheidet, ob FW akzeptiert wird. Wenn dort kein Signing-Check, ist FW-Path angreifbar (siehe `cloud_sync.md`). | M |
| Factory-Reset GPIO                           | Physisch erreichbar.                                                                                                                                                | H |
| MQTT TLS                                     | CA-bundle-validiert, **kein Cert-Pinning**, keine Client-Cert. Hostname-Verify aktiv (libmosquitto-default).                                                          | H |
| MQTT auf 1883 (plaintext) möglich            | `mqtt port=1883 socket=%d` ist statisch — Code-Pfad besteht. Wenn `mqtt_server` (config.lua) auf Port 1883 zeigt, kein TLS.                                          | M |
| MQTT-Credentials                             | `token` aus config.lua, plain in SQLite (`/customer/share/avl20.db`). Bei FS-Zugriff auslesbar.                                                                       | H |
| Lua-Script-Injection                         | `Get_callee` ruft Lua-Code aus DB → wenn DB von Cloud beschreibbar, Code-Injection-Möglichkeit.                                                                       | M |
| `system()` für `wpa_supplicant`              | `AT+B WIFI connect <ssid>` direkt in shell. **Wenn `<ssid>` nicht escaped wird** → Shell-Injection via LAN-TCP.                                                       | M |

---

## 11. Zusammenfassung & offene Fragen

**Was wir jetzt sicher wissen (H):**

* `avlink` ist 3 Prozesse: Master + AT-Server + Mqtt-Client.
* AT+B-Channel ist Plain-TCP `0.0.0.0:10086`, Regex-Parser, ohne Auth.
* Genau **13 Subcommand-Familien** existieren (siehe §4-Tabelle).
* MQTT-Cloud: libmosquitto, TLS 1.2, CA-Bundle, **kein Pinning**.
* Lua ist **Konfigurations-Träger** (`config.lua`) und **Routing-Logik** (`Get_callee`).
* Bus-State-Machine hat 6 dokumentierbare States + 14 SIP-IPC-Events.
* Factory-Reset wischt DB+TZ+Network-Interfaces.

**Was unklar bleibt (Disassembly nötig):**

* Exaktes Topic-Format (Präfix? `cloud.Account/<mac>/...` ist plausibel).
* `mqtt_client_action_handler_filter`-Frame-Format auf `/customer/share/mqtt-client.socket` (Struct-Layout).
* Ob beim Port-1883-Codepfad `tls_set` übersprungen wird.
* `1422933`-Magic-Bedeutung.
* Genauer Inhalt von `mosquitto_tls_opts_set` (cert_reqs, tls_version, ciphers).
* Inhalt der `AT+B KEY`-Variante.
* Ob `AT+B WIFI connect …` SSID escaping macht (`system()` ist sicher gelinkt; Schreibweise via `system()` aus Format-Builder wäre injection-anfällig).

**Empfehlung für Folgearbeit:** Ghidra/r2 mit ARM-Loader + jansson/mosquitto/lua FLIRT-Signatures laufen lassen — alle in §7 genannten Funktionen sind bereits durch ihre Logging-Strings auffindbar (xref auf Format-String → Funktion). Damit liesse sich der genaue Mqtt-IPC-Frame-Layout (M) und die Auth-Logik im AT+B-Channel (falls existent, hier nicht belegt) verifizieren.
