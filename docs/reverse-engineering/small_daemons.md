# Villa GW V3.0 — Kleine Daemons (Reverse Engineering)

Dieser Bericht ergänzt das große Reverse-Engineering um die vier "kleinen" Helfer-Daemons,
die neben `avlink` (Hauptdaemon, AT+B-Bus auf TCP 10086) und `uart2d` (Bus-UART-Bridge) auf
dem Villa GW laufen.

| Daemon | Sprache | Listen | Zweck (Kurzfassung) |
|---|---|---|---|
| `discovery` | C (ARM ELF) | UDP mcast `239.255.255.240:6210` + Unix `/var/run/discovery.socket` | Vendor-LAN-Discovery ("Search-Response") für Companion-App / Cloud-Provisioning |
| `custode2.lua` | Lua | TCP `127.0.0.1:60000` | "Wächter" — WiFi-/Ethernet-State-Machine, dreht LEDs, koordiniert `mimedia`/`uart2d`/P2P |
| `monitor.lua` | Lua | (kein Socket — Watchdog) | Log-Rotation für Nginx + (alter, auskommentierter) voip-server-Memory-Watchdog |
| `firmware_upgrade.lua` | Lua | TCP `0.0.0.0:10010` | Cloud-OTA-Update + Missed-Call-Image-Upload an PBX |

Quelle: `villa_gw_dump/etc/init.d/{discovery,monitor,rcS}`,
`villa_gw_dump/customer/app/sbin/{discovery,custode2.lua}`,
`villa_gw_dump/usr/sbin/monitor.lua`,
`villa_gw_dump/customer/share/firmware_upgrade.lua`,
`strings villa_gw_dump/customer/app/sbin/{discovery,avlink}`.

---

## 1. `discovery` (Binary)

**Datei:** `/usr/sbin/discovery` (in unserem Dump unter `customer/app/sbin/discovery` — auf
dem Gerät via `start-stop-daemon -b -S -x /usr/sbin/discovery`).

ARM 32-bit ELF, gegen glibc 2.7/2.4 gelinkt, gebaut mit GCC 9.1.0.
~26 KB, kein Symbolstrip — Funktionen sind zwar weg, aber `.rodata` ist hochaufschlussreich.

### Sockets

`strings` liefert nur eine Handvoll relevanter Konstanten:

```
239.255.255.240        ← Multicast-Group
6210                   ← UDP-Port (Listen + Send)
/var/run/discovery.socket   ← lokales Unix-Domain-Socket
```

Plus die libc-Symbole: `socket`, `bind`, `setsockopt` (IP_ADD_MEMBERSHIP/IP_MULTICAST_IF
implizit über `interface setsockopt() sending interface`), `recvfrom`/`sendto`,
plus `accept`/`listen` (für das Unix-Socket).

**Architektur:**
1. UDP-Socket joined Multicast-Gruppe `239.255.255.240`, hört auf Port `6210`.
2. Unix-Stream-Socket `/var/run/discovery.socket` für lokale Clients (= `avlink`).
3. Single-threaded `epoll`-Loop.

### Wire-Protokoll (JSON über UDP)

Genau ein `printf`-Template im Binary:

```
{"command":"search","type":"response","request_id":"%s","data":%s}
```

Reverse: ein Companion-Client schickt an `239.255.255.240:6210` (oder Unicast, das Socket
empfängt beides) ein JSON wie

```json
{"command":"search","type":"request","request_id":"<uuid>"}
```

…und `discovery` antwortet per `sendto` mit der `response`-Variante. Im Binary:

```
search
request_id
request_id=%s.
configure_server fail
```

`data` wird via `/var/run/discovery.socket` von `avlink` befüllt. In `avlink` (siehe
`strings avlink`):

```
discovery.c
/var/run/discovery.socket
update_mqtt_discovery_client_config
discovery_ctl
discovery_config
{"name":"%s","id":"%s","ip":"%s","mac":"%s","version":"%s","hardware":"%s","state":"%s","config":"%s"}
```

