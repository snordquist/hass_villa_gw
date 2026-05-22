# Villa-Bus Raw-Protokoll — Reverse-Engineering-Notizen

**Letzter Stand:** 2026-05-22 (FW-Version **4.1.12**).

**Quellen:**
1. Raw-Mitschnitt von `/dev/ttyS1` (1070 Bytes, 73 Frames) — siehe original-Quelle unten.
2. Static-Reverse-Engineering von `uart2d` ARM-Binary (`villa_gw/firmware/4.1.12/update/app/sbin/uart2d`,
   md5 `d9bffb8b407e4452e4248bee9aa93db9`) — Log-Format-Strings und Funktions-Symbole.
3. Static-Reverse-Engineering von `avlink` 4.1.12 ARM-Binary (md5 `5bd7318ee3b246c5f78dd83075031930`).

**Wichtig zur FW-Version:** `uart2d` ist zwischen 4.1.11 und 4.1.12 **bit-identisch**. Die Bus-Logik wurde
in 4.1.12 nicht angefasst — alle Erkenntnisse aus dem 4.1.11-Capture und der 4.1.11-Binary gelten
unverändert weiter. `avlink` 4.1.12 hat nur Mutex- und Logging-Fixes (`pthread_mutex_lock` →
`_trylock`, neue `stderr`-Logging) — keine neuen Bus-Commands.

**Quelle 1 — Wire-Capture (Bezug):** `/tmp/bus_sniff.bin` (1070 Bytes, vom User über `cat /dev/ttyS1 > /tmp/bus_sniff.bin` aufgezeichnet am 2026-05-21). Lokale Kopien: `/tmp/bus.sample.bin`, `/tmp/bus.sample.hex`, `/tmp/frames_annotated.txt`.

**UART-Setting:** 9600 baud (per `stty -F /dev/ttyS1`); termios-Setup via `cfsetispeed`/`cfsetospeed`/`tcsetattr` in `uart2d` bestätigt (Baudrate-Konstante als integer im Code, nicht als String).

> **Wichtig zur Capture:** Während der Aufzeichnung liefen drei parallele `cat /dev/ttyS1`-Prozesse. Da das Kernel den seriellen Stream tty-internal zwischen Lesern aufteilt, sind die analysierten Frames **eine Teilmenge** des tatsächlichen Bus-Verkehrs. Plus: `cat /dev/ttyS1` zeigt nur RX-Frames vom Bus, **nicht TX-Frames** die `uart2d` schreibt. Für vollständige TX/RX-Aufzeichnung müsste ein LD_PRELOAD-Shim auf `uart2d`s `read`/`write`-Syscalls greifen — siehe [`reverse-engineering/live_forensics.md`](reverse-engineering/live_forensics.md).

---

## Zusammenfassung

- **Frame-Sync gefunden:** Jedes Frame beginnt mit `55 5F`.
- **Frame-Struktur entschlüsselt:** 10-Byte-Header + variabler Payload + 1-Byte-Prüfsumme.
- **Prüfsumme bestätigt:** `sum(alle_bytes_außer_letzten) mod 256 == letztes_byte` — **73/73 Frames** (100 %).
- **Adressierung bestätigt:** Outdoor=1, GW=2 stimmt mit den bekannten Notes überein.
- **Polling sichtbar:** Der Bus war während der Aufzeichnung **im Idle/Polling-Modus** — kein Klingel-, Monitor- oder Unlock-Event sichtbar.

---

## Frame-Layout (bestätigt durch Capture + uart2d-Binary)

```
Offset  Bytes  Feld           Beispiel    Bedeutung
------  -----  ----------     ---------   ----------------------------------------------------
  0..1    2    SYNC           55 5F       Frame-Start-Magic, in 73/73 Frames vorhanden
  2       1    LEN            02          Payload-Länge in Bytes (frame_len = 11 + LEN)
  3       1    CMD            0A          Befehls-Code; high-bit (0x80) = Response-Flag
  4..6    3    FROM           00 01 01    3-Byte-Adresse der Quelle (siehe Adressierung unten)
  7..9    3    TO             00 02 01    3-Byte-Adresse des Ziels
 10       1    SEQ            13          Sequenz-/Transaktions-ID, zwischen Req+Resp geteilt
 11..    LEN-1 PAYLOAD        20 15       Befehlsspezifische Daten (LEN-1 Bytes)
 10+LEN   1    CHK            0D          Prüfsumme = sum(bytes[0..10+LEN-1]) mod 256
```

