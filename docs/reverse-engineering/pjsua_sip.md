# Villa GW V3.0 — `pjsua` und SIP-Stack (Reverse Engineering)

Tiefenanalyse des `pjsua`-Binaries, des umgebenden SIP-Codes in `avlink`, der Lua-Handler
und der Datenbankschicht.

| Item | Wert |
|---|---|
| Binary | `/customer/app/sbin/pjsua` (ARM 32-bit ELF, stripped, ~900 KB) |
| Build-Pfad | `/root/workspace/evm/systec/apps/pjsua/VoipPhone/` |
| Compiler | GCC + Boost 1.68 + statisch gelinkte PJSIP + OpenSSL |
| User-Agent | `VCP01` |
| Hersteller-Tag | `Systec outdoor and management central` |
| Quellen | `pjsua_app.cpp`, `MessageQueue.cpp`, `ControlVideoRtp.cpp`, `main.cpp` |
| Launcher | `avlink` (via `system("killall pjsua; pjsua &")`) — **kein Init-Script!** |
| IPC zu avlink | `/var/run/msg.socket` (Unix-Stream, Custom-MessageQueue) |
| Listen-Ports | UDP 5060 (SIP plain), TCP 5060 (SIP-TCP), TCP 5061 (SIP-TLS), UDP **33333** (RTP base — pjsua-Default `--rtp-port` ist `4000`, hier aber via Hersteller-Patch auf 33333 gesetzt) |
| DB | `/customer/share/avl20.db` Tabellen `sipServer`, `callList`, `config` (Key `sip`) |

---

## 1. Multi-Account-Support

### Beobachtung

Avlink-Strings beweisen **echte Multi-Account-Implementierung**, NICHT nur ein zweiter
Account-Slot. Pattern (mehrfach):

```
account=%d, user_id=%s, passwd=%s, sip_server=%s, alias=%s.
IPC_SIP_ACCOUNT_ADD acc_id=%d, flag=%d, acc_addr=%s
IPC_SIP_ACCOUNT_DEL accId=%d, status=%d
on_third_sip_call_disconnected acc_id=%d
on_third_sip_call_disconnected state=%d, count_call=%d
make_sip_call_third_party
make_call_third_party_server
check_third_party_server
third_party callee empty!
third_sip data empty.
pjsip_add_account2 failed.
```

Drei semantisch verschiedene Account-Klassen:

| Account | Zweck | Quelle |
|---|---|---|
| **Account 0 — "Cloud"** | iLifestyle: `s00c0000DEVICE_ID @ de.ilifestyle-cloud.com;transport=tls` | `config` Tabelle Key `sip` |
| **Account 1+ — "Third-Party"** | Benutzerdefiniert pro `callList`-Eintrag mit `callType=2` | `sipServer` Tabelle, joinable über `callList.serverId` |
| **Lokaler Server-Slot** | "simple registrar" — Symbol `pjsua simple registrar` (PJSIP-Sample, vermutlich nicht aktiv) | Binary-String |

`IPC_SIP_ACCOUNT_ADD` ist also nicht auf `acc_id=1` beschränkt — `avlink` ruft
`pjsip_add_account_common` für jeden neuen `sipServer`-Datensatz auf, der via Lua
(`addCallList.lua`/`updateCallList.lua` mit `callType == 2`) angelegt wurde.

### Praktische Konsequenz

Ein **zweiter Account ist machbar** — aber nur als _outbound third-party callee_,
**nicht als gleichberechtigte Identität, bei der HA als zweiter Registrar agiert**.
`avlink` registriert pro `sipServer.server` einzeln; jeder Eintrag wird beim Start
und nach `AT+B RELOAD callList` neu aufgesetzt.

**Limit:** Boundary-Check `pjsua_var.acc_cnt < sizeof(pjsua_var.acc) / sizeof(pjsua_var.acc[0])`
im pjsua-Binary — das ist `PJSUA_MAX_ACC` (PJSIP-Default 8). `PJSUA_MAX_CALLS=4` (binary
zeigt `--max-calls=N (default:4, max:255)`).

### Wie ein zweiter Account angelegt wird

Per REST/Lua (`/api/addCallList`):

