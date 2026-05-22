# Villa GW V3.0 — P2P, RTMP-Cloud, RTSP-Local: Stream-Routing-Reverse-Engineering

**Stand:** 2026-05-22 (Live-Device 203.0.113.10, AVL20-P, FW siehe `/etc/VERSION`).
**Quellen:**
- Lua-Handler `customer/lua/{p2p,video,avSetting}.lua`
- Daemons `customer/app/sbin/{mimedia,avlink,uart2d,pjsua,custode2.lua}` (Strings + Code-Lese)
- Live-Config `GET /api/video` und `GET /api/avSetting` auf dem Gerät
- Cross-Refs: `database.md`, `cloud_sync.md`, `small_daemons.md`

## TL;DR

| Frage | Antwort |
|---|---|
| Wie wird "RTMP-cloud" gewählt? | `config[video].transfer = 1` (DB-Wert), gesetzt durch Cloud-Sync (`autoSync.lua`) oder REST `POST /api/cloudDevice` |
| Wie wird P2P aktiviert? | `transfer = 2` — **nur dann** startet `custode2.lua` den `media-server`-Prozess |
| Bricht HA-RTSP wenn wir Mode wechseln? | **Nein.** `rtsp_demo` läuft im `mimedia`-Master und ist von `transfer` unabhängig |
| Aktueller Live-Wert | `transfer:1`, `enable:true`, `rtsp:"rtsp://%s/live.sdp"`, RTMP-Stream-Key in Cloud-URL |
| Was startet den H.264-Encoder? | Erst `AT+B RTSP start` / `AT+B VIDEO START` an `mimedia:10600` — gesendet bei eingehendem RTSP-Client bzw. bei SIP-Call |
| Empfehlung | **`transfer = 0`** setzen (siehe §8) — schaltet Cloud-Push ab, RTSP läuft weiter, HA bleibt funktional |

---

## 1. Transfer-Mode-Semantik (`config[video].transfer`)

**Storage:** SQLite `/customer/share/avl20.db`, Tabelle `config`, Row `name='video'`,
Spalte `item` enthält JSON:

```json
{
  "enable":   true,
  "rtsp":     "rtsp://%s/live.sdp",
  "rtmp":     "rtmp://rtmp.de.ilifestyle-cloud.com/live/RTMP_STREAM_KEY_REDACTED",
  "p2p_server": "p2p.de.ilifestyle-cloud.com",
  "transfer": 1
}
```

**Werte (definitiv aus Code rekonstruiert):**

| Wert | Bedeutung | Code-Beleg | Auswirkung |
|---|---|---|---|
| `0` | "Off" / nur Local | kein Branch im Code, default-Fallthrough | Kein RTMP-Publish, kein P2P-Daemon. RTSP-Local läuft weiter. |
| `1` | **RTMP-Cloud-Push** (aktuell) | `mimedia` published RTMP zu `rtmp://...` aus `video.rtmp` | GW pusht Live-Stream zur Hersteller-Cloud sobald Encoder läuft |
| `2` | **P2P** | `custode2.lua:520` — `if video.transfer == 2 ... start media-server` | `media-server`-Prozess wird gestartet, hängt an `p2p.de.ilifestyle-cloud.com` |

**Wichtig:** der Default-Fallback in `autoSync.lua:173` und `cloudDevice.lua:32` ist `2`,
falls die Cloud kein `video_transfer` schickt. Bei initial neu-gepairten Geräten kommt also
zuerst P2P. Unser GW steht auf `1` weil die Cloud explizit `video_transfer:1` zurückliefert.

**Bus zwischen DB und Daemon:**

```
HTTP POST /api/video → video.lua → UPDATE config[video]
                                 → AT+B RELOAD     (10086, avlink)
                                 → sleep 2
                                 → AT+B RELOAD himedia  (60000, custode2)
                                                      → media_reload()
                                                          → killall mimedia
                                                          → mimedia master &
                                                          → p2p_start()
                                                              → if transfer==2: start media-server
```

(`video.lua:26-49`)