**Gesamt-Frame-Länge:** `11 + LEN` Bytes. Beobachtete Werte: 12, 13, 14, 16, 17 Bytes (LEN ∈ {1,2,3,5,6}).

**Confidence: HOCH** — bestätigt durch:
1. 100 % Prüfsummen-Match über alle 73 Frames im Wire-Capture.
2. Direkter Beleg im uart2d-Binary (4.1.12) durch das eingebettete Logging-Format-String, das genau dieses Layout ausdruckt:

   ```
     cmd  : 0x%02x
     len  : 0x%02x
     from : 0x%02x 0x%02x 0x%02x       ← 3 Bytes
     to   : 0x%02x 0x%02x 0x%02x       ← 3 Bytes
     data%d: 0x%02x                    ← Payload-Bytes nummeriert
   ```

   Damit ist die Felder-Grenze in den 6 Bytes zwischen Offset 4 und 9 **eindeutig 3+3 als zwei zusammengehörige Adress-Tripel**, nicht (RSVD+ADDR+SUB)×2 wie vorher angenommen.

### Adressierung — 3-Byte hierarchisch

Jedes `from`/`to` ist ein 3-Byte-Identifier. Aus den beobachteten Werten (`00 01 01` Outdoor, `00 02 01` GW) ist die plausibelste Interpretation:

| Byte | Vermutete Semantik | Wert in der Probe |
|------|---------------------|-------------------|
| `byte[0]` | **Domain / Master-Sektor** | immer `0x00` (1× Bus-Master in diesem Setup) |
| `byte[1]` | **Node-Adresse** (= `callList.key` in der DB) | `1` = Outdoor, `2` = GW, `0x63` = ??? (Discovery?) |
| `byte[2]` | **Sub-Channel / Port** | immer `0x01` (1 Channel/Tür pro Node hier) |

In Mehrfamilien-Setups (mehrere Häuser am gleichen Bus) ist `byte[0]` vermutlich nicht-Null; in der typischen Einfamilienhaus-Konfiguration ist es immer `0x00`. Confidence: **mittel** — die Drei-Byte-Form ist hart belegt, die Semantik der drei Bytes ist plausibel aber nicht durch Multi-Site-Capture verifiziert.

### Hinweis zur SEQ-Position
Die SEQ-Annahme basiert darauf, dass byte[10] in Request/Response-Paaren identisch ist (Beispiel: Frames `[2]` und `[3]` haben beide `seq=0x13`, ebenso `[4]/[5]=0x14`). Damit ist byte[10] formal das erste Payload-Byte mit semantischer Sonderrolle "Sequence/Transaction-ID" — alternativ kann man es als 1. Payload-Byte und nicht als Header-Feld interpretieren. Funktional unverändert.

---

## Häufigkeitsanalyse

### Frame-Längen-Histogramm

| LEN-Byte | Frame-Länge | Anzahl |
|---:|---:|---:|
| 0x00 | 12 | 9  |
| 0x01 | 13 | 9  |
| 0x02 | 14 | 28 |
| 0x04 | 16 | 13 |
| 0x05 | 17 | 14 |

### CMD-Byte-Verteilung pro Richtung

| Richtung | CMD | Anzahl | Vermutung |
|---|---|---:|---|
| o→g | 0x0A | 9  | Outdoor sendet Status/Heartbeat (Request) |
| o→g | 0x8A | 10 | Outdoor sendet 0x8A — evtl. Reply auf vorher gesendetes 0x0A |
| o→g | 0x01 | 10 | Outdoor sendet 17-Byte Request (Status mit Zeit/Version?) |
| o→g | 0x82 | 17 | Outdoor sendet 0x82 — variable Länge (12 oder 14) |
| o→g | 0x0C | 2  | Spezielles SRC=0x63! Vermutung: andere Innenstation/Teilnehmer |
| o→g | 0x81 | 3  | Outdoor sendet 0x81 — Reply-Form |
| g→o | 0x01 | 2  | GW sendet 17-Byte Request |
| g→o | 0x02 | 9  | GW sendet 13-Byte Request |
| g→o | 0x81 | 10 | GW sendet 16-Byte Reply (paired mit o→g 0x01) |
| g→o | 0x8a | 1  | GW sendet einmal 0x8A |