Das ist der **`data`-Body** der Discovery-Response. `avlink` öffnet das Unix-Socket
("discovery_ctl"/"discovery_config"), pusht das aktuelle Device-Info-JSON; `discovery`
cached es und stempelt es bei jedem `search`-Request mit der zugehörigen `request_id`
in das Response-Template.

### Zweck

Reines **Vendor-LAN-Discovery** (kein mDNS, kein SSDP, kein Bonjour). Die Hersteller-App
("Systec" — Marker im Binary: `"Systec"`) scannt das LAN über die Multicast-Gruppe, das
GW antwortet mit MAC/IP/Version/State → Onboarding ohne Cloud.

**Konsequenzen für HA:**
- Wir können das selbst aufrufen und z.B. eine `homeassistant.config_flow` Discovery
  bauen. Request: `{"command":"search","type":"request","request_id":"<uuid>"}` als UDP
  an `239.255.255.240:6210` (oder direkt Unicast an das GW). Response enthält
  `name/id/ip/mac/version/hardware/state/config` → genau das, was wir für die
  HA-Integration brauchen.
- **Wichtig:** das ist nur Discovery — kein Event-Stream. Kein Klingel-Push hier drin.

### Auffälligkeiten

- "configure_server fail" + `__isoc99_sscanf` + die Tatsache, dass das Binary
  `htonl`/`ntohs` benutzt: discovery parsed *eingehende* JSON-Felder (mindestens
  `command`, `type`, `request_id`) selbst (kein json-lib gelinkt, alles handgestrickt
  per `sscanf`/`strncmp`).
- Buffer-Größe-Strings (`tried to set socket receive buffer from %d to %d, got %d`)
  → benutzt `SO_RCVBUF`-Tuning.
- Keine Authentifizierung, kein Token. Discovery-Response ist plain JSON über Multicast
  — bekannt aus jedem Lieferketten-IoT-Gerät.

---

## 2. `custode2.lua` (TCP 60000 — "Wächter")

**Datei:** `/customer/app/sbin/custode2.lua`, 1264 Zeilen Lua, läuft via Lua-5.x mit
`llinux`, `lepoll`, `bit`, `luasocket`, `lsqlite3`, `socket.http`. Wird **nicht** von
init.d gestartet — vermutlich aus `/customer/demo.sh` o.ä. (in unserem Dump fehlt
`rc.local`, aber `rcS` ruft `exec /etc/init.d/rc.local` auf).

### Was der "Custode" tut

Trotz des trockenen Namens ist das das **Netzwerk-Brain** des GW:

1. Liest beim Start `wifi`-Config aus `/customer/share/avl20.db` (`config`-Tabelle).
2. Entscheidet Betriebsmodus:
   - **eth0**-Kabel da → LAN-Modus
   - **wlan_sta** (Client) → mit `wpa_supplicant`/`wpa_cli` verbinden
   - **wlan_ap** (Soft-AP, SSID-Prefix `avlink_<mac-suffix>`) für Erst-Einrichtung
3. Lädt MT7601-WiFi-USB-Treiber-Module je nach Modus (`/customer/wifi/load_sta.sh` bzw.
   `load_ap.sh`), startet/stoppt `udhcpc`/`udhcpd`.
4. Steuert die **WiFi-LED** an GPIO 6:
   - `on` = connected, `off` = idle, `blink` = associating.
5. Startet/restartet alle Sub-Daemons sobald das Netz steht:
   - `mimedia master &` (P2P-Media-Server-Master)
   - `uart2d uart2d &` (Bus-Gateway)
   - `media-server` (P2P, je nach `video.transfer == 2`-Config)
6. Hat **Watchdogs**:
   - `wlan0_watch_dog` — alle 20 s arping zum Gateway; ab 5/8 min Fehler `reassociate`,
     nach 10 min **`reboot`**. (Aggressiv!)
   - `p2p_watch_dog` — liest `/var/run/gst-p2p.heartbeat`, kein Heartbeat in 30 s × 5 →
     `media-server` neustarten.
