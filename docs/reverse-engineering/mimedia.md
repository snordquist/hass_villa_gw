# Villa GW V3.0 — `mimedia` Daemon (Reverse Engineering)

Tiefenanalyse des `mimedia`-Binary vom Villa GW V3.0 (AVL20P).
Quelle: `villa_gw_dump/customer/app/sbin/mimedia` — ARM 32-bit ELF, dynamisch gelinkt,
stripped, BuildID `c34bb293…f510`, 858 928 Byte, gebaut mit GCC 9.1.0.
Source-Hinweis im Binary: `/root/workspace/evm/systec/apps/avio_p2p/common/mi_comm_audio.c`
→ Vendor ist **systec**, Projektname intern `avio_p2p`.

| Aspekt | Wert |
|---|---|
| Arch | armv7 (EABI5), `/lib/ld-linux-armhf.so.3` |
| TLS | libssl/libcrypto 1.0.0 |
| Media-SDK | Sigmastar MI ("MStar Infinity") — `libmi_sys/ai/ao/sensor/vif/vpe/venc/iqserver/divp/scl/disp/region` |
| RTMP | libRTMP (eingebettet — Adobe-Handshake + Auth-Strings vorhanden) |
| DB | SQLite3 statisch eingebaut (volle FTS5/JSON1) |
| Threads | `pthread_create`/`pthread_join` + `pthread_setschedparam` (Echtzeit-Prio für AI/AO) |

> mimedia ist **kein** generischer ONVIF/RTSP-Server. Keine ONVIF-, Tutk-, IOTC- oder
> Kalay-Strings. Es ist ein **Single-Vendor-Stack** mit eingebettetem RTSP-Demo-Server
> ("rtsp\_demo.c" — typisches Sigmastar-Sample) + RTMP-Push-Client + custom AT+B-Brücke.

---

## 1. Prozess-Modell (Master / avio-server / media-server)

mimedia wird von `custode2.lua` als **`mimedia master &`** gestartet (siehe
`custode2.lua:549`). Der Master `fork()`t zwei Worker-Children und überwacht sie:

```
[0] master: pid = %d , avio-server: pid = %d, count = %d
[1] master: pid = %d , media-server: pid = %d, count = %d
sig exit pid = %d children[%d].pid = %d, children[%d].status = %d
!!! children[%d]: pid = %d, exit status = %d
```

Auf den ersten Child-Slot lädt der Master den **avio-server** (das ist der A/V-Capture-
und RTSP-Prozess); der zweite Slot wäre für **media-server** (RTMP-Push) — der wird
allerdings **nicht** vom Master selbst gespawnt, sondern on-demand von `custode2.lua`
gestartet:

```lua
-- custode2.lua:522
local p2p_cmd = 'start-stop-daemon -S '.. video.p2p_server ..' -x media-server &'
```

Der `count`-Wert ist ein **Restart-Counter** — bei `exit_status != 0` killt der Master
über `signal()` (`Catch signal!!!`) Geschwister und respawnt. Watchdog-Stil
fork-supervisor, kein systemd.

### Sub-Threads in avio-server

`pthread_create`-Aufrufe (aus Stringlogs in `mi_comm_audio.c`):

| Thread | Zweck |
|---|---|
| `create ai thread.` | Audio-In: PCM von Sigmastar `MI_AI_GetFrame`, durch VQE (AEC/NS/AGC), `MI_AI_EnableAenc` → G.711/G.726/AAC, in RTP-Queue |
| `create ao thread.` | Audio-Out: RTP/G.711 vom Peer → `MI_AO_SetAdecAttr` → `MI_AO_SendFrame` |
| `rtsp-stream` / `rtsp_demo` listener | RTSP-Accept-Loop (Port 554) |
| `rtmp` push | nur wenn `video.transfer == 2` und `rtmp = "rtmp://…"` in DB |
| 10600 TCP listen | siehe §3 |
| VENC poll (`MI_VENC_GetFd` + `select()` + `MI_VENC_GetStream`) | H.264-NAL-Units in Shared-Packet-Queue |

VQE = "Voice Quality Enhancement". Die AI-Pipeline kann **Beamforming** (`MI_AI_DisableBf`)
und **Single-Sideband-Suppression** (`MI_AI_DisableSsl`) — bewusst über VQE konfigurierbar.

---

## 2. Pipeline (Sigmastar MI Bind-Graph)