```json
POST /api/addCallList
{
  "callNo": "100",         // sichtbare Anruf-Nr im UI
  "name": "HomeAssistant",
  "address": "",
  "key": "ha-bridge",      // muss UNIQUE sein (callList.key UNIQUE constraint)
  "userType": 0,           // 0=normal, 1=Außenstation, 2=Wachstation
  "callType": 2,           // ← 2 = SIP (vs. 0=Bus, 1=IP)
  "ipAddr": "",
  "server": "203.0.113.11", // FreePBX/Asterisk/HA-Add-on Adresse
  "account": "villa-gw",
  "password": "secret",
  "callee": "homeassistant",  // Ziel-User auf dem Server
  "enable": 1,
  "shareCode": ""
}
```

Dann sendet `addCallList.lua` `AT+B RELOAD callList\r\n` an avlink:10086,
avlink iteriert `callList LEFT JOIN sipServer`, ruft `pjsip_add_account2` pro
`callType==2`-Eintrag auf.

---

## 2. Call-Routing & `callList.callType`-Werte

Aus der Schema-Kommentar-Zeile direkt im SQL-Dump (`avl20.schema.sql`):

```sql
callType INTEGER,      --网络地址类型 0: bus, 1: ip , 2: sip
```

| Wert | Bedeutung | Pfad |
|---|---|---|
| `0` | **Bus** (Villa-Bus, klassische Außenstation) | avlink → uart2d:10087 → `/dev/ttyS1` → Bus-Frame |
| `1` | **IP-Direct** (vermutlich proprietäres TCP/UDP zu HHG-IP-Stations) | avlink → direkt zu `callList.ipAddr` (Hinweis: `ipAddr=%s,buf=%s,accout=%d` in pjsua-Strings — pjsua weiß *auch* von ipAddr → wird teilweise als SIP-URI ohne Registrar gebaut) |
| `2` | **SIP** mit Third-Party-Server | avlink → pjsua via Unix-Socket → `pjsip_add_account2` + `pjsua_call_make_call` |

Anderen Werten begegnen wir im Code nicht. Default in fresh-installed DB ist
`callType=NULL` für den initialen Eintrag — das wird im Lua-Layer als "kein
Routing" behandelt.

**Routing-Entscheidung lebt vollständig in `avlink`** (`pjsip-handler.c`,
`make_sip_call`, `make_call_third_party_server`). pjsua entscheidet nichts
selbst — es bekommt fertige Calls-To-Make per IPC.

### Bus-zu-SIP-Bridging

Schlüssel-String:

```
<sip:bus%d@
```

Das ist `avlink`s URI-Format, wenn ein **Bus-eingehender Anruf** auf SIP gebridged
wird (Außenstation klingelt → SIP-Call zu Cloud-Account). Der Bus-Sender wird als
`bus<addr>@<sip-server>` enkodiert — der Cloud-Backend nutzt das, um zu wissen,
welche Außenstation läutet. Das ist der Mechanismus, der die iLifestyle-App auf dem
Handy klingeln lässt.

---

## 3. TCP 33333 — was ist das wirklich?

**Kurz: das ist kein TCP-Port, sondern UDP**, der lokale RTP-Base-Port von pjsua.

PJSIP-Default ist `--rtp-port=4000`. Die Strings im Binary enthalten:

```
rtp-port
--rtp-port=N  Base port to try for RTP (default=4000)
--rtp-port %d
```

Der HHG-Build initialisiert pjsua jedoch fest mit `--rtp-port=33333` (kein
String-Literal sichtbar, weil er als ganzzahliger argv-Wert in `main.cpp` hart
kodiert ist — vermutlich `pjsua_app.cpp`-Patch).

Was tatsächlich auf 33333 lauscht, wenn aktiv:
- **UDP 33333** = lokaler RTP-Audio-Port (G.711 µ-law primär — die DB-Config
  zeigt `"audio_format":"G.711"`)
- **UDP 33334** = RTCP-Paar (PJSIP-Konvention RTP+1=RTCP)
- Bei aktivem Video-Call zusätzlich `33335/33336` etc.

Diese Ports sind nur **bei aktiver Session belegt**. Im Idle-State sind sie
geschlossen — das erklärt, warum `nmap` sie sporadisch sieht (während des
Probe-Zeitpunkts klingelte vermutlich gerade etwas) oder nicht.