7. NTP-Setup über `pool.ntp.org` + Zeitzonen-Sync via `/customer/share/zoneinfo/`.
8. Set Hardware-MAC: liest `/customer/mac`, schreibt mit `ifconfig eth0 hw ether <mac>`,
   beim Erststart wird MAC aus `ra0`/`wlan0` HWaddr exportiert und reboot ausgelöst.

### Der TCP-Port 60000

```lua
sfd = linux.tcpserver("127.0.0.1", "60000", 5);
```

Nur **localhost**, akzeptiert AT-ähnliche Kommandos (Format: `AT+B RELOAD <thing>\r\n`).
`R.client` ist im Code-Kommentar (Z. 20) als `--向外通知的fd,长连接` markiert
("der fd nach außen für Benachrichtigungen, Long Connection"). Wird intern für die
Eintreiber-Kommunikation genutzt.

**Empfangene Kommandos (Z. 586–631):**

| Cmd | Wirkung |
|---|---|
| `AT+B RELOAD wifi\r\n` | killall `uard2d`/`mimedia`, lese WiFi-Config neu, `check_mode_2()` |
| `AT+B RELOAD himedia\r\n` | `media_reload()` — `mimedia`+P2P komplett neustarten |
| `AT+B RELOAD address\r\n` | `autoSync.lua` triggern + `uart2d` neustarten (sip-Adressbuch / Tasten-Mapping) |
| `AT+B RELOAD NTP\r\n` | `set_ntp()` aus DB |
| `AT+B RELOAD icloud\r\n` | P2P-Subsystem reload |
| `AT+B RELOAD eth0 start\r\n` | `network_down('wlan0')`, `R.flag1=1` |
| `AT+B RELOAD eth0 stop\r\n` | `/etc/init.d/networking restart`, WiFi-LED on |
| `AT+B check ip\r\n` | Schickt aktuellen IP-Status an TCP 10086 (avlink) |

### Was custode2 nach außen schickt

Custode2 ist auch **Client** auf `127.0.0.1:10086` (= avlink AT+B-Bus). Es sendet z.B.
`AT+B WIFI connect <ip>` / `AT+B WIFI disconnect`, sobald der Link kommt/geht. Diese
Nachrichten kennen wir bereits aus dem AT+B-Reverse — sie sind die `WIFI`-Status-Events,
die im großen Daemon-Doku auftauchen.

### Konsequenzen für HA

- **Kein Bus-Tap.** Custode2 macht nur Netzwerk-/Daemon-Lifecycle, keine
  Bus/Klingel-Events.
- TCP 60000 ist nützlich als **Remote-Reload-Hebel**: wir können ohne SSH per
  `nc 127.0.0.1 60000 <<< 'AT+B RELOAD wifi'` einen kompletten Re-Sync der Netzwerk-
  Stacks erzwingen. Für unsere Integration aber irrelevant außer als Debug-Werkzeug.
- Wenn die HA-Integration mal "alles ist tot" sieht (Bus-Stream weg, mimedia weg),
  könnte ein `AT+B RELOAD himedia` der billigste Recovery-Hebel sein. Nur localhost
  → braucht SSH-Tunnel.

---

## 3. `monitor.lua` + `/etc/init.d/monitor` — kein Bus-Monitor

**Wichtig: Trotz des verheißungsvollen Namens hat das NICHTS mit dem Bus zu tun.**

`/etc/init.d/monitor` (start-Block):

```sh
uptime_damemon 15.0 60 &           # externer Binary — nicht in unserem Dump
monitor.lua 0>/dev/null 1>/dev/null 2>&1 &
```

- `uptime_damemon` (sic — Typo, ist nicht `daemon`): externer System-Watchdog, läuft
  parallel. Binary fehlt im Dump → wahrscheinlich Standard-OEM-Reboot-Watchdog
  (Argumente: `15.0` = Interval-Sekunden, `60` = max idle?).
- `monitor.lua` lebt in `/usr/sbin/`. Quellcode (176 Zeilen) macht **nur zwei Sachen**:

### Aufgabe 1: Nginx-Log-Rotation

```lua
logpaths = {
    "/usr/local/nginx/logs/access.log",
    "/usr/local/nginx/logs/error.log",
}
```