---

## 2. P2P-Implementation (`p2p.lua` + `media-server`)

### REST-Handler `p2p.lua` — verwaltet `config[p2p]`, NICHT `video.p2p_server`

```lua
-- GET  /api/p2p  → liest aus DB + ruft `AT+B CONTACTS` an avlink:10086
-- POST /api/p2p  → UPDATE config[p2p] + AT+B RELOAD
```

`config[p2p]` ist im Default `[]` (leer). Es ist eine **zweite, ungenutzte** P2P-Server-Liste,
nicht der Endpoint, der wirklich verwendet wird. Der echte P2P-Endpoint steckt in
`config[video].p2p_server`.

`AT+B CONTACTS` ist außerdem ein verirrtes Kommando — es liefert nicht "P2P-Konfig"
sondern die Bus-Kontaktliste; siehe `database.md` (callList-Tabelle). `p2p.lua` ist
also **kosmetisch** und entkoppelt von dem, was tatsächlich gestartet wird.

### Wirklicher P2P-Daemon: `media-server`

`custode2.lua:514-543`:

```lua
local function p2p_start()
    R.p2p.start = true
    local video = get_video_config()
    if video then
        if video.transfer == 2 and video.p2p_server and #video.p2p_server > 0 then
            R.p2p.server = video.p2p_server
            local p2p_cmd = 'start-stop-daemon -S '..video.p2p_server..' -x media-server &'
            os.execute(p2p_cmd)
        end
    end
end
```

- Binary `media-server` ist **nicht im Firmware-Dump** — wird vermutlich erst zur Laufzeit
  über `cloudDevice`/Pairing geladen oder ist nur auf dem Gerät installiert (`/customer/`-Pfad
  unbekannt; `which media-server` würde es zeigen).
- Argument `-S <p2p_server>` heißt: der Daemon connectet zum Cloud-Helper (TCP, proprietäres
  Protokoll). Stream-Daten laufen dann über diese Outbound-Connection. NAT-Traversal
  funktioniert ohne Port-Forward.
- Heartbeat-File `/var/run/gst-p2p.heartbeat` — wenn 30 s × 5 keine Update → `custode2`
  killt und restartet `media-server` (`custode2.lua:995-1019`).
- `gst-` Prefix lässt vermuten: GStreamer-basierter Stream-Pusher.

### P2P-Cloud-Server

`p2p.de.ilifestyle-cloud.com` (siehe `cloud_sync.md`). Protokoll proprietär,
nicht replizierbar ohne erheblichen Aufwand. Vendor: Systec / iLifestyle.

---

## 3. RTMP-Cloud (aktueller Modus)

### Stream-Key-Generation

Stream-Key `RTMP_STREAM_KEY_REDACTED` (siehe `secrets.local.md`) wird vom
**Cloud-Server** generiert und beim Pairing/Sync via `GET /api/device?id=<MAC>` an das GW
geliefert (Feld `video_url`). Das GW patcht das Feld nicht — es übernimmt 1:1 was die
Cloud sendet (`autoSync.lua:154`).

Heißt: der Stream-Key ist **device-bound** (vermutlich aus MAC + Server-Secret abgeleitet),
nicht erratbar, aber im DB-Klartext lesbar.

### Publish-Pfad in `mimedia`

Aus `strings mimedia`:

```
rtmp://192.168.1.163/live/stream     ← Debug-Default
RTMP is ready %X, id = %d
rtmp url = %s
rtmp server exit!!!
NetStream.Publish.Start
NetStream.Publish.PublishNotify
```

`mimedia` enthält einen vollständigen **librtmp-Publisher-Stack** (FCPublish/FCUnpublish,
AMF-Encoder, Publisher-Auth). Bei `transfer ∈ {1}` und aktivem Encoder pusht es den
H.264-Stream in die Cloud. Bei `transfer = 2` wird der gleiche Encoder-Output stattdessen
über `media-server` → P2P-Endpoint geroutet (separater Prozess, anderes Protokoll).

