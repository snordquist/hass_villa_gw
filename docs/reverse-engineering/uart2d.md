# uart2d — Bus-Master-Daemon

Statisches Reverse Engineering von `/customer/app/sbin/uart2d`
(armv7-EABI5, dyn. linked, stripped, ~717 KB).

`uart2d` ist der **einzige** Prozess, der direkten Zugriff auf den Villa-Bus
(UART `/dev/ttyS1`, RS-485) hat. Sämtliche Bus-Befehle (Wake / Live-View / Unlock
/ Call / Hook / Hang) gehen durch ihn. avlink ist nur sein Frontend — die
AT-Kommandos werden von dort über TCP an uart2d weitergegeben.

→ Sehe auch [[avlink]] (Frontend), [[bus-protocol-notes]] (Frame-Format),
[[villa-gw-ttys1-readers-break-commands]] (Memory: ttyS1-Reader stören uart2d).

## TL;DR

| Aspekt | Wert |
|---|---|
| Pfad | `/customer/app/sbin/uart2d` |
| Start | aus `custode2.lua` (Lua-State-Machine) via `os.execute("uart2d uart2d &")` |
| Restart-Trigger | `AT+B RELOAD address` an custode2.lua:60000 |
| Serial-Device | `/dev/ttyS1` |
| Termios | `tcsetattr` / `cfsetispeed` / `tcgetattr` (Baudrate per Konstante — nicht string-resolvable, vermutl. 19200 oder 9600) |
| TCP-RPC-Port | **10087** (in HA-Integration korrekt) |
| Echo-/UDP-Funktion | `usfd` (UDP-Socket) — String `"echo server bind error"` deutet auf einen UDP-Echo/Discovery-Path |
| TLS | libssl/libcrypto **gelinkt aber nicht TLS für den Bus** — vermutlich für SQLite/SSL-Module oder andere Unterfunktionen |
| Linkage | libc, libcrypto.so.1.0.0, libdl, libm, libpthread, libssl.so.1.0.0 |

## Lifecycle

```text
boot
 └─ /etc/init.d/rcS  → /etc/init.d/rc.local
     └─ Startup-Wrapper (start_app / app start script aus customer/app/)
         └─ avlink (TCP 10086)         ← AT+B-Dispatcher + MQTT-Client
         └─ custode2.lua (TCP 60000)   ← Netzwerk/Reload-Statemachine
             ├─ wenn 'AT+B RELOAD address': killall uart2d; uart2d uart2d &
             └─ wenn 'AT+B RELOAD wifi':   killall uart2d/uard2d, mimedia
```

`AT+B RELOAD address` ist die offizielle "kick uart2d" Anweisung — wird intern
ausgelöst wenn die Bus-Adressliste sich ändert (callList/userType DB-Update).

Aus `custode2.lua:610-611`:
```lua
elseif buf == 'AT+B RELOAD address\r\n' then
    R.log("reload hiuart")
    check_sync()
    os.execute("killall uart2d uard2d")
    os.execute("uart2d uart2d &")
```

Beachte: das Lua schreibt `uart2d uart2d` (binary + argv[0]). Der Daemon
verwendet `argv[0]` vermutlich für Logging — beide Worte sind absichtlich
gleich.

## Sockets

### TCP 10087 — die produktive RPC-Schnittstelle

In dieser Datei keine String-Konstante `"10087"` zu sehen, aber wir wissen aus
Live-Tests, dass uart2d auf `127.0.0.1:10087` lauscht (siehe Memory
`villa-gw-ttys1-readers-break-commands`). Vermutlich wird der Port als
`htons(10087)` integer-konstant geladen — daher kein lesbares Literal.

avlink schreibt seine `AT+B UART …`-Befehle als ASCII-Lines hierhin und liest
die Antwort zurück. Beispiele aus dem Binary:

```
AT+B UART call %d %d
AT+B UART unlock %d
AT+B UART hang %d
AT+B UART hook %d
AT+B UART monitor %s
AT+B UART ringback %s
AT+B MJPG Snap
AT+B VIDEO START
AT+B VIDEO STOP
```

Diese werden also TYP-isch nicht von HA selbst geschickt sondern durch avlink
generiert — was bestätigt: die HA-Integration spricht **uart2d** direkt
(Port 10087) für die Bus-Befehle und braucht avlink nicht zwingend.

### UDP — vermutl. 9527, Echo/Heartbeat

Strings:

```
usfd < 0 socket create fail
usfd epoll_ctl fail
echo server bind error
```

Ein `usfd` (User-UDP-FD) Socket wird per epoll bewacht. Der "echo server"-Pfad
deutet auf einen UDP-Loopback-Test (vielleicht Selbsttest oder eine
Slave-Discovery-Probe an die Außenstation). Live mit `netstat -lnup` checken.

### Unix-Socket

```
cfd = socket(PF_UNIX, SOCK_STREAM, 0)
/var/run/uart-log.socket
```

uart2d öffnet zusätzlich einen Unix-Stream-Socket für Logging zur avlink-Seite.

## UART-Setup für `/dev/ttyS1`

Symbole:

```
tcsetattr
tcgetattr
cfsetispeed
/dev/ttyS1
```

Baudrate-Konstante nicht als String, sondern wahrscheinlich als
`B9600`/`B19200`-Integer im Code. Aus der Bus-Spec (von früheren Bus-Frame-Captures)
ist die Rate **9600 8N1** zu erwarten — siehe `bus-protocol-notes.md`.

`termios`-Flags: kein Hardware-Flow-Control (RS-485 ist Half-Duplex über
RX/TX-Pin + Direction-Toggle, üblich via GPIO).

## Frame-Builder-Funktionen

Aus den Symbol-Strings ableitbar — alle erzeugen Outbound-Bus-Frames:

| Funktion (intern) | Zweck | AT-Command-Entsprechung |
|---|---|---|
| `userial_send_call_msg` | Ruf an Außenstation initiieren | `AT+B UART call <src> <dst>` |
| `userial_send_intercom_call_msg` | Intercom-Ruf | (intercom-AT) |
| `userial_send_call_resp` | Antwort auf eingehenden Ruf | (intern) |
| `userial_send_monitor_call_msg` | Live-View-Anforderung | `AT+B UART monitor <…>` |
| `userial_send_hook_msg` | Hook-toggle | `AT+B UART hook <…>` |
| `userial_send_hang_msg` | Auflegen | `AT+B UART hang <…>` |
| `userial_send_req_unlock_msg` | Türöffner | `AT+B UART unlock <…>` |
| `userial_send_cmd_01` | generischer Cmd-Builder | (intern) |
| `userial_ip_send_cmd_01` | IP-Variante (für IP-Außenstationen) | (intern) |

Parallel dazu existieren `utcp_*`-Funktionen, die identische Nachrichten
**über TCP statt UART** versenden — das sind die Builder, die `userial_*`
wrappen, bevor das Ergebnis ins Frame gepackt wird:

```
utcp_send_call_msg
utcp_send_call_intercom_msg
utcp_send_call_hook_intercom_rsp_msg
utcp_send_unlock_msg
utcp_send_hang_msg
utcp_send_monitor_msg
utcp_send_ringback_intercom_rsp_msg
```

→ Der Naming-Split `userial_*` vs `utcp_*` lässt vermuten, dass uart2d sowohl
RS-485 (UART) als auch IP-basierte Außenstationen (utcp_*) verwaltet — also
ein gemeinsamer Bus-Layer mit zwei Transports.

## DB-Zugriff (read-only)

uart2d öffnet `/customer/share/avl20.db` für lesende Lookups:

```
select item from config where name = 'video';
select item from config where name = 'av_link';
select callType from callList where key = '%d';
select userType from callList where key = '%d';
select enable from callList where key = '%d';
update config set item = '{"enable":%s,"rtsp":"%s","rtmp":"%s"}' where name = 'video';
```

Das `UPDATE` ist die einzige Schreibstelle — uart2d aktualisiert `video.enable`
wenn die Außenstation den Stream startet/stoppt. Konsequenz für HA: der
Camera-Stream-State liegt _auch_ in der DB, nicht nur in avlinks State.

## Verhalten bei mehreren ttyS1-Readern (Memory-Verweis)

Aus [`villa-gw-ttys1-readers-break-commands`]: wenn parallel zu uart2d ein
`cat /dev/ttyS1 > …`-Prozess offen ist, verteilt der Kernel die UART-Reads
round-robin → uart2d sieht keine ACK-Frames mehr → **alle** Befehle timeouten,
avlink loggt `AT_UART_MONITOR response=err`. Diagnose-Schritt 1 bei "Wake/Unlock
geht nicht": `ps | grep ttyS1` auf dem GW.

## Open Items für `live_forensics.md`

- [ ] `netstat -lntp | grep uart2d` — bestätige TCP 10087 + ggf. weitere
- [ ] `netstat -lnup | grep uart2d` — bestätige UDP 9527 + Echo-Server
- [ ] `cat /proc/$(pidof uart2d)/cmdline` → genau `uart2d uart2d`?
- [ ] `lsof -p $(pidof uart2d)` — alle FDs (DB, ttyS1, Unix-Socket, Sockets)
- [ ] Strace einer kompletten Bus-Session (1× Wake + 1× Unlock):
      `strace -f -s 200 -e trace=read,write,sendto,recvfrom -p $(pidof uart2d)`
      → roher Frame-Stream auf ttyS1
- [ ] Termios-Setup via `stty -F /dev/ttyS1 -a` **ohne** uart2d zu killen
      (vorsicht: `cat` würde uart2d sabotieren)

## Bezug zur HA-Integration

Die HA-Integration verwendet `127.0.0.1:10087` direkt mit den `AT+B UART …`
Lines — das ist der saubere, dokumentierte Weg. Es gibt **keinen** Grund, den
Port 10086 (avlink) für Bus-Befehle zu adressieren; das produziert nur
verwirrende `AT_UART_MONITOR response=…`-Logs aus avlinks tcp_cmd_handler-Fehlerpfad.

Verwandt: [[avlink]], [[mqtt_topics]], [[boot_init]], [[reference-villa-gw-internals]].