→ **In der bestehenden `architecture.md` ist "33333 pjsua SIP RTP relay" leicht
irreführend** — kein "Relay", sondern lokaler RTP-Endpoint. Außerdem UDP, nicht
TCP. Es lohnt sich, das dort zu korrigieren.

---

## 4. Inbound INVITE — kann das GW als Registrar fungieren?

### Hinweise im Binary

Ja, der **PJSIP simple-registrar-Sample-Code ist enthalten**:

```
pjsua simple registrar
mod-default-handler
sip:%s         ← URI-Format der Registrar-Antwort
```

Aber: der Lua-/avlink-Code **aktiviert dieses Modul nie**. Der Code-Pfad existiert,
ist aber tot — keine Möglichkeit, ihn per Config zu enablen.

### Wie GW-Inbound real funktioniert

Eingehende INVITEs werden vom registrierten **Cloud-Account** empfangen
(Server pusht via persistentem TLS-Socket). Avlink filtert per:

```
on_incoming_call state=%d, callID=%d, local_addr=%s, remote_addr=%s
on_incoming_call hangup call.
check_outdoor_incoming_call
```

`check_outdoor_incoming_call` validiert die **From-URI gegen `callList`** —
nur registrierte/whitelisted Callee-Adressen kommen durch. Alle anderen INVITEs
werden mit 4xx/BYE sofort beendet. Das macht das GW _als offenes Registrar_
nutzlos für Dritte.

### LAN-Direct INVITE (ohne Registrar)?

Pjsua hört zwar auf UDP 5060/TCP 5060. Ein direkter INVITE von außen klappt
**nicht praktisch**, weil:

1. `check_outdoor_incoming_call` verlangt, dass die From-URI in `callList`
   matcht.
2. Auto-Answer-Konfiguration ist im Binary parametrisiert (`--auto-answer=code`),
   aber der avlink-Pfad ruft pjsua mit fixen Args ohne `--auto-answer`.
3. Selbst wenn man durchkommt: das GW antwortet mit SDP, das auf die lokale
   Hisilicon-Kamera zeigt → wir kriegen Audio/Video, aber kein "Tür-Öffnen", weil
   wir nicht autorisiert sind. Dafür müsste eine DTMF-Sequenz akzeptiert werden.

**Trotzdem versuchen?** Ja:

```bash
printf 'OPTIONS sip:probe@<GW-IP> SIP/2.0\r\nVia: SIP/2.0/UDP probe;branch=z9hG4bK-test\r\nFrom: <sip:probe@probe>;tag=probe\r\nTo: <sip:probe@<GW-IP>>\r\nCall-ID: probe@probe\r\nCSeq: 1 OPTIONS\r\nMax-Forwards: 70\r\nContent-Length: 0\r\n\r\n' | nc -u <GW-IP> 5060
```

→ liefert 200 OK + `User-Agent: VCP01` zurück, ohne Auth. Damit kann man die
Liveness des Stacks sanity-checken.

---

## 5. TLS-Setup

### Cert-Store

```
/customer/share/ca-certificates.crt
```

Standard-CA-Bundle (Mozilla-Root-CAs, kein eigenes CA). Avlink referenziert
`SSL_CTX_load_verify_locations`. Es ist **kein Cert-Pinning** vorhanden — pjsua
verifiziert die Server-Cert-Chain gegen den System-CA-Bundle nur, falls
`--tls-verify-server` gesetzt ist.

### Verifikation aktiviert?

Pjsua-Binary kennt sowohl `--tls-verify-server` als auch `--tls-verify-client`,
aber wir können in den avlink-Strings _keine_ Stelle finden, an der pjsua mit
dieser Flag gestartet wird. Wahrscheinlich läuft TLS also **ohne Verifikation**
(`pjsua` ohne Flag = Default `verify_server=false`). Das passt zum
HHG-typischen "billige Cloud-Bequemlichkeit"-Pattern.

### TLS-Version

Binary listet sowohl `SSLv3_method`, `SSLv3_server_method` als auch `TLSv1_server_method`
auf. Avlink-String zeigt aber für MQTT:

```
tlsv1.2
Could not set TLS configuration: %d
```

— das ist Mosquitto-MQTT, nicht pjsua. Pjsua verwendet das, was OpenSSL als
Default aushandelt — die einkompilierte libssl ist von ~2018, also TLS 1.2.
TLS 1.3 ist **nicht** im Binary (kein `TLS_method`, kein `TLSv13`).