Wann genau `mimedia` **anfängt** zu publishen wird gleich in §6 geklärt — Spoiler: erst bei
explizitem AT+B-Wake-Befehl, nicht permanent.

### RTMP vs RTSP — App-Pfade

| Pfad | Konsument | wann genutzt | Auswirkung |
|---|---|---|---|
| `rtmp://rtmp.de.ilifestyle-cloud.com/live/<key>` | iLifestyle-App via Cloud-Pull | bei Klingel-Push-Nachricht aus App | Cloud reicht Stream an App weiter |
| `rtsp://<gw-ip>/live.sdp` | LAN-Clients (HA, VLC) | jederzeit lokal | Direktzugriff, kein Cloud-Routing |
| P2P (proprietär) | iLifestyle-App via P2P-Server | wenn `transfer=2` | App connectet via NAT-Traversal direkt |

---

## 4. Local-Mode (`transfer = 0`)

**Ist es möglich?** Ja, technisch.

- Der Code prüft nur `if video.transfer == 2` für `media-server`-Start.
  Bei `0` (oder jedem anderen Wert ≠ 2) wird **kein** P2P-Daemon gestartet.
- Ob `mimedia` bei `transfer=0` den RTMP-Push *auch* abschaltet, ist aus den Strings
  nicht direkt ablesbar. Es gibt aber **keinen** Code-Pfad in `mimedia`, der ohne
  `AT+B VIDEO START` einen RTMP-Connect aufbaut. Heißt: RTMP-Connect passiert nur als
  Reaktion auf einen externen Trigger (siehe §6).
- `rtsp_demo` (RTSP-Server in `mimedia`) startet beim `mimedia`-Boot **unabhängig** vom
  Transfer-Mode — verifiziert durch `[INFO] rtsp server demo starting on port %d` in
  jedem Boot-Log.

**Auswirkungen wenn man `transfer = 0` setzt:**

| Funktion | Status |
|---|---|
| RTSP `rtsp://<IP>/live.sdp` | **bleibt** (sofort, vor jedem Cloud-Roundtrip) |
| Snap `/customer/config/snap.jpg` über `AT+B MJPG Snap` | **bleibt** (lokal, kein Cloud-Pfad) |
| RTMP-Push zur ilifestyle-Cloud | **wahrscheinlich aus** (kein Trigger mehr) |
| `media-server`-Prozess | nicht gestartet |
| SIP-Calls / Klingel-Events | **bleiben** (komplett separate Pipeline) |
| MQTT-Events (Klingel) | **bleiben** (avlink/MQTT-Client unabhängig) |
| iLifestyle-App Live-View | **bricht** (App hat keine Stream-Source) |

**Risiko:** Cloud-Sync setzt nach jedem Reboot via `autoSync.lua:173` den Wert wieder
auf `c_transfer` (= was die Cloud zurückliefert, aktuell `1`). Heißt: `transfer=0` ist
**nicht persistent** — nach jedem Reboot/Re-Sync zurück auf `1`.

**Persistent-Lösung:**
1. Cloud-Sync deaktivieren (`config[purpose].bindSelf = 0`, siehe `database.md` §7)
2. Oder DNS-Hijack `de.ilifestyle-cloud.com` → liefern Replica-`/api/device` mit
   `"video_transfer": 0`
3. Oder Lua-Patch in `autoSync.lua:173` — Pfad: `getConfig.transfer or c_transfer or 2`
   ändern zu `0`.

---

## 5. HA-RTSP-Pfad — funktioniert in jedem Modus?

**Ja.** Der RTSP-Server ist eine **eigenständige Komponente** in `mimedia` (Strings:
`rtsp_demo.c`, `rtsp_new_demo`, `rtsp_handle_PLAY` ...), nicht von der `transfer`-Variable
abhängig.

Verifikation via Live-OPTIONS/DESCRIBE auf `203.0.113.10:554`:

```
RTSP/1.0 200 OK
Server: rtsp_demo
Content-Type: application/sdp

v=0
s=rtsp_demo
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1;sprop-parameter-sets=Z0IAHukBQHtCAAAH0gABhwQI,aMqPIA==
a=control:rtsp://203.0.113.10/live.sdp/track1
```