**Beobachtung:** CMD-Bytes mit gesetztem high-bit (0x81, 0x82, 0x8A) korrelieren in 21/22 Fällen mit unmittelbar vorhergehendem Frame in Gegenrichtung mit gleichem low-7-bit CMD und gleicher SEQ. Das **bestätigt die Request/Response-Konvention**: `cmd | 0x80 == reply_cmd`.

### Periodisches Polling
SEQ-Werte 0x13 → 0x1F erscheinen monoton aufsteigend in Schritten von 1 (mit unregelmäßigen Einschüben fremder SEQ-Werte 0x65, 0xD9, 0xF0, 0xE6, 0xC0 etc.). Interpretation:
- Es gibt **einen periodischen Poll-Counter** (vermutlich GW pollt Outdoor jede Sekunde).
- Daneben gibt es **on-demand Transaktionen** mit eigener SEQ (vermutlich aus Cloud-/REST-API-Befehlen).

---

## Beispiel-Frames (annotiert)

### Periodische Status-Pakete (CMD 0x0A / 0x01)

```
[2] o->g  CMD=0x0A LEN=14 SEQ=0x13  payload=13 20 15        full=555f020a0001010002011320150d
[3] o->g  CMD=0x8A LEN=14 SEQ=0x13  payload=13 21 15        full=555f028a0001010002011321158e
[4] o->g  CMD=0x01 LEN=17 SEQ=0x14  payload=14 20 15 01 1e 11   full=555f0501000101000201142015011e1138
[5] g->o  CMD=0x81 LEN=16 SEQ=0x14  payload=14 21 01 1e 11      full=555f04810002010001011421011e11a3
```

**Beobachtungen zum Payload:**
- Byte 1 des Payload variiert zwischen `0x20` und `0x21` zwischen Request/Reply → könnte ein "Direction-Flag" o.Ä. sein.
- Die Trailer-Bytes `01 1E 11` bzw. `1E 11` tauchen in **vielen** Status-Frames konstant auf → **vermutlich Firmware-/Protokoll-Version oder Capability-Maske** (`1E 11` als Konstante in fast jedem o→g cmd=0x01 Frame).
- Byte 0 des Payload (gleich SEQ-Byte) wiederholt sich; das suggeriert, dass die "SEQ" eigentlich Teil des Payloads ist und nicht des Headers.

### Spezial-Adresse 0x63 (=99)

```
[6] o->g  CMD=0x0C LEN=17 SEQ=0x15  full=555f050c0001630002011520f100000052
```

Hier ist `byte[5..6] = 01 63`, d.h. die Quelle ist `(1, 0x63)`. Vermutung: **dritte Bus-Teilnehmer** (z.B. ein zweites Outdoor-Modul, eine Kamera oder Audio-Untermodul); die `0x63`-Subadresse ist auffällig (`99`). Tritt 2× in der Probe auf — möglicherweise zyklische Identifikation.

### Variabel-längige 0x82 Frames

```
[8] o->g  CMD=0x82 LEN=12 SEQ=0x65  payload=65               full=555f008200010100020165a0
[9] o->g  CMD=0x82 LEN=14 SEQ=0x65  payload=65 00 10         full=555f0282000101000201650010b2
```

Beide Frames teilen sich SEQ 0x65 → vermutlich **Multi-Part-Response** oder zwei verschiedene Reply-Typen auf dieselbe Anfrage. Confidence: mittel.

### Einzelner Outlier mit "lesbarer" Datenfolge

```
[?] g->o  CMD=0x01 LEN=17 SEQ=0xD1  payload=d1 00 15 03 78 11   full=555f0501000201000101d10015037811bc
```