Aus den Funktionsnamen `ST_Vif_*`, `ST_Vpe_*`, `ST_Venc_*`, `ST_Sys_Bind`,
`ST_StartPipeLine` rekonstruiert:

```
SENSOR (MI_SNR_*)                  AUDIO IN MIC (MI_AI_*)
   │                                  │
   ▼                                  ▼
VIF dev/chn (MI_VIF_SetDevAttr)     AENC G.711/G.726/AAC
   │ (BindChnPort2)                   │
   ▼                                  ▼
VPE chn  (Rotation, Crop, Mode)     RTP queue (audio)
   │   ├─► port0 ─► VENC H.264 (main_stream) ─► RTP queue (video) ─► RTSP/RTMP
   │   └─► port1 ─► VENC MJPEG  ─► ST_DoCaptureJPGProc ─► /customer/config/snap.jpg
   ▼
(VPE → VENC binds via MI_SYS_BindChnPort2)
```

Zentral: `MI_VENC_CreateChn` öffnet zwei Channels — einen für **H.264 Stream**
(`main_stream`-String im Binary), einen für **JPEG-Snap** (`MI_VENC_GetJpegParam`/
`SetJpegParam`). Beide Channels werden mit `MI_VENC_StartRecvPic` / `StopRecvPic`
**on-demand** ein- und ausgeschaltet → daher das **idle = blaues Bild** Verhalten
in HA (siehe `villa_gw_stream_ondemand`-Memory): solange kein Client an RTSP zieht,
ruht der Encoder, der Sensor liefert aber weiterhin Bilder an VPE/VIF.

Wichtige Log-Linien (in dieser Reihenfolge zu finden):

```
xxxxxxxxxxx........ST_Vpe_StartPort......%d  %d....
Vpe create port w %d h %d
ST_Venc_StartChannel
index %d, Crop(%d,%d,%d,%d), outputsize(%d,%d), maxfps %d, minfps %d, ResDesc %s
choice which resolution use, cnt %d / You select %d res
MI_SNR_GetCurRes error
video venc stream start ok
----- video shared packet: offset=%d, remaining=%d
video venc stream stop...
```

`MI_VENC_RequestIdr` wird benutzt, um **bei neuem RTSP-Client einen sofortigen
Keyframe** zu erzwingen — das ist der Grund, warum HA-Wiedergabe nach SETUP/PLAY
sofort ein Bild liefert (auch wenn der Encoder vorher schlief).

---

## 3. TCP 10600 — Stream-Control-Channel

```
echo server bind error
listen error
bind error
10600 tcp recv len:%d, data:%s
```

**`10600 tcp recv len:%d, data:%s`** ist der einzige Logbefehl auf diesem Listener;
das Format `len:%d, data:%s` und die Bezeichnung "echo server" zeigen, dass der
Server **AT-ähnliche, NULL-terminierte Klartext-Befehle** verarbeitet.

Die exakte Wortmenge ist aus den im Binary verbliebenen Konstanten ableitbar — es gibt
genau **5 unterstützte Befehle**:

```
AT+B MJPG Snap        ← Snapshot auslösen (siehe §6)
AT+B VIDEO START      ← H.264-Encoder aktivieren (StartRecvPic)
AT+B VIDEO STOP       ← H.264-Encoder deaktivieren (StopRecvPic)
AT+B RTSP start       ← RTSP-Server starten, falls noch nicht laufend
AT+B RTSP stop        ← RTSP-Server stoppen
```

**Wer ruft 10600 an?** → `avlink`. Indirekt nachgewiesen: in `avlink` finden sich
sowohl der String **`AT+B UART switchCamera`** als auch ein lokales `127.0.0.1`-
Connect, aber **keine** `AT+B MJPG Snap`/`AT+B VIDEO`/`AT+B RTSP`-Strings — d.h.
diese Worte werden **dynamisch** von avlink konstruiert und über TCP an
`127.0.0.1:10600` zu mimedia gesendet.

`tcp client to %s:%d, %d, %s` + `!!! connect %s error` (mimedia) und auf der
Gegenseite die HTTP-API-Endpunkte für Snapshot/Stream-Toggle:

```
REST /api/video/snap  → avlink → "AT+B MJPG Snap"            → mimedia:10600
REST /api/video/start → avlink → "AT+B VIDEO START"          → mimedia:10600
                              + DB-update video.enable=1
                              → custode2 sieht reload → media_reload()
```