**Beobachtungen:**

- **Nur Video.** Kein `m=audio` im SDP → der RTSP-Stream ist **video-only**. Audio läuft
  ausschließlich über SIP/RTP, nicht RTSP. (Erklärt warum die HA-Camera kein Audio hat.)
- **H.264 Baseline** (Profile-IDC `0x42`, Level 30 — siehe SPS-Bytes). Kompatibel mit
  allen modernen Browsern und go2rtc.
- **packetization-mode=1** → STAP-A + FU-A erlaubt, was modern ist.
- `track1`-Control-URL → braucht SETUP+PLAY (Standard-RTSP).

**Konsequenz:** HA `camera`-Integration funktioniert immer, **solange `mimedia` läuft**
und das Gerät RTSP-Port 554 erreichbar ist. Cloud-Status egal. P2P egal. Transfer egal.

**Edge Case:** Wenn `mimedia` crashed / nicht läuft → `custode2.lua p2p_watch_dog`
restartet. Wenn `media_reload` via `AT+B RELOAD himedia` läuft → ~5 s Downtime (kill +
sleep 3 + restart). HA sieht in der Zeit "stream offline" → autoreconnect über
`stream.restart` reicht.

---

## 6. Encoder-Wake-Logik — wann startet H.264-Encoder?

**Kern-Erkenntnis:** Der H.264-Encoder läuft **nicht permanent**. `mimedia` startet ihn
on-demand, getriggert via `AT+B`-Kommandos auf TCP `127.0.0.1:10600` (`mimedia`-Listen-
Port).

### AT+B-Kommandos an `mimedia:10600`

Aus `strings mimedia`:

| Kommando | Wirkung |
|---|---|
| `AT+B VIDEO START\r\n` | Startet komplette VENC-Pipeline (`ST_Vif_StartPort` → `ST_Vpe_StartPort` → `ST_Venc_StartChannel`), beginnt RTMP-Publish wenn `transfer=1` |
| `AT+B VIDEO STOP\r\n` | Stoppt komplette Pipeline, gibt VENC-Channel frei |
| `AT+B RTSP start\r\n` | Aktiviert RTSP-Encoder-Tap (`get_rtsp_stream_start`) |
| `AT+B RTSP stop\r\n` | Deaktiviert RTSP-Tap (`rtsp_stream_stop`) |
| `AT+B MJPG Snap\r\n` | One-Shot: VENC in JPEG-Mode, ein Frame → `/customer/config/snap.jpg` |

### Wer sendet diese Kommandos?

`strings uart2d` zeigt `AT+B VIDEO START` und `AT+B MJPG Snap` als Strings, aber
`uart2d` "tcp send to 10086" (avlink) — nicht direkt zu 10600. Heißt:

```
uart2d → 10086 (avlink) →  ? → 10600 (mimedia)
                       └→ pjsua (SIP-Stack)
```

In `avlink`-Strings kein `10600`-Ref → das Forwarding läuft vermutlich über `pjsua` oder
einen anderen Subprozess. `pjsua` enthält Strings `VideoURL-RTMP`/`VideoURL-RTSP` und
`m_remote_video_url=`, was nahelegt: bei SIP-INVITE schickt es VIDEO START.

**Empirisch beobachtbar:** Der RTSP-Stream am Live-Gerät **antwortet sofort** auf
DESCRIBE → der `rtsp_demo`-Server läuft immer; **aber** ob danach echte Frames kommen,
hängt davon ab ob der Encoder aktiv ist. Aus `project_villa_gw_stream_ondemand`
(Memory): RTSP liefert **blaues Bild** wenn niemand "wakes" das Video — heißt der
Encoder-Tap füttert leere/Default-Frames bis ein VIDEO START kommt.

**Trigger-Quellen für VIDEO START:**

1. **SIP-Call eingehend** (Klingel gedrückt → Innenstation klingelt) → `pjsua` ruft
   VIDEO START.