Die Bytes `15 03 78` sehen wie ein BCD-Datums-/Zeitfeld aus (z.B. Stunde=15, Minute=03, Sekunde=78 — letzteres nicht plausibel, also wohl was anderes). Confidence: spekulativ.

---

## Was NICHT im Mitschnitt war

Folgende Events, die im Cloud-MQTT-/REST-Pfad bekannt sind, **fehlen** in dieser Aufzeichnung:

- **Klingel-Event** (`call_btn_trigger`): kein offensichtliches Burst-Frame, das eine Klingel signalisieren würde.
- **Monitor-Anfrage** (`on_receive_monitor: state=1, ...`): nicht beobachtbar.
- **Unlock-Aktion** (`elock.lua`): nicht beobachtbar.

Der Bus war während der ~Minute Aufzeichnung **im reinen Polling-Modus**.

Außerdem zeigt eine `cat /dev/ttyS1`-Aufzeichnung **nur RX**, nicht TX — Frames die `uart2d` *schreibt* (= alle Action-Frames Richtung Outdoor) sind grundsätzlich unsichtbar. Für TX-Capture braucht es einen LD_PRELOAD-Shim auf `uart2d`s `read`/`write`-Syscalls (siehe [`reverse-engineering/live_forensics.md`](reverse-engineering/live_forensics.md)).

---

## Aus dem uart2d-Binary (Stand 4.1.12) — interne API

`villa_gw/firmware/4.1.12/update/app/sbin/uart2d` ist gestrippt, aber die Logging-Format-Strings im
`.rodata` verraten die komplette Frame-Builder-API. Diese Funktionen sind im Binary nachweisbar:

### Frame-Builder (UART, RS-485)

| Symbol | Log-Strings | Bus-Effekt |
|---|---|---|
| `userial_send_call_msg` | `userial_send_call_msg:src_addr = %d, dst_addr = %d` | Klingel-/Outgoing-Call zur Außenstation |
| `userial_send_call_resp` | `userial_send_call_resp` | Antwort auf eingehende Anrufmeldung |
| `userial_send_monitor_call_msg` | `userial_send_monitor_call_msg:src_addr = %d, dst_addr = %d` | Silent Live-View anfordern |
| `userial_send_hook_msg` | `userial_send_hook_msg:addr %d` | Off-Hook / Annehmen |
| `userial_send_hang_msg` | `userial_send_hang_msg:addr %d` und `userial_send_hang_msg:addr %d,%d` | Auflegen (1- oder 2-arg-Form, vermutlich addr+state) |
| `userial_send_intercom_call_msg` | `userial_send_intercom_call_msg:src_addr = %d, dst_addr = %d` | Intercom Indoor↔Indoor |
| `userial_send_req_unlock_msg` | `userial_send_req_unlock_msg:src_addr = %d, dst_addr = %d` | Türöffner-Relay-Trigger |
| `userial_send_cmd_01` | `userial_send_cmd_01:src_addr = %d, dst_addr = %d` | Status-Request CMD 0x01 (1-Hz-Polling-Pfad) |

### Frame-Builder (IP, parallele Logik für IP-Außenstationen)

Identische Set, nur Transport ist TCP statt UART:

```
utcp_send_call_msg
utcp_send_call_intercom_msg
utcp_send_call_hook_intercom_rsp_msg
utcp_send_unlock_msg
utcp_send_hang_msg
utcp_send_monitor_msg
utcp_send_ringback_intercom_rsp_msg
userial_ip_send_cmd_01
```

`uart2d` entscheidet bei jedem Bus-Befehl auf Basis der DB-Spalte `callList.callType` (0=Bus/UART, 1=IP/TCP), welche Variante er aufruft. Beide produzieren das gleiche logische Bus-Frame, nur das physische Transport-Medium ist verschieden.

### Bestätigte hardcoded CMD-Werte

Aus statischen Log-Strings im Binary:

```
" 111 send 0x0a from 0x01 to 0x01"
" 222 send 0x0a from 0x01 to 0x01"
" 0x0a failed"
```