**Anders gesagt:** TCP 10600 ist die **lokale Control-Plane** zwischen `avlink`
(Hauptdaemon auf 10086) und `mimedia` — ein eigenes, dünnes AT-Bus-Subprotokoll,
einseitig: nur Befehle, kein Streaming. Es ist **nicht** der RTSP/RTP-Pfad.

---

## 4. RTSP-Server (TCP 554)

Implementierung basiert auf `rtsp_demo.c` / `rtsp_msg.c` (klassisches Sigmastar-
Demo, von vielen IP-Cam-Vendoren benutzt — leicht erkennbar an den Symbolnamen).

### Funktionen (aus `.rodata`-Symbolnamen):

| Funktion | Aufgabe |
|---|---|
| `rtsp_new_demo` | RTSP-Listener auf Port 554 anlegen, accept-Loop starten |
| `rtsp_set_client_socket` | Client-FD in Session einhängen, Keepalive setzen |
| `rtsp_new_client_connection` / `rtsp_del_client_connection` | Verbindungs-Lifecycle |
| `rtsp_new_session` / `rtsp_del_session` | Logische RTSP-Session (CSeq+Session-ID) |
| `rtsp_set_video` / `rtsp_set_audio` | Codec-Attribute + SPS/PPS in Session laden |
| `rtsp_handle_OPTIONS` | "Public: OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN" |
| `rtsp_handle_DESCRIBE` | baut SDP (siehe unten), `Content-Type: application/sdp` |
| `rtsp_handle_SETUP` | Transport-Parsing (TCP-Interleaved **und** UDP), siehe unten |
| `rtsp_handle_PLAY` | `rtp_tx_data` aktivieren, sofort `MI_VENC_RequestIdr` |
| `rtsp_handle_PAUSE` | Senden anhalten, Encoder läuft weiter |
| `rtsp_handle_TEARDOWN` | Session schließen, ggf. Encoder stoppen |
| `rtsp_process_request` | Top-Level-Dispatcher: liest CSeq, ruft passenden Handler |
| `rtsp_recv_msg` / `rtsp_send_msg` | Wire-IO mit `rtsp_msg_parse_from_array` / `rtsp_msg_build_to_array` |
| `rtsp_do_event` | Event-Loop (vermutlich select-/epoll-basiert, mit RTCP-Receiver) |
| `rtsp_tx_video` / `rtsp_sever_tx_video` / `rtsp_tx_audio` | Daten aus Stream-Queue → RTP encoden → senden |

### SDP-Body (genau das, was im Binary steckt)

```
s=rtsp_demo
o=- 0 0 IN IP4 0.0.0.0
c=IN IP4 0.0.0.0
m=video 0 RTP/AVP %d
a=rtpmap:%d H264/%d
a=fmtp:%d packetization-mode=1;sprop-parameter-sets=…;sprop-sps=…;sprop-pps=…
m=audio 0 RTP/AVP %d
a=rtpmap:%d G726-%d/%d/1
a=fmtp:%d profile-level-id=1;mode=AAC-hbr;sizelength=13;indexlength=3;…;config=%02X%02X
```

→ unterstützte Video-Codecs: **H.264** und **H.265** (`rtsp_codec_data_parse_from_user_h264`/`_h265`,
   Encoder-Helper `rtp_enc_h264`, `rtp_enc_h265`). HA bekommt H.264 (`main_stream`).
→ Audio: **G.711, G.726, AAC** (`rtp_enc_g711`, `rtp_enc_g726`, `rtp_enc_aac`).

### Transport

Beide RTP-Modi sind implementiert:

| Mode | Logline |
|---|---|
| **RTP over TCP (interleaved)** | `[INFO] new rtp over tcp for %s ssrc:%08x peer_addr:%s interleaved:%u-%u` |
| **RTP over UDP** | `[INFO] new rtp over udp for %s ssrc:%08x local_port:%u-%u peer_addr:%s peer_port:%u-%u` |

UDP-Port-Pärchen werden über `__rtp_udp_local_setup` gebunden (zwei aufeinander­
folgende freie Ports → RTP + RTCP). Bei `error: not found free local port for rtp/rtcp`
schlägt SETUP fehl. HA via go2rtc/FFmpeg setzt `Transport: RTP/AVP/TCP;interleaved=0-1`
und nimmt damit den TCP-Pfad → keine UDP-Probleme im LAN/NAT.