### Konsequenz für HA

- Wir können einen lokalen FreePBX/Asterisk mit selbstsigniertem TLS-Cert anbieten
  — das GW akzeptiert es vermutlich kommentarlos.
- Wenn wir TLS forcieren wollen: GW als Client zu HA verbinden — HA müsste als
  SIP-Server agieren (Asterisk-Add-on). MITM-resistent ist das nicht (kein
  Pinning).

---

## 6. IPC zwischen `avlink` und `pjsua`

### Transport

Unix-Stream-Socket: **`/var/run/msg.socket`** (in beiden Binaries referenziert).
Klassisches Server-Client-Pattern: avlink ist der Master, pjsua öffnet beim Start
das Socket und blockt auf `MessageQueue::recv()` (siehe Symbol `MessageQueue.cpp`).

### Framing

Custom binary-framed mit struct:

```c
struct msgCommand {
    int messageType;   // siehe Enum unten
    int callId;
    int accId;
    ... payload ...
};
```

Hinweise aus den Strings:

```
setMsgRecvHandleFunc msgCommand.messageType:   ← Setter für Handler-Callback
responseStatus  messageType=%d                 ← Antwort-Format
sipRes.messageType=%d
ipAddr=%s,buf=%s,accout=%d                     ← Payload-Layout für CallMake
```

### Message-Type-Enum (IPC_SIP_\*)

Alle aus `avlink`-Strings rekonstruiert. Numerierung **nicht im Binary sichtbar**
(kein numerisches Mapping), aber die Reihenfolge der `switch`-cases entspricht
typischer C-Enum-Konvention (0,1,2…).

| Konstante | Richtung | Funktion |
|---|---|---|
| `IPC_SIP_ACCOUNT_ADD` | avlink → pjsua | Account hinzufügen (`pjsua_acc_add`) — Payload: `acc_id`, `flag`, `acc_addr` |
| `IPC_SIP_ACCOUNT_DEL` | avlink → pjsua | Account entfernen, Status zurück |
| `IPC_SIP_RE_REGISTER` | avlink → pjsua | Force re-register (z.B. nach IP-Change) |
| `IPC_SIP_UNREGISTER` | avlink → pjsua | Account abmelden |
| `IPC_SIP_MAKE_CALL` | avlink → pjsua | Anruf starten — Payload enthält `ipAddr`, `buf` (URI), `accout` (Account-Idx) |
| `IPC_SIP_DTMF_OPEN_DOOR` | pjsua → avlink | Eingehendes DTMF-`'%c'` → avlink interpretiert als Tür-Öffnen-Trigger |
| `IPC_SIP_STATUS_REGISTER` | pjsua → avlink | Registration-State-Change (`STATE_REGISTER_SUCCESS` / `STATE_REGISTER_FAIL`) |
| `IPC_SIP_STATUS_CALL` | pjsua → avlink | Call-State-Change (`stateType=%d`, `acc_id`, `callID`, `remote_addr`) |
| `IPC_SIP_STATUS_MEDIA` | pjsua → avlink | Media-State (ACTIVE/INACTIVE) — auch Video-URL-Push |
| `IPC_SIP_STATUS_MESSAGE` | pjsua → avlink | SIP-MESSAGE (Text-IM) — vermutlich für In-Band-Kommandos |
| `IPC_SIP_STATUS_MWI` | pjsua → avlink | Message-Waiting-Indication — **wird explizit ignoriert** (`on IPC_SIP_STATUS_MWI ignore.`) |

### State-Machine (avlink-seitig)

```
STATE_REGISTER_FAIL → STATE_REGISTER_SUCCESS → STATE_TALKING → STATE_TALKING_BUS
```

Mit Timer:
- `STATE_TALKING_BUS timeout.` (Auto-Hangup, vermutlich nach `parameter.ringtime`)
- `STATE_REGISTER_SUCCESS timer_register ignore.` (debouncing)

### Konsequenz für HA

Direktes Sprechen mit `/var/run/msg.socket` **lohnt sich nicht** — das Framing
ist proprietär, kein offener Standard, kein Pakage. Stattdessen via
`AT+B RELOAD callList` einen `callType=2`-Eintrag erzeugen und pjsua wird gegen
unseren Server registrieren. Das ist offiziell unterstützt vom Lua-API
(`/api/addCallList`).