→ **CMD `0x0a` ist explizit hartcodiert** für den Heartbeat-Pfad. Die `from 0x01 to 0x01` im Log-Text legen einen Default-Wert für den Heartbeat-Trigger an (vermutlich Outdoor self-loop bei Heartbeat-Generierung).

**Action-CMDs (`call`, `monitor`, `unlock`, `hook`, `hang`, `intercom`, `switchCamera`, `ringback`) haben KEIN literal-String-Vorkommen ihres CMD-Werts** im Binary — die Codes sind als integer-Immediates in den Frame-Build-Funktionen und nicht ohne Disassembly extrahierbar. Belegte Pfade existieren (siehe Frame-Builder-Tabelle), aber das exakte CMD-Byte muss per Live-TX-Capture (LD_PRELOAD-Shim) oder ARM-Disassembly bestätigt werden.

### State-Machine / Timer

Im Binary belegt sind separate Timer-Callbacks pro Bus-Aktion (jeder mit eigenem Timeout-Pfad):

| Callback | Trigger |
|---|---|
| `call_timeout_callback` | Outgoing Call timeout |
| `monitor_timeout_callback` | Monitor-Session läuft ab (`monitor timeout addr %d`) |
| `intercom_call_timeout_callback` | Intercom timeout |
| `intercom_ringback_timeout_callback` | Ringback während Intercom |
| `timer_callback` | Generic timer |

18 verschiedene `hang N`-Log-Stellen (N = 1..18, plus `3.1`) belegen 18+ State-Machine-Branches im Auflege-Pfad — das deutet auf eine komplexe Cleanup-Logik bei verschiedenen Call-States hin.

### Monitor-Frame-Internals

Aus Log-Strings extrahiert:

| Log-String | Bedeutung |
|---|---|
| `parse monitor param = %c, times = %d` | Monitor nimmt zwei interne Parameter: 1-Byte char + integer (vermutlich Wiederholungen/Dauer) |
| `monitor update camera 0x%.2x, id:%d, max:%d, times = %d` | Camera-Switch innerhalb einer Monitor-Session: cam_byte, id, max-Wert, times |
| `talk cam switch to [0x%02x] src [0x%02x]` | Kamera-Wechsel im laufenden Talk: src + target je 1 Byte |
| `monitor %d cam switch NEXT to %d` / `UP to %d` | Cam-Navigation per Richtung |
| `monitor %d switch NEXT door %d` / `UP door %d` | Door-Navigation (für Setups mit mehreren Türen) |
| `monitor addr %d has been used , form %d!!!!!` | Parallel-Monitor-Kollision auf gleicher addr |
| `monitor request channel error` | Channel-Konzept im Monitor-Request (ein Frame-Feld definiert vermutlich den Audio/Video-Kanal) |
| `monitor start .... form %d to %d` / `monitor finish ####` | Start/End-Marker im Log |

### TCP-Bridge uart2d ↔ avlink

`uart2d` ist nicht nur Server (Port 10087, akzeptiert AT+B-Strings), sondern auch **Client zu avlink:10086**:

```
tcp recv from 10086 len:%d, data:%s
tcp send to 10086 :%s
```

Vermutlich pusht `uart2d` Status-/ACK-Updates an `avlink` zurück (`response=ok/err/finish` für Bus-Aktionen). Das erklärt die `AT_UART_MONITOR response=%s`-Logs in `avlink-server.c`.

---

## AT+B-Befehlsformate (avlink-Forwarding zu uart2d:10087)

Aus dem `avlink` 4.1.12-Binary extrahiert — exakte printf-Templates, die `avlink` über TCP zu `uart2d:10087` schickt:

```
AT+B UART call %d %d           ← key_index, outdoor_addr
AT+B UART monitor %d %s %d     ← state, addr-string, duration
AT+B UART hook %d              ← call_id
AT+B UART hang %d              ← call_id
AT+B UART unlock %d            ← relay_index
AT+B UART intercom %d %d       ← from_addr, to_addr
AT+B UART switchCamera         ← (keine args)
AT+B UART ringback %s          ← state-string
AT+B MJPG Snap                 ← Snapshot
AT+B VIDEO START               ← Encoder-Lifecycle
AT+B VIDEO STOP

# Avlink-intern (nicht für externe Clients):
AT+B check ip
AT+B CHECKSIP 3 %d
AT+B RECORD %d
AT+B RELOAD callList
AT+B RELOAD mqtt
AT+B UPGRADE %d
```