### URL-Pfad / Authentifizierung

```
[DEBUG] add session path: %s
[WARN ] rtsp urlpath:%s err
[WARN ] path is not matched %s (old:%s)
[ERROR] path:%s (%s) is exist!!!
```

Der Pfad-Teil der RTSP-URL (`rtsp://gw:554/<path>`) wird gegen eine intern verwaltete
Session-Path-Tabelle gematcht. Der konkrete Pfad (z. B. `/live/stream` oder leer)
wird aus der **avl20.db** geladen — siehe §5.

**Auth:** das Binary parst `Authorization:`- und `User-Agent:`-Header
(`rtsp_msg_parse_user_agent`), aber es gibt **keinen** `WWW-Authenticate`-Build-String,
keinen `Unauthorized`-Response, keinen MD5/SHA Digest-Path im RTSP-Handler. Die
einzigen `Unauthorized`/Digest-Strings stammen aus dem RTMP-Adobe-Auth-Stack und sind
für den RTSP-Server tot. **Fazit: RTSP ist auf dem Villa GW vollständig anonym** —
wer auf Port 554 kommt, kommt rein. Schutz also nur per Netz-Isolation.

---

## 5. Konfig-Source: `avl20.db` `config.video`

`mimedia` liest **eine** Zeile aus SQLite:

```
SELECT item FROM config WHERE name = 'video';
UPDATE config SET item = '{"enable":%s,"rtsp":"%s","rtmp":"%s"}' WHERE name='video';
get video param: enable = %d, rtsp = %s, rtmp = %s
avlink.db video not config.....
```

Die JSON-Struktur:

| Key | Bedeutung |
|---|---|
| `enable` | "1"/"0" — globale Aktivierung; bei 0 startet weder RTSP- noch RTMP-Pfad |
| `rtsp` | RTSP-URL-Pfad oder vollständige URL (z. B. `rtsp://0.0.0.0:554/`) |
| `rtmp` | RTMP-Push-Ziel; leer = kein Push |
| `transfer` | (aus custode2 ergänzt) `2` = P2P-Server-Modus aktiv |
| `p2p_server` | Pfad zur `media-server`-Binary, wenn `transfer==2` |

`avlink` schreibt diese Zeile via REST (`/api/video/*`). Default-Demo-URL im Binary:

```
rtmp://192.168.1.163/live/stream
```

— **Vendor-Test-IP**, nichts Produktives. Wird zur Laufzeit überschrieben.

---

## 6. Snapshot-Subsystem (`AT+B MJPG Snap`)

Trigger-Pfad:

```
HTTP /api/video/snap (lua)
  └─► avlink (TCP 10086)
       └─► TCP-Send "AT+B MJPG Snap\r\n" → mimedia:10600
            └─► rtsp_/control thread liest 10600-Buffer, dispatch
                 └─► ST_DoCaptureJPGProc
                      ├─ MI_VENC_GetJpegParam (Q-Faktor, Bitrate)
                      ├─ MI_VENC_StartRecvPic (JPEG-Channel, einmaliger Frame)
                      ├─ MI_VENC_GetStream (mit kurzem select-Timeout)
                      ├─ write(/customer/config/snap.jpg, …)   ← atomarer overwrite
                      └─ MI_VENC_StopRecvPic
```

Zwischenstring **`jpG@jpG`** im `.rodata` deutet auf einen vendor-internen 8-Byte-
Marker im JPEG-Header hin (Eigenes Container-Format? Wahrscheinlich aber nur ein
Debug-Pattern — die Datei selbst ist normales JPEG, sonst würden Browser die nicht
anzeigen).

Single-Shot: keine MJPEG-Stream-Schleife. HA holt `snap.jpg` über `/api/video/snap`
und parsiert es als JPEG. Daher die **leichte Latenz** beim Snapshot-Request (~600-
800 ms): VENC-Channel wird startet, einen Frame ziehen, stoppen.

---

## 7. RTMP-Push (Cloud / "transfer=2")

Wenn `video.rtmp` und `video.enable` gesetzt sind, baut mimedia parallel zu RTSP
einen **outgoing RTMP-Connect** auf — d. h. der GW ist hier **Publisher**, nicht
Server. Logs:

```
rtmp url = %s
RTMP is ready %X, id = %d
rtmp server exit!!!
```