---

## 7. Lua-Integration

### `sip.lua`

REST-Handler für `/api/sip` (Read + Write).

**GET**: Liest `config` Tabelle Key `sip` aus SQLite, plus `online`-Flag von
`AT+B APPLICATION` (`{"state":1,"sip":1,"call":[0]}`). Antwortet mit
`{password, mqtt_server, nickname, server, name, contact, online}`.

**POST**: Schreibt JSON in `config` Tabelle, dann `AT+B RELOAD\r\n` an
avlink:10086 → avlink re-init des Cloud-Accounts via `IPC_SIP_RE_REGISTER`.

Dieser Endpoint kontrolliert **nur den Cloud-Account** (`name=s00c…`,
`server=de.ilifestyle-cloud.com`). Third-Party-SIP läuft separat über
`callList`-API.

### `getServerList.lua`

POST `/api/getServerList`. Read-only Query auf `sipServer` Tabelle:

```sql
SELECT id, server, account, password
FROM sipServer
GROUP BY server, account, password
ORDER BY id ASC
```

Ergebnis ist eine **deduplicate Liste** aller jemals konfigurierten SIP-Server
(über mehrere callList-Einträge hinweg) — UI-Reuse, damit man bei neuem
Kontakt nicht nochmal alle Server-Details eintippen muss.

**Wichtig: `callee` ist NICHT im SELECT!** Pro callee gibt es einen eigenen
`sipServer`-Eintrag (`INSERT INTO sipServer(...) VALUES (server, account, password, callee)`).
Eine Server-Dedupe schiebt nur Server/User-Combinations zusammen.

### `addCallList.lua` / `updateCallList.lua`

Schreiben in `callList` (+ ggf. `sipServer`). Triggern danach
`AT+B RELOAD callList\r\n` → avlink ruft `pjsip_add_account_common` für
neuen Eintrag.

Validierung in Lua:
- `callNo` muss unique sein (-4 = Status 4 = "Anrufnummer existiert")
- `callList.key` muss unique sein (-5 = Status 5 = "Schlüssel existiert")

### Lua → C Bridge

`avlink` ruft auch **selbst Lua-Code auf** (eingebettete Lua-VM):

```
Get_callee
lua_pcall Get_callee %d.
```

`Get_callee` ist eine Lua-Funktion in `/customer/share/config.lua` (Binary-String:
`/customer/share/config.lua`), die für eine gegebene `key`/`callNo` den
zugehörigen Callee aus der DB liefert. Vermutet: avlink hat keinen eigenen
SQLite-Reader, sondern delegiert komplexe Joins an Lua. Diese Datei ist im
Dump aber **nicht enthalten** — sie wird vom Hersteller mit dem Image
deployed und liegt eventuell im verschlüsselten Teil.

---

## 8. Hidden Features

### DTMF — wichtigste Funktion!

**Tür öffnen per DTMF**:

```
on IPC_SIP_DTMF_OPEN_DOOR=%c
```

Pjsua decodiert eingehende RFC2833-DTMF-Töne und ein DTMF-Trigger schickt
`IPC_SIP_DTMF_OPEN_DOOR` an avlink. Avlink mappt dann die Ziffer (vermutlich
auf den `key_setting`-Config — der ist in unserem Dump leer `{}`).

Aus pjsua-Strings:
```
Incoming DTMF on call %d: %c
Received DTMF digit %c, vol=%d
Call %d dialing DTMF %.*s
Call %d: DTMF sent successfully with INFO
DTMF strings to send (0-9*R#A-B)
```

**Beide Methoden** sind aktiv:
- RFC2833 (in-band RTP, primär)
- SIP INFO (out-of-band, fallback)

### Konsequenz: HA als App-Replacement

Wenn HA als SIP-Endpoint **mit dem GW telefoniert** und dann eine `*`-Ziffer
(oder die in `key_setting` definierte Ziffer) per DTMF sendet → **Tür öffnet**.
Das ist exakt der Mechanismus, den die Original-iLifestyle-App nutzt.

→ Realistisch ist das aber nicht der einfachste HA-Pfad — `AT+B UART unlock`
auf TCP 10087 ist trivialer.