2. **App-Pull via Cloud** → Cloud schickt Pairing-Signal über MQTT → `avlink` ruft
   VIDEO START.
3. **HA via RTSP-PLAY?** Möglicherweise — wenn `rtsp_demo.c` einen `on_play`-Hook hat,
   der intern VIDEO START schickt. Aus den Strings nicht ablesbar.

→ **Action-Item:** Wenn HA-Stream "blaues Bild" zeigt, manueller Wake via
   `echo -ne 'AT+B VIDEO START\r\n' | nc 203.0.113.10 10600` testen.
   (Port 10600 ist Live auf der GW-IP offen, siehe nmap-Scan.)

### MJPG-Snap

`AT+B MJPG Snap\r\n` an `mimedia:10600`:

1. `MI_VENC_SetJpegParam` (Codec → MJPEG, Qualität-Default)
2. `MI_VENC_StartRecvPic` (One-Shot)
3. `MI_VENC_GetStream` → File-Write nach `/customer/config/snap.jpg`
4. `MI_VENC_StopRecvPic`

Snap-File ist **single-shot**, jedes Snap überschreibt das vorherige. Wird auch beim
verpassten Anruf von `firmware_upgrade.lua AT+B RECORD` gelesen (siehe `small_daemons.md`).

---

## 7. MJPG-Snap — Latenz, Frequenz-Limit

### Latenz (geschätzt)

Aus Code-Pfad rekonstruiert:

1. VENC umconfigurieren (H.264 → MJPEG): ~50–100 ms (SoC-VPU-Umschaltung)
2. Frame erfassen + encoden: ~30–60 ms (ein JPEG-Frame bei ~720p)
3. File-Write: ~10 ms
4. Falls Encoder gerade RTMP/RTSP pusht: VENC ist schon initialisiert → schneller (~50 ms)
5. Falls Encoder schläft: Boot-Up VENC-Pipeline → +200–500 ms

**Realistische Wake-to-Snap-Latenz:** **0.3–1.0 s** (warm) bzw. **1–2 s** (kalt).

### Frequenz-Limit

- **Kein expliziter Rate-Limiter im Code.** Wer `AT+B MJPG Snap` zu schnell hintereinander
  sendet, riskiert dass VENC noch im Reconfig-State hängt → `ST_Venc_StartChannel fail`
  in den Logs.
- **Praktisches Limit:** ≤1 Snap pro 2 s. Bei höherer Frequenz lieber RTSP nutzen.
- **Konflikt mit RTMP-Push:** Während `transfer=1` aktiv pusht, blockiert MJPG den
  H.264-Encoder. `mimedia` müsste zwischen H.264-RTMP und JPEG umschalten — das könnte
  den RTMP-Stream stören. Memory `project_villa_gw_ttys1_readers_break_commands` warnt
  schon vor ähnlichen Bus-Konflikten.

### MJPG vs RTSP-Snapshot

HA hat zwei Optionen für Einzelbild:

| Methode | Latenz | Auflösung | Codec | Bus-Last |
|---|---|---|---|---|
| `AT+B MJPG Snap` + lese `/customer/config/snap.jpg` | 0.3–2 s | wie VENC (720p?) | JPEG | gering, aber stört Encoder |
| ffmpeg-snapshot vom RTSP-Stream | 0.5–1 s | exakt VENC-Auflösung | aus H.264-IDR extrahiert | mittel |

→ **Empfehlung:** für HA-Snapshot-Kamera **lieber RTSP+ffmpeg**, weil:
1. Kein extra Wake-Roundtrip.
2. Kein VENC-Umschalt-Risiko.
3. HA macht das nativ (`stream.recording`/`camera.snapshot`).

Snap-File ist nur sinnvoll wenn HA es nicht selbst aus dem Stream ziehen kann (Off-Cycle).

---

## 8. HA-Empfehlung — Mode auf Local setzen?

### Aktuelle Situation