Alle 10 Sekunden: wenn `access.log` oder `error.log` > 512 KB → `mv → .old`, dann
`kill -USR1 $(cat nginx.pid)` (nginx reopen-logs Signal).

Eigenes Log unter `/usr/share/monitor.log` (rotiert bei 256 KB nach `.old`).

### Aufgabe 2: voip-server-Memory-Watchdog (DEAKTIVIERT)

```lua
--if not voip_monitor() then
--    R.restart_count = R.restart_count + 1
--    os.execute("/etc/init.d/voip-server stop;sleep 2")
--    os.execute("/etc/init.d/css start")
--    os.execute("/etc/init.d/voip-server start")
--    R.log("voip-server restart ok", R.restart_count)
--end
```

Komplett auskommentiert. Die `voip_monitor()`-Funktion existiert noch, liest
`/var/run/voip-server.pid`, prüft VSS gegen MemTotal — würde bei > 3.9× RAM-Verhältnis
neu starten. Tot.

`stdin_handler` ist als `coroutine.wrap` registriert, aber nicht definiert (Z. 151
verweist auf `stdin_handler`, aber nur `std_in_handler` ist deklariert) — sieht aus
wie unausgereifter Code, läuft in der Praxis im else-Zweig.

### Konsequenzen für HA

- **Kein Goldfund.** Kein Bus-Mirror, kein Event-Subscribe, kein Klingel-Tap.
- Nützliche Nebenerkenntnis: wir können `/usr/share/monitor.log` und
  `/usr/local/nginx/logs/{access,error}.log` per SSH lesen → Debug-Quelle für
  Web-UI-Fehler.

**Fazit:** Der Name "monitor" ist irreführend — gemeint ist "system-health monitor"
(RAM/Logs), nicht "bus monitor".

---

## 4. `firmware_upgrade.lua` (TCP 10010)

**Datei:** `/customer/share/firmware_upgrade.lua`, 597 Zeilen Lua.
Gestartet von `avlink` selbst (`strings avlink` zeigt `lua /customer/share/firmware_upgrade.lua &`).

### Listen

```lua
local sfd = linux.tcpserver("0.0.0.0", "10010", 5)
R.log('tcpserver 127.0.0.1:10010')   -- Log-Text ist falsch!
```

**SICHERHEITSPROBLEM 1:** Das Log behauptet `127.0.0.1:10010`, aber tatsächlich wird
auf **`0.0.0.0:10010`** gebunden — also **alle Interfaces, auch das LAN**. Sehe ich auch
in unserem Bus-Notes nirgends erwähnt.

### Protokoll

`AT+B`-Kommandos via TCP, gleiches Format wie der avlink-Hauptbus. Nur zwei Befehle:

| Cmd | Wirkung |
|---|---|
| `AT+B UPGRADE 1\r\n` | Refresh-Config aus DB + Version an PBX posten |
| `AT+B UPGRADE 2\r\n` | Nur Version an PBX posten (deprecated) |
| `AT+B UPGRADE 3\r\n` | **Firmware download + install** |
| `AT+B RECORD <contact>\r\n` | Missed-Call-Snapshot per Multipart POST an PBX hochladen |

### Upgrade-Flow (AT+B UPGRADE 3)

1. DB `/customer/share/avl20.db` lesen: `cloud_account` (server+token),
   `device_update` (update_server), `sip` (nickname, sip_id, contact).
2. MAC von `ifconfig` als `device_id`; Version aus `/etc/VERSION`.
3. `PUT http://<server>/api/device/<id>/info`
   Body: `{"path":[],"data":{"hardware":"2.0","firmware":"<ver>"},"mode":2}`
   Auth: `Authorization: <token>` (JWT, kein Bearer-Prefix).
4. `GET http://<update_server>/download_path?product_name=AVL20P`
   Antwort: `{"code":0,"download_path":"http://c1.systec-pbx.net/software/update.tar.bz2"}`
5. `http.request` → `/tmp/update.tar.bz2`.
6. `cd /tmp/ && tar -xjf update.tar.bz2 && cd update && sh update.sh`
7. Notification an avlink (`AT+B UPGRADE 4` = upgrading, `5` = failed) via
   TCP `127.0.0.1:10086`.