Stack: ganze libRTMP statisch eingebunden — alle Adobe-Handshake-Symbole
(`HandShake`, `RTMP_Connect0/1`, `RTMP_ClientPacket`, SecureToken, FCPublish/
NetStream.Publish.Start). Auch **Auth-Modi** vorhanden:

- `authmod=adobe` + `pubUser`/`pubPasswd` (`Publisher username/password`)
- `code=403 need auth`
- TLS via OpenSSL (RTMPS/RTMPTS)

Das ist der "Cloud-Push"-Pfad — falls der GW vom Vendor in eine RTMP-Cloud
hochstreamt. Auf einem reinen LAN-Setup (HA-Use-Case) ist `video.rtmp = ""` →
Pfad bleibt schlafend. `media-server`-Binary (separater Prozess, von custode2
gestartet) handhabt vermutlich **P2P-Tunneling** (start-stop-daemon -S <server>),
mimedia selbst macht RTMP direkt.

---

## 8. Stream-Trigger-Logik (Warum on-demand?)

**Antwort: hängt am RTSP-Client, nicht am Bus-Monitor.**

Beweisreihenfolge:

1. `AT+B VIDEO START` aktiviert den H.264-VENC-Channel über `MI_VENC_StartRecvPic`.
   Das ist die **harte** Bus-Steuerung — z. B. wenn die Outdoor-Station klingelt,
   schickt `avlink` (via SIP/INVITE) ein VIDEO START an mimedia, damit die
   Pipeline läuft, bevor PJSUA den Audio-Pfad öffnet.

2. **Ohne** VIDEO START läuft VENC nicht — `MI_VENC_GetStream` liefert dann
   `Not Enough Bandwidth`/Errors. Folge: ein RTSP-PLAY auf einen idle GW würde
   **nur SDP, aber keine Frames** liefern (genau das blaue Bild).

3. **Aber:** in `rtsp_handle_PLAY`/`rtsp_set_video` wird der Encoder gestartet
   und `MI_VENC_RequestIdr` aufgerufen — d.h. mimedia kann den Encoder auch
   **eigenständig** anwerfen, wenn ein RTSP-Client kommt. Das ist genau das,
   was HA/go2rtc auslöst.

4. Stop ist **nicht** automatisch (`TEARDOWN` schließt Session, aber `MI_VENC_StopRecvPic`
   wird nur explizit über `AT+B VIDEO STOP` getriggert — sonst läuft Encoder weiter,
   bis der Stream-Queue-Backpressure-Timer abläuft oder mimedia neu lädt).

Praktische Folge: wenn du HA-Camera-Stream öffnest, läuft VENC ab dem ersten PLAY.
Schließt du den Stream, kann VENC noch eine Weile weitercodieren, bis er idle-out
geht oder bis SIP-Callend ein `AT+B VIDEO STOP` schickt. Das passt zu dem im
Memory `villa_gw_stream_ondemand` festgehaltenen Verhalten.

---

## 9. IPC zu avlink / uart2d — wie läuft `AT+B UART switchCamera`?

mimedia hat **keinen** UART-Code (keine `/dev/tty*`-Strings, kein termios). Die
Kameraumschaltung läuft so:

```
HTTP /api/uart/switch_camera   (lua endpoint)
  └─► TCP → avlink:10086   "AT+B UART switchCamera"
       └─► avlink schreibt Bus-Frame an uart2d (TCP 10087)
            └─► uart2d sendet AT-Frame über /dev/ttyS1 an Outdoor-Station
                 └─► Station antwortet "OK\r\n"
       ◄─ avlink notifies mimedia über TCP 10600?  → NEIN, nicht nötig:
          die Outdoor-Cam ist eine separate Hardware-Kamera; mimedia auf dem
          GW liefert dagegen den lokal angeschlossenen Sensor.
```

`switchCamera` wechselt also nicht den lokalen Sensor in mimedia, sondern den
**aktiv anzuzeigenden Kamera-Stream** auf der **Outdoor-Station** — ein Bus-
Command, das mimedia gar nichts angeht. Daher der zentrale Hinweis im Memory
`project_villa_gw_ttys1_readers_break_commands`: parallel laufende `cat /dev/ttyS1`-
Reader klauen die Antwort, **avlink** kriegt `response=err`, der HA-Wake-Befehl
schlägt fehl — aber **mimedia** läuft davon unbeeindruckt weiter.