- Live: `transfer=1`, RTMP-Push aktiv zur Hersteller-Cloud.
- Cloud bekommt unseren Türklingel-Stream live (bei jedem VIDEO START).
- HA nutzt nur RTSP-Local — Cloud-Push ist Datenschleuse ohne Nutzen für uns.

### Risiko-Bewertung "RTMP-Cloud bleiben" (Status Quo)

| Risiko | Schwere |
|---|---|
| Cloud sieht alle Klingel-Streams live | **hoch** (Datenschutz) |
| Stream-Key in DB-Klartext + RTMP unverschlüsselt (kein RTMPS!) | mittel (jeder im LAN kann mit-streamen wenn er den Key kennt) |
| Bandbreite: 720p H.264 ~1 Mbit Uplink während Klingel | niedrig |
| Cloud kann Stream-Key rotieren → Pairing-Funktion bricht | sehr niedrig (außerplanmäßig) |

### Risiko-Bewertung "transfer=0 setzen"

| Auswirkung | Schwere |
|---|---|
| iLifestyle-App kein Live-Video mehr | **niedrig** — App benutzen wir eh nicht (HA-only) |
| Cloud-Sync setzt es zurück | mittel — braucht zusätzlich `bindSelf=0` oder DNS-Hijack für Persistenz |
| HA-RTSP betroffen | **nein** — bleibt komplett funktional |
| MQTT/SIP/Klingel-Events betroffen | **nein** — separate Pipelines |
| Wake-Logik betroffen | **eventuell** — bei `transfer=0` startet möglicherweise auch der RTMP-Encoder-Tap nicht; RTSP-Tap startet aber separat. Muss empirisch verifiziert werden |

### Empfehlung

**Ja, `transfer=0` setzen, aber gestaffelt:**

#### Phase 1: Test (reversibel)

```bash
# via REST (admin/admin Auth)
curl -X POST http://203.0.113.10/api/video \
    -H "Content-Type: application/json" \
    -b /tmp/villa_cookies.txt \
    -d '{"enable":true,"rtsp":"rtsp://%s/live.sdp","rtmp":"rtmp://rtmp.de.ilifestyle-cloud.com/live/RTMP_STREAM_KEY_REDACTED","p2p_server":"p2p.de.ilifestyle-cloud.com","transfer":0}'
```

Dann verifizieren:
1. `nmap -p 554 203.0.113.10` → Port 554 weiterhin offen
2. RTSP-DESCRIBE → 200 OK
3. HA-Kamera lädt live ein Bild beim Klingeln
4. `tcpdump -i any host rtmp.de.ilifestyle-cloud.com` → kein Outbound mehr

#### Phase 2: Persistent (falls Phase 1 OK)

`config[purpose].bindSelf = 0` setzen, damit `autoSync.lua` nicht beim Reboot überschreibt:

```sql
-- via SSH
sqlite3 /customer/share/avl20.db \
  "UPDATE config SET item = json_set(item, '\$.bindSelf', 0) WHERE name='purpose';"
```

Oder schöner via `POST /api/purpose` (siehe `http_api.md`).

#### Phase 3 (optional): Cloud abklemmen

Wer es ganz sauber will: zusätzlich `mqtt_server` auf lokalen HA-Broker umlenken
(siehe `database.md` §5) → Cloud komplett deaktiviert, Klingel-Events kommen direkt
in HA.

### Wann **nicht** umstellen?

- Wenn die iLifestyle-App produktiv genutzt wird (z.B. Familienmitglieder mit
  Smartphone-Klingel-Push) — die braucht entweder RTMP-Cloud (`transfer=1`) oder
  P2P (`transfer=2`).
- Wenn die HA-Integration noch nicht stabil läuft und die App als Fallback dient.

---

## 9. Open Questions / Empirisch zu Verifizieren

1. **Tut `rtsp_demo` "wake" beim PLAY?** Sendet HA via `camera.stream_source` ein PLAY,
   startet dann der VENC-Encoder von selbst? Test: `mimedia` mit `strace` während
   RTSP-Client connectet beobachten.