### Sicherheitsbedenken (zusammengefasst)

- **`0.0.0.0:10010`, kein Auth.** Jeder im LAN kann `nc <gw-ip> 10010 <<<
  'AT+B UPGRADE 3\r\n'` schicken → das GW lädt die nächste Update-Tarball von
  Cloud + führt `sh update.sh` als root aus.
- **HTTP statt HTTPS** für sowohl Update-URL als auch Version-Post → MITM trivial.
- **`update.tar.bz2` wird ohne Signaturprüfung** entpackt und `update.sh` ausgeführt →
  wenn jemand DNS für `c1.systec-pbx.net` umlenkt, hat er Root.
- **`Parm.debug = false`** im Code, aber wenn `true`, sind hardcoded JWT-Token und
  Test-`device_id` aktiv (Z. 193–198) — die Token sind keine echten Live-Tokens
  (vermutlich abgelaufen), bestätigt aber den Auth-Mechanismus.
- **Hardcoded URL-Templates** lassen erkennen: die Vendor-Cloud ist
  `*.systec-pbx.net` (`tj.systec-pbx.net` Account-Server, `c1.systec-pbx.net`
  Update-Server) — siehe auch `api-cloud.md`.

**Empfehlung für unsere HA-Integration:** Port 10010 zu **blockieren** (lokale
Firewall am Router, port-block für die GW-IP) ist der saubere Härtungs-Schritt
für jeden, der das GW in einem produktiven Netz hat. Wir können es im README
erwähnen.

### "RECORD"-Pfad (interessant!)

```
AT+B RECORD <contact>\r\n
```

Kopiert `/customer/config/snap.jpg` → `/customer/config/copy.jpg`, baut Multipart-Body
mit Feldern `device_name`, `from` (sip_id), `to` (contact), `state=1`, `duration=0`,
plus dem JPEG, POSTet an `http://<server>/api/call/record`.

**Bedeutet:** Bei verpasstem Anruf macht das GW einen Türstand-Snapshot (von wo?
vermutlich vom Stream-Snap-Endpoint des avlink), packt ihn als Form-Multipart und
schickt ihn an die PBX-Cloud. Das Snap-File `/customer/config/snap.jpg` ist wahrscheinlich
genau der **Klingel-Trigger-Snapshot**, den wir uns auch über lokales `valve.gartenbewasserung`-
äquivalentes Endpoint ziehen könnten — auch wenn der hier nur in der Cloud-Variante
benutzt wird.

→ **Action-Item:** Prüfen ob `/customer/config/snap.jpg` über die REST-API erreichbar
ist oder ob avlink ein lokales Snap-Endpoint bietet. (Siehe `api-rest.md`.)

---

## 5. Init-Reihenfolge

Aus `villa_gw_dump/etc/init.d/rcS`:

```sh
exec /etc/init.d/rc.local
```

`rc.local` ist **nicht** in unserem Dump. Die `init.d/{discovery,monitor,nginx}`-Scripts
sind die kanonischen Sysvinit-Wrapper. Da kein `rcN.d/`-Verzeichnis existiert, werden
sie wahrscheinlich aus `rc.local` (auf Gerät) direkt mit `start` aufgerufen.

Beobachtete Reihenfolge (rekonstruiert):

1. `rcS` mountet alles, startet `telnetd`, `sshd`, bringt eth0 hoch.
2. `rc.local` (fehlt im Dump, würde diese Reihenfolge ausführen):
   - `/etc/init.d/nginx start`
   - `/etc/init.d/discovery start` (`/usr/sbin/discovery`)
   - `/etc/init.d/monitor start` (`uptime_damemon` + `monitor.lua`)
3. Aus `/customer/demo.sh` (existiert auf Gerät, fehlt im Dump): startet `custode2.lua`
   im Hintergrund. `custode2` startet dann `avlink`/`mimedia`/`uart2d`/`pjsua`, sobald
   das Netz steht.