`avlink.db video not config.....` (Tippfehler im Original) wird beim Boot
geloggt, falls die `video`-Zeile fehlt — mimedia exit-t dann nicht, sondern
wartet auf `AT+B RTSP start` oder DB-Update.

---

## 10. avio-server vs. media-server (Zusammenfassung)

| | **avio-server** | **media-server** |
|---|---|---|
| Wer startet | `mimedia master` (fork) | `custode2.lua` via `start-stop-daemon -S <p2p_server> -x media-server` (on demand) |
| Aufgabe | Lokale A/V-Aufnahme + Encode + RTSP-Server (Port 554) + 10600-Control + Snapshot | P2P-Tunnel zum Vendor-Cloud-Backend (Reverse-NAT-Relay) |
| Trigger | Immer (Master-Watchdog) | Nur wenn `video.transfer == 2` UND `video.p2p_server` gesetzt |
| Netzwerk | listen 554, listen 10600, optional outbound RTMP | outbound TCP zu `video.p2p_server` |
| Im LAN-HA-Setup | **Pflichtprozess** | **Deaktiviert** (transfer != 2) |

**Wichtig:** Beide laufen aus **demselben Binary** `mimedia` — der Master entscheidet
über `argv[0]` bzw. einen internen Mode-Switch (master vs. avio-server vs. media-
server), wie der typischen Sigmastar-Sample-Architektur entsprechend. Im Custode-
Aufruf `mimedia master &` ist `master` das `argv[1]`-Modeflag.

---

## 11. Hidden Debug & Signal-Handling

### Logging
Die `rtsp_demo`-Bibliothek hat **eingebaute Loglevel-Strings**:

```
[DEBUG %s:%d:%s]   ← function, line, file
[INFO  %s:%d:%s]
[WARN  %s:%d:%s]
[ERROR %s:%d:%s]
```

Diese werden alle in stdout/stderr gepiped — auf dem GW landet das in `usr-log.log`
über die übliche `> /var/log/…`-Umlenkung im Init-Script. Keine separate Logdatei.
Kein Env-Var `LOG_LEVEL` (lässt sich aus dem Binary nicht ableiten — alle Level
hart gleichberechtigt gebaut). Ein versteckter Verbose-Switch wäre allenfalls
ein `argv`-Mode, aber `compile_options` als Symbolname deutet eher auf SQLite-
intern (`PRAGMA compile_options`).

### Signal-Handler
```
Catch signal!!!
sig exit pid = %d children[%d].pid = %d, children[%d].status = %d
!!! children[%d]: pid = %d, exit status = %d
```

Master fängt **SIGCHLD** (children exit → respawn) und vermutlich **SIGTERM/SIGINT**
für sauberes Shutdown (`MI_SYS_Exit`, `ST_Sys_Exit`, `ST_StopPipeLine`). Workers
selbst ignorieren `SIGPIPE` (würde Socket-Writes killen) — Standardpattern.

### Speicher-Hack
```
echo 3 > /proc/sys/vm/drop_caches
```

mimedia ruft tatsächlich `system("echo 3 > /proc/sys/vm/drop_caches")` auf, um
Page-Cache zu droppen — vermutlich bei VENC-Restart, um Fragmentation in der
Sigmastar-MMA-Heap zu vermeiden. Ein typischer Hack auf Low-Mem-Boards.

---

## 12. Stream-Queue (vendor `stream_queue.c`)

Zwischen VENC-Output und RTSP-/RTMP-Sender liegt ein ringbuffer:

```
stream_queue.c
streamq_alloc
[ERROR] alloc memory failed for stream_queue
[ERROR] alloc memory for video rtp queue failed
[ERROR] alloc memory for audio rtp queue failed
----- video shared packet: offset=%d, remaining=%d
```

H.264-NALs werden in **shared packets** abgelegt — Multi-Subscriber-Pattern: ein
Frame kann gleichzeitig an mehrere RTSP-Clients **und** den RTMP-Pusher
gesendet werden, ohne Mehrfach-Encoding. Daher auch der "shared packet" Log mit
`offset`/`remaining` pro Consumer.

`[WARN] client %s will lost audio packet` ist die typische Backpressure-Drop-
Logline für lahme Audio-Clients.

---

## 13. Quick-Reference: Was auf welchem Port?