### Console-Mode (CLI)

PJSIP-Library hat einen vollwertigen Telnet-CLI-Modus:

```
--no-cli-console    Disable CLI console
pj_cli_sess_exec
handle_exec
```

Aktiv per Default; man kann auf STDIN tippen:
- `q` = Quit
- `L` = Reload
- `cl` = List ports
- `cc` = Connect port
- `d` = Dump status
- `dd` = Dump detailed
- `#` = Send RFC 2833 DTMF
- `*` = Send DTMF with INFO

Da avlink pjsua aber als Background-Prozess startet (`pjsua &`), ist STDIN
ein Pipe → die CLI ist effektiv tot. Wenn man pjsua manuell killt und vom
SSH-Terminal aus startet, hat man die volle Shell.

### Replaces / Call-Transfer

```
pjsip_replaces_init_module
pjsua_call_xfer_replaces
Require=replaces&
```

Vollständige RFC3891-Replaces-Unterstützung — d.h. Anrufer kann übernommen
werden (Call-Pickup). avlink nutzt das nicht aktiv, der Code-Pfad ist aber da.

### ICE / TURN / STUN

```
--use-ice, --ice-regular, --use-turn, --turn-srv, --turn-user, --turn-passwd
--stun-srv
```

Alles im Binary, aber **nicht in der Config aktiv**. iLifestyle ist klassisches
SIP-mit-NAT-Traversal über das Cloud-Backend (Server hält persistente Connection
nach LAN).

### SRTP

```
--use-srtp 0|1|2
--srtp-secure 0|1|2 (SRTP-required: 0:no, 1:tls, 2:sips)
```

Im Binary verfügbar, in Praxis aus (sonst wären MITM-Hooks im PCAP sichtbar).

### Multi-Codec Audio

```
--add-codec, --dis-codec, G.711, G.722, ilbc, --ilbc-mode
```

DB-Config zeigt `"audio_format":"G.711"` — primär µ-law. Andere Codecs sind
einkompiliert, aber nicht aktiviert.

### Video

```
m_remote_video_url=%s rtmp=%s rtsp=%s
VideoURL-RTSP, VideoURL-RTMP
ControlVideoRtp.cpp
```

Video läuft **nicht über SIP-SDP** — die SIP-Session signalisiert nur eine
"VideoURL"-Custom-Header, der den remote Client zu `rtsp://…` oder
`rtmp://rtmp.de.ilifestyle-cloud.com/live/<key>` schickt. Das ist der Grund,
warum die iLifestyle-App nicht klassisches Video-SIP nutzt — sie pulled das
Video parallel per RTMP/RTSP aus der Cloud.

---

## Was machbar für Home Assistant — Übersicht

| Ziel | Realisierbar? | Pfad |
|---|---|---|
| HA als **zweiter SIP-Account** auf dem GW registrieren | ✅ Ja | `/api/addCallList` mit `callType=2`, `server=HA-Asterisk-IP`, fertige Account-ID/Pass. GW registriert sich periodisch bei HA. |
| HA als **eingehender Caller** (HA klingelt am GW direkt per LAN-SIP) | ❌ Nein | `check_outdoor_incoming_call` filtert nach From-URI gegen callList — externe Caller werden gedropt. |
| HA als **Registrar für das GW** (GW registriert sich bei HA) | ⚠️ Ja, aber Cloud-Account bleibt | Cloud-Account in `/api/sip` lässt sich auf HA-Server umbiegen — dann verliert man iLifestyle-App. Besser: parallelen Account via callList. |
| **Door-Open per DTMF** wenn HA mit GW telefoniert | ✅ Ja | Anruf annehmen, `*` als RFC2833-DTMF senden — avlink mappt auf elock. (Mapping aber via `key_setting` config, derzeit leer → vorher `/api/userKey` setzen.) |
| **Inbound-Notify** ("jemand klingelt") via SIP | ✅ Ja (über HA-Asterisk-Account) | Wenn HA als callType=2-Callee eingetragen ist, schickt GW bei Tür-Klingel ein INVITE an HA. |
| **Audio-Stream** (Wechselsprechen) | ✅ Ja | Sobald SIP-Call aufgebaut, läuft G.711 µ-law RTP. UDP-RTP-Port lokal **33333+** auf GW-Seite. |
| **Video-Stream über SIP** | ❌ Nein | Video ist Custom-RTSP-URL, kein SDP-Video. Stattdessen direkt `rtsp://admin:admin@<GW>/live.sdp` ziehen. |
| **MITM**-resistente Verbindung | ❌ Nein | Kein Cert-Pinning, TLS-Verify scheinbar aus. Nur im LAN sicher. |
| **Console-Debugging** | ⚠️ Teils | Pjsua-CLI braucht TTY → erst `killall pjsua && /customer/app/sbin/pjsua` interaktiv starten. Dann aber bricht avlink-Integration. Eher für Forensik denn Produktion. |
| **Mehr als 1 third-party Account** | ✅ Ja (bis 8) | `PJSUA_MAX_ACC=8` (PJSIP-Default, nicht customisiert sichtbar). Einfach mehr callList-Einträge mit callType=2. |