`avlink` selbst loggt diese auf der RX-Seite mit normalisierten `AT_*`-Tags:

```
AT_UART                                                 ← Generischer UART-Receiver
AT_UART status=%d, uart_ret=%d, match_args_uart=%s      ← Parser-Result
AT_UART_CALL key_index=%d, outdoor_addr=%d
AT_UART_HANG state=%d self->key_index=%d
AT_UART_HOOK response=%s
AT_UART_INTERCOM from=%d, to=%d
AT_UART_MONITOR response=%s
AT_UART_RINGBACK state=%d, response=%s
AT_UART_UNLOCK response=%s
AT_CHECKSIP=%s
AT_MONITOR=%s
AT_MUSIC=%s
AT_UPGRADE=%s
AT_WIFI=%s
```

→ Das bestätigt: das Format `AT_UART_<CMD> response=<status>` ist der **stabile Log-Stream** für jeden Bus-Befehl. Ein Companion-Daemon, der `/customer/share/usr-log.log` tailt, sieht jeden Bus-Befehl mit `response=ok|err|finish` exakt im obigen Format.

---

## Empfehlung für nächste Schritte

1. **LD_PRELOAD-Shim auf `uart2d`** (höchste Priorität für die Doku-Vervollständigung) — hookt `read`/`write` auf FD `/dev/ttyS1` und mirrort jeden Frame mit Timestamp + Direction in eine separate Datei. Damit:
   - Action-CMD-Bytes (call/monitor/hook/hang/unlock/intercom/switchCamera/ringback) werden eindeutig identifiziert.
   - Sowohl TX- als auch RX-Frames werden sichtbar (nicht nur RX wie bei `cat /dev/ttyS1`).
   - `uart2d` läuft normal weiter, keine Bus-Funktion bricht.
2. **Andere `cat /dev/ttyS1`-Prozesse stoppen** (auf alle Fälle, falls vorhanden) damit `uart2d` saubere ACK-RX bekommt — siehe Memory `villa-gw-ttys1-readers-break-commands`.
3. **Aktiv triggern während LD_PRELOAD-Capture läuft:**
   - Klingelknopf an der Outdoor-Station drücken → Frame-Burst um `call_btn_trigger` einfangen.
   - Aus der App/HA eine Monitor-Session öffnen → Frame-Sequenz um Stream-Aktivierung.
   - Aus der App "Tür entriegeln" → Unlock-Befehlssequenz.
4. **Längere Aufzeichnung** (mehrere Minuten Idle) um sicher zu sein, dass die `1E 11`-Konstante und die `0x63`-Subadresse tatsächlich stabil sind.
5. **ARM-Disassembly mit Ghidra/radare2** auf den Frame-Builder-Funktionen (`userial_send_call_msg` etc.) — die jeweilige Funktion lässt sich über den `.rodata`-Xref auf den Log-Format-String lokalisieren; dort dann nach `mov rN, #imm`-Patterns suchen, die das CMD-Byte vor dem Frame-Build laden.
6. **Parser-Skript schreiben** (Python): nimmt `bus.bin`, splittet an `55 5F`, validiert Checksum, kategorisiert nach (CMD, Richtung), gibt Diff zwischen aufeinanderfolgenden Captures aus.

---

## Anhang: vollständige Frame-Liste

Vollständige geparste Liste aller 73 Frames in `/tmp/frames_annotated.txt`. Format:

```
Idx  Dir   Cmd   Len  Seq   Payload                Full-Hex
---  ---   ---   ---  ---   ---------------------  ----
  0  o->g  0x8a   14  0x21  210103                 555f028a0001010002012101036a
  ...
```

## Anhang: Verifikations-Skript