2. **`transfer=0` → wirklich kein RTMP?** Strings im `mimedia` zeigen den RTMP-Code-Pfad,
   aber ob die `transfer`-Variable ihn auch wirklich gattert, ist nicht aus Lua erkennbar
   (steht in mimedia-C-Code, der `transfer` aus DB liest — siehe Format-String
   `get video param: enable = %d, rtsp = %s, rtmp = %s` — beachte: `transfer` fehlt in
   diesem Log, könnte heißen dass mimedia es gar nicht lesst und der gesamte Branch
   nur in `custode2` entschieden wird).
3. **MJPG-Snap im RTMP-aktiven Zustand:** Stört es das laufende RTMP? Empirisch testen.
4. **Wo ist `media-server` Binary?** Im Dump nicht vorhanden. SSH ins Gerät, `which
   media-server` + `dpkg -S` oder `find / -name media-server` würde es zeigen.
5. **`config[p2p]`-Liste:** Wer konsumiert das? Bisher kein Daemon gefunden, der `p2p`
   (nicht `video.p2p_server`) liest. Vermutung: Failover-Liste für `media-server` per
   `-S <fallback>`-Argument-Erweiterung, aber im aktuellen Custode-Code nicht
   referenziert.

---

## 10. Quellen-Cross-Reference

| Behauptung | Quelle |
|---|---|
| `transfer=2` startet `media-server` | `customer/app/sbin/custode2.lua:520-525` |
| `transfer`-Default-Fallback ist `2` | `customer/lua/autoSync.lua:173`, `cloudDevice.lua:32` |
| Stream-Key kommt aus `code.video_url` (Cloud) | `autoSync.lua:154` |
| RTSP-Server `rtsp_demo` ist Teil von `mimedia` | `strings mimedia`: `rtsp_demo.c`, `rtsp server demo starting on port %d` |
| RTSP-SDP video-only, H.264 Baseline | Live OPTIONS/DESCRIBE auf `203.0.113.10:554` |
| `mimedia` listen TCP 10600 für AT+B | `strings mimedia`: `10600 tcp recv len:%d, data:%s` |
| `AT+B MJPG Snap` schreibt `/customer/config/snap.jpg` | `strings mimedia`, `firmware_upgrade.lua` |
| Heartbeat-File `/var/run/gst-p2p.heartbeat` | `custode2.lua:982,1005` |
| P2P-Watchdog-Logik (5×30s → restart) | `custode2.lua:995-1019` |
| `media-server`-Binary nicht im Dump | `find villa_gw_dump -name media-server` (leer) |
| `video.lua` POST triggert beide Reloads | `customer/lua/video.lua:26-49` |

---

## 11. Quick-Reference — Endpoints / Ports

| Komponente | Listen | Protokoll | Auth |
|---|---|---|---|
| `rtsp_demo` (in `mimedia`) | `0.0.0.0:554` | RTSP | **keine** |
| `mimedia` AT+B-Bus | `127.0.0.1:10600` | AT+B-Text | keine, loopback |
| `avlink` AT+B-Bus | `127.0.0.1:10086` | AT+B-Text | keine, loopback |
| `custode2` AT+B-Bus | `127.0.0.1:60000` | AT+B-Text | keine, loopback |
| `nginx` Lua-REST | `0.0.0.0:80` | HTTP+JSON | session (admin/admin Default) |
| RTMP-Publisher (in `mimedia`) | Outbound zu Cloud-RTMP | RTMP | publisher-auth (in Key) |
| `media-server` (wenn `transfer=2`) | Outbound zu P2P-Server | proprietär | unbekannt |

**Verifizierte offene Ports am Live-Gerät 203.0.113.10 (nmap):**
`554, 10086, 10600` (RTSP, avlink-bus, mimedia-bus). Port 10600 ist auf dem **LAN-Interface**
offen — das ist eine ungewollte Exposure (eigentlich nur loopback gedacht). Härtungs-Hinweis
für `docs/security.md`: lokale `iptables`-Drop für Port 10600 von externen Interfaces.