| Port | Listener | Inhalt | Auth |
|---|---|---|---|
| **554/tcp** | mimedia → avio-server / rtsp_demo | RTSP (OPTIONS/DESCRIBE/SETUP/PLAY/PAUSE/TEARDOWN), RTP-over-TCP + RTP-over-UDP, H.264/H.265 + G.711/G.726/AAC | **Keine** |
| **10600/tcp** | mimedia → control-thread | AT+B-Subset (`MJPG Snap`, `VIDEO START/STOP`, `RTSP start/stop`) — von `avlink` (lokal) | Keine (nur 127.0.0.1) |
| outbound 1935/tcp | mimedia → libRTMP | RTMP-Publish zur Vendor-Cloud, optional | adobe pubUser/pubPasswd |
| outbound `video.p2p_server` | media-server (separat) | P2P-Tunnel, wenn `transfer==2` | vendor-proprietär |

---

## 14. Take-aways für Home-Assistant-Integration

1. **HA muss nur RTSP `rtsp://<gw>:554/<path>` ansprechen** — Pfad steht in
   `avl20.db` `config.video.rtsp`. Bei leerem Pfad: `rtsp://<gw>:554/`.
2. **Auth ist None** — der GW ist nur durch LAN-Isolation geschützt; nicht ins
   IoT-VLAN ohne Firewall.
3. **On-demand-Encoder**: erstes PLAY triggert IDR + Encoder-Start; HA via go2rtc
   sieht das blaue Bild nur, wenn der Stream noch nie aktiviert wurde — sobald
   ein PLAY ankommt, läuft VENC.
4. **Snapshot ist HTTP, nicht RTSP**: `/api/video/snap` → mimedia produziert
   `/customer/config/snap.jpg` — HA `still_image_url` zeigt darauf.
5. **TCP 10600 nicht von HA direkt ansprechen** — gehört avlink. Geht über
   `AT+B …` an 10086 (avlink) → avlink relayt an 10600.
6. **media-server (P2P)** wird auf dem Standard-LAN-Setup **nie** gestartet —
   nur wenn der Owner vorher Vendor-Cloud aktiviert hatte.
7. **RTSP per TCP-Interleaved** ist robuster als UDP (NAT, Paketverluste); go2rtc
   default `protocols=tcp` ist hier richtig.

---

## Anhang: vollständige Liste der `rtsp_*`-Symbole (zur Disassembler-Navigation)

```
rtsp_new_demo                       rtsp_msg_transport_s
rtsp_set_client_socket              rtsp_msg_cseq_s
rtsp_new_client_connection          rtsp_msg_session_s
rtsp_del_client_connection          rtsp_msg_content_length_s
rtsp_new_session                    rtsp_msg_server_s
rtsp_del_session                    rtsp_msg_user_agent_s
rtsp_set_video                      rtsp_msg_date_s
rtsp_set_audio                      rtsp_msg_content_type_s
rtsp_handle_OPTIONS                 rtsp_msg_public_s
rtsp_handle_DESCRIBE                rtsp_msg_accept_s
rtsp_handle_SETUP                   rtsp_msg_parse_uri
rtsp_handle_PLAY                    rtsp_msg_parse_startline
rtsp_handle_PAUSE                   rtsp_msg_parse_transport
rtsp_handle_TEARDOWN                rtsp_msg_parse_cseq
rtsp_process_request                rtsp_msg_parse_session
rtsp_recv_msg                       rtsp_msg_parse_content_length
rtsp_send_msg                       rtsp_msg_parse_user_agent
rtsp_recv_rtp_over_udp              rtsp_msg_parse_public_
rtsp_recv_rtcp_over_udp             rtsp_msg_parse_accept
rtsp_do_event                       rtsp_msg_parse_from_array
rtsp_tx_video                       rtsp_msg_build_to_array
rtsp_sever_tx_video                 rtsp_msg_frame_size
rtsp_tx_audio                       rtsp_codec_data_parse_from_user_h264
rtsp_new_rtp_connection             rtsp_codec_data_parse_from_user_h265
__rtp_udp_local_setup               rtsp_codec_data_parse_from_user_g726
rtp_tx_data                         rtsp_codec_data_parse_from_user_aac
                                    rtsp_codec_data_parse_from_frame_aac
                                    rtsp_codec_data_parse_from_frame_h264
                                    rtsp_codec_data_parse_from_frame_h265
```

(typo `parse_public_` mit Trailing-Underscore und `rtsp_sever_tx_video` sind im
Binary so vorhanden — Vendor-typos, nicht meine.)