```python
import sys
data = open(sys.argv[1], 'rb').read()
SYNC = b'\x55\x5f'
positions = []
i = 0
while True:
    p = data.find(SYNC, i)
    if p < 0: break
    positions.append(p); i = p + 1

for k in range(len(positions) - 1):
    f = data[positions[k]:positions[k+1]]
    if len(f) < 12: continue
    payload_len = f[2]
    expected_len = 11 + payload_len
    cmd  = f[3]
    # 3-byte hierarchical addresses (per uart2d-internal log format)
    src_addr = (f[4], f[5], f[6])   # (domain, node, sub)
    dst_addr = (f[7], f[8], f[9])
    seq = f[10]
    chk = f[-1]
    chk_ok = (sum(f[:-1]) & 0xff) == chk
    print(f"len_ok={len(f)==expected_len} chk_ok={chk_ok} "
          f"from={src_addr[0]:02x}:{src_addr[1]:02x}:{src_addr[2]:02x} "
          f"to={dst_addr[0]:02x}:{dst_addr[1]:02x}:{dst_addr[2]:02x} "
          f"cmd=0x{cmd:02x} seq=0x{seq:02x} payload={f[11:-1].hex()}")
```

## Anhang: LD_PRELOAD-Shim für TX-Capture (Skizze)

Da `cat /dev/ttyS1` nur RX-Bytes liefert, ist für TX-Capture ein shim-basierter Ansatz nötig. Skizze für ARM-Cross-Compile (zielt auf den Live-GW, nicht macOS):

```c
/* uart2d_shim.c — ARM ELF, LD_PRELOAD vor uart2d
 * cross-compile: arm-linux-gnueabihf-gcc -shared -fPIC -o uart_shim.so uart2d_shim.c -ldl
 * deploy:        scp uart_shim.so root@gw:/customer/; auf GW:
 *                killall uart2d; LD_PRELOAD=/customer/uart_shim.so uart2d uart2d &
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <unistd.h>
#include <fcntl.h>
#include <string.h>
#include <stdio.h>
#include <sys/stat.h>

static int  log_fd       = -1;
static int  ttys1_fd     = -1;
static ssize_t (*real_read)(int, void *, size_t)        = NULL;
static ssize_t (*real_write)(int, const void *, size_t) = NULL;
static int     (*real_open)(const char *, int, ...)     = NULL;

static void ensure_log(void) {
    if (log_fd < 0) {
        log_fd = open("/tmp/bus_shim.bin", O_WRONLY | O_CREAT | O_APPEND, 0644);
    }
}

static void log_frame(char dir, const void *buf, size_t len) {
    if (log_fd < 0) ensure_log();
    if (log_fd < 0) return;
    /* simple framing: 1 byte dir ('R'/'W'), 2 bytes BE length, bytes */
    unsigned char hdr[3] = { (unsigned char)dir,
                             (unsigned char)((len >> 8) & 0xff),
                             (unsigned char)(len & 0xff) };
    write(log_fd, hdr, 3);
    write(log_fd, buf, len);
}

int open(const char *p, int flags, ...) {
    if (!real_open)  real_open  = dlsym(RTLD_NEXT, "open");
    int fd = real_open(p, flags);
    if (fd >= 0 && p && strcmp(p, "/dev/ttyS1") == 0) ttys1_fd = fd;
    return fd;
}

ssize_t read(int fd, void *buf, size_t len) {
    if (!real_read)  real_read  = dlsym(RTLD_NEXT, "read");
    ssize_t n = real_read(fd, buf, len);
    if (fd == ttys1_fd && n > 0) log_frame('R', buf, n);
    return n;
}

ssize_t write(int fd, const void *buf, size_t len) {
    if (!real_write) real_write = dlsym(RTLD_NEXT, "write");
    if (fd == ttys1_fd) log_frame('W', buf, len);
    return real_write(fd, buf, len);
}
```

Mit der Datei `/tmp/bus_shim.bin` (Frame-Format: `<R|W><lenHi><lenLo><bytes>`) lässt sich nachträglich TX/RX separiert auswerten — dann sind die noch fehlenden Action-CMD-Bytes (call, monitor, unlock, hook, hang, intercom, switchCamera, ringback) eindeutig identifizierbar.