---

## Empfohlener HA-Integrationspfad

**Mittelfristig** (wenn man richtig integriert sein will, statt nur `uart2d`-AT+B):

1. Asterisk-Add-on (oder externes FreePBX) im LAN aufstellen, User `villa-gw` mit
   bekanntem Passwort.
2. Per `/api/addCallList` einen Eintrag anlegen mit:
   - `callType=2`
   - `server=<HA-IP>` (UDP/TCP, kein TLS nötig — LAN)
   - `account=villa-gw`, `password=…`
   - `callee=homeassistant`
3. Per `/api/userKey` die DTMF-Ziffer für "Tür öffnen" definieren (z.B. `*`).
4. HA-Asterisk-Hook: bei eingehendem Call von `villa-gw` → `notify.mobile_app`,
   parallel `binary_sensor.tuer_klingel` auf `on` für 3 Sekunden.
5. Wenn User auf Notification "Öffnen" tappt → HA pickt den Call auf, sendet
   `*` DTMF, hangup. Tür ist offen, ohne `AT+B UART unlock` nutzen zu müssen.

**Vorteile gegenüber aktuellem AT+B-Hack:**
- Audio (bidirektional) ist Free, kein zusätzlicher Code-Pfad.
- Klingel-Event ist push (INVITE), nicht polling (`door_in`-ADC).
- Multiple HA-Endgeräte können simultan klingeln.

**Nachteile:**
- Mehr moving parts (Asterisk).
- HACS-Integration müsste das SIP-Setup wrappen — `/api/addCallList` aufzurufen
  ist trivial, aber ein User braucht trotzdem einen funktionierenden SIP-Server.

---

## Offene Fragen / Was wir nicht reverse-engineered haben

| Frage | Warum offen |
|---|---|
| Genaues numerisches Mapping der `IPC_SIP_*`-Enum-Werte | Im Binary nur via Strings sichtbar, keine Konstanten — müsste man per `strace -e trace=read` auf `/var/run/msg.socket` capturen. |
| `config.lua`-Inhalt (`Get_callee`) | Datei nicht im Dump enthalten. |
| Genaue Bus-zu-SIP-Bridge-Logik bei eingehendem Bus-Anruf | `<sip:bus%d@…>` ist klar, aber wie `bus%d` zum Cloud-Account-Routing passt, ist nur grob aus `check_outdoor_incoming_call` ableitbar. |
| Welche Lua-Funktionen avlink über `lua_pcall` aufruft (außer `Get_callee`) | Müsste man dynamisch tracen. |
| Ob `--auto-answer` jemals von avlink gesetzt wird | Nicht in den sichtbaren Strings — vermutlich nein. |
| Was `AT+B CHECKSIP 3 %d` macht | Auch im avlink-Code referenziert, aber kein Lua-Endpoint. Vielleicht ein interner Health-Check. |

---

## Quellen

- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/app/sbin/pjsua` (Binary)
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/app/sbin/avlink` (Binary)
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/lua/sip.lua`
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/lua/getServerList.lua`
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/lua/addCallList.lua`
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/lua/updateCallList.lua`
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/lua/getCallList.lua`
- `/Users/sascha.nordquist/git/private/homeassistant/homeassistant-villa-gw/villa_gw_dump/customer/share/avl20.dump.sql` (Schema-Kommentare)
- `villa_gw_dump/customer/share/avl20.schema.sql`