4. `avlink` startet via `lua /customer/share/firmware_upgrade.lua &` den OTA-Daemon und
   meldet sich beim `discovery`-Daemon über `/var/run/discovery.socket` an.

Schematische Abhängigkeiten:

```
rc.local
 ├── nginx           (HTTP-Frontend, /usr/local/nginx)
 ├── discovery       (UDP 6210 + /var/run/discovery.socket)
 └── monitor         (uptime_damemon + monitor.lua)
demo.sh
 └── custode2.lua    (TCP 60000, LED, network state-machine)
       └── (when network up) avlink           (TCP 10086 AT+B bus)
              ├── uart2d                       (bus-uart bridge → tasten/klingel)
              ├── mimedia/pjsua/media-server   (SIP + RTP/P2P)
              └── firmware_upgrade.lua &       (TCP 10010 OTA)
```

---

## 6. Versteckte Hooks für HA-Integration

| Feature | Wert für uns | Notiz |
|---|---|---|
| `discovery` UDP 6210 Search-Response | **Hoch** | Auto-Discovery für `config_flow`, liefert IP/MAC/Version/State. Würde "HA findet das GW automatisch im LAN" möglich machen — analog zu wie HA z.B. Sonos findet. |
| `discovery` `data`-JSON Format | Mittel | `{name,id,ip,mac,version,hardware,state,config}` — `state` und `config` Felder noch nicht dekodiert. `config` ist vermutlich ein String mit dem Onboarding-Status (paired/unpaired). |
| `custode2` TCP 60000 `RELOAD` Hebel | Niedrig | Nur lokal nutzbar (loopback), kein Bus. Hilft für "alles neustarten ohne reboot" wenn HA tunnel-SSH hat. |
| `firmware_upgrade.lua` TCP 10010 | **Negativ** (Security) | Im README als "blockieren" empfehlen. Außerdem: `AT+B RECORD` zeigt, dass ein Snapshot-File `/customer/config/snap.jpg` bei Klingeln existiert — interessant für Klingel-Bild-Sensor. |
| `monitor.lua` | Niedrig | Nur Log-Rotation, kein Bus-Tap. |
| `nginx.access.log`/`error.log` | Mittel | Debug-Quelle für REST-API-Calls (Path/Status der WebUI-Endpoints). |
| `/usr/share/monitor.log` | Niedrig | System-Watchdog-Log, hilft bei Reboot-Forensik. |

### Empfohlene nächste Schritte

1. **Discovery testen:** Mit `socat` / Python ein
   `{"command":"search","type":"request","request_id":"x"}` an
   `239.255.255.240:6210` (oder direkt Unicast an GW) senden und Response captureen.
   Vergleichen mit unserer aktuellen mDNS-Annahme im `config_flow.py`.
2. **`avl20.db.dump.sql` querschecken:** Das `state`-Feld im Discovery-Body
   wird vermutlich dynamisch aus der DB gezogen. Nicht in den kleinen Lua-Files,
   sondern in `avlink` selbst.
3. **Snap-File:** Beim Test-Anruf prüfen, ob `/customer/config/snap.jpg` aktualisiert
   wird → potenziell neuer Image-Sensor für die HA-Integration.
4. **Port 10010 dokumentieren:** In `docs/security.md` Härtungs-Hinweis ergänzen
   ("im LAN blockieren — root-Code-Execution ohne Auth möglich").

### Was NICHT in diesen Daemons steckt

- ❌ Bus-Mirror / Event-Subscribe für Klingel-Events (das macht weiterhin nur
  `uart2d` ↔ `avlink` AT+B-Bus auf TCP 10086 — siehe Hauptdoku).
- ❌ MQTT-Client-Innereien (in `avlink` selbst, `mqtt-client.c`).
- ❌ SIP-Logik (in `pjsua`/`mimedia`).
- ❌ Persistenz von Tastenzuweisung (das ist `address.lua` + DB).

Die "kleinen" Daemons sind tatsächlich klein — sie bringen Netz + Discovery +
Auto-Update hoch, mehr nicht. Der spannende Bus-Traffic bleibt das, was wir
bereits via `avlink:10086` analysiert haben.
