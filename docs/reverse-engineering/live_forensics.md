# Live-Forensik-Cheatsheet — Villa GW

Diese Datei ist ein **Lauf-Plan**, kein Bericht. Sie sammelt alle Live-Checks,
die offen geblieben sind aus den statischen Analysen ([[tls_pinning]],
[[uart2d]], [[jwt_forge]], [[boot_init]], [[avlink]] etc.) — sortiert nach
Risiko (niedrig → hoch).

## Zugang

Telnet ist im Boot-Script eingeschaltet (`busybox telnetd&` in
`/etc/init.d/rcS:21`) und es gibt **keine** Passwort-Abfrage — direkter
Root-Login.

```sh
telnet 203.0.113.10
# direkt root-Shell, kein Login
```

Alternativ SSH (dropbear, ab Zeile 25 im rcS):

```sh
ssh root@203.0.113.10   # PW unbekannt, telnet ist der einfachere Weg
```

→ Vor jeder Bus-relevanten Aktion **zuerst**:

```sh
ps | grep ttyS1 | grep -v grep
```

Wenn mehr als ein Reader: aufräumen oder Reboot
(Memory [[villa-gw-ttys1-readers-break-commands]]).

## Risikoklasse 1 — read-only, kann nichts kaputt machen

### Allgemeiner System-Snapshot

```sh
# erste 5 Befehle in einer Session, dann logout — alles in eine Datei pipen
{
  echo "=== uname ===";      uname -a
  echo "=== /proc/cmdline ==="; cat /proc/cmdline
  echo "=== uptime ===";      uptime
  echo "=== free ===";        free
  echo "=== df ===";          df -h
  echo "=== mount ===";       mount
  echo "=== passwd ===";      cat /etc/passwd
  echo "=== shadow ===";      cat /etc/shadow 2>&1
  echo "=== sshd_config ===";  cat /customer/ssh/etc/sshd_config
} > /tmp/snap.txt
# dann von außen:
scp root@203.0.113.10:/tmp/snap.txt ./live/  # falls ssh läuft
# oder
nc 203.0.113.10 12345 < /tmp/snap.txt        # mit telnet+nc-Loop
```

### Prozess- und Socket-Map

```sh
ps -ef                  # alle Prozesse + argv
netstat -lntp           # TCP listening
netstat -lnup           # UDP listening
netstat -lnxp           # Unix domain
netstat -anp            # alle Connections (für Cloud-MQTT-Sicht)
```

Erwartung:

| Port | Proto | Process | Quelle |
|---|---|---|---|
| 10086 | TCP | avlink | [[avlink]] |
| 10087 | TCP | uart2d | [[uart2d]] |
| 9527 | UDP | uart2d | [[uart2d]] |
| 6210 | UDP (multicast) | discovery | [[small_daemons]] |
| 60000 | TCP | custode2 (lua) | [[boot_init]] |
| 5060 | UDP+TCP | pjsua | [[pjsua_sip]] |
| 33333 | TCP | pjsua | [[pjsua_sip]] |
| 554 | TCP | mimedia (RTSP) | [[mimedia]] |
| 10600 | TCP | mimedia | [[mimedia]] |
| 10010 | TCP | (firmware_upgrade) | [[boot_init]] |
| 80/443 | TCP | nginx | [[http_api]] |

Auffälligkeiten dokumentieren — alles, was nicht in der Liste ist, ist ein
Hinweis auf zusätzliche Vendor-Services.

### File-Descriptor-Inventar pro Daemon

```sh
for proc in avlink uart2d mimedia pjsua nginx; do
  pid=$(pidof $proc | awk '{print $1}')
  echo "=== $proc (pid=$pid) ==="
  ls -la /proc/$pid/fd 2>&1
  cat /proc/$pid/cmdline | tr '\0' ' '; echo
  cat /proc/$pid/status | head -20
done
```

Bestätigt:
- avlink hat `/customer/share/ca-certificates.crt` offen
- uart2d hat `/dev/ttyS1` exklusiv (FD nur einmal!)
- Welche FDs auf Cloud-Server (Hostname-Resolve via netstat -an)

### TLS-Material — kritisch für MQTT-Hijack-Pfad

```sh
ls -la /customer/share/ca-certificates.crt
wc -l /customer/share/ca-certificates.crt
md5sum /customer/share/ca-certificates.crt
head -50 /customer/share/ca-certificates.crt
# Vergleich mit Mozilla baseline (aktuelle CA-Bundle md5 z.B. von curl.haxx.se)
```

Live-Probe der Cloud-Cert-Chain (vom HA-Host, nicht vom GW):

```sh
echo | openssl s_client -connect de.ilifestyle-cloud.com:8883 -servername de.ilifestyle-cloud.com -showcerts 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256 -subject -issuer -dates
```

→ Mit Fingerprint speichern, damit bei einem späteren Re-Audit erkennbar wird,
ob der Vendor Pinning-Mechanismen neu eingeführt hat.

Strace auf avlink während Verbindungsaufbau (nach `killall avlink; avlink &`):

```sh
strace -f -s 200 -e trace=open,openat,connect,read,write -p $(pidof avlink) 2>&1 | head -200
```

Sucht nach allen geöffneten Dateien — bestätigt, ob es nur die eine cacert-Datei
gibt oder noch weitere (z.B. Client-Cert).

### DB-Dump für Vollständigkeit

```sh
sqlite3 /customer/share/avl20.db .dump > /tmp/avl20.live.dump.sql
sqlite3 /customer/share/avl20.db .schema > /tmp/avl20.live.schema.sql
sqlite3 /customer/share/avl20.db "SELECT name FROM config;"
sqlite3 /customer/share/avl20.db "SELECT * FROM user;"   # creds (admin/admin etc.)
```

Sichern, mit `villa_gw_dump/customer/share/avl20.dump.sql` vergleichen — Diff
zeigt Felder, die sich seit dem Dump geändert haben (z.B. `purpose.bindSelf`,
`cloud_account.token`).

## Risikoklasse 2 — minimal-invasiv, nur sniffen

### Bus-Frame-Capture **OHNE** uart2d zu killen

→ **NEIN.** Direktes `cat /dev/ttyS1` sabotiert uart2d
([[villa-gw-ttys1-readers-break-commands]]). Stattdessen:

a) **strace-basiertes Sniffen** (kein zweiter Reader nötig, lauscht auf
   read()/write()-Syscalls):
   ```sh
   strace -f -s 200 -e trace=read,write -p $(pidof uart2d) 2>&1 \
     | grep -E '"\\\\x' | head -100
   ```

b) **LD_PRELOAD-Shim** auf `read/write` für FD `/dev/ttyS1` — größere
   Investition, dokumentiert separat unter Pfad C in [[tls_pinning]] /
   den Splitter-Studien.

c) **SSE-Endpoint via OpenResty** — schreibt parsed events in nginx-Logs +
   serviert sie als SSE-Stream an HA. Kein UART-Interception nötig.

### MQTT-Topic-Capture vom Cloud-Broker (passiv)

```sh
# vom HA-Host, mit den DB-Credentials aus avl20.dump.sql cloud_account
mosquitto_sub -h de.ilifestyle-cloud.com -p 8883 \
  --cafile /etc/ssl/certs/ca-certificates.crt \
  -u "$CLOUD_USER" -P "$CLOUD_PW" \
  -t '#' -v
```

→ alle Topics live anzeigen — Ground-Truth für [[mqtt_topics]]-Analyse.

### avlink Log live

```sh
# avlink schreibt nach stdout/stderr — wenn nicht init-gestartet:
ps -ef | grep avlink
# vermutlich gibt es ein /var/log/* oder einen ringbuffer
ls -la /var/log/ /tmp/*.log 2>&1
# als letzter Ausweg: kill+restart unter strace
killall avlink; sleep 1; cd /customer/app && ./sbin/avlink 2>&1 | tee /tmp/avlink.live.log &
```

(Vorsicht: avlink-Restart killt MQTT-Cloud-Connect kurzzeitig. Reboot ist die
sicherere Variante wenn man nicht weiß was sonst wo restart-getriggert wird.)

## Risikoklasse 3 — schreibend / verändernd

### CA-Bundle-Tausch (für MQTT-Hijack)

**Vor jedem Schreibvorgang Backup**:

```sh
cp /customer/share/ca-certificates.crt /customer/share/ca-certificates.crt.bak
sha256sum /customer/share/ca-certificates.crt.bak
```

Eigene CA anhängen (additive Variante):

```sh
cat >> /customer/share/ca-certificates.crt <<'EOF'
-----BEGIN CERTIFICATE-----
...unsere selbst-signierte Root-CA...
-----END CERTIFICATE-----
EOF
```

Rollback: `mv .bak`-Datei zurück.

### MQTT-Server-DB-Switch

```sh
sqlite3 /customer/share/avl20.db \
  "UPDATE config SET item = json_set(item, '\$.mqtt_server', '203.0.113.14') WHERE name='sip';"
echo -ne 'AT+B RELOAD icloud\r\n' | nc 127.0.0.1 60000
```

Rollback:

```sh
sqlite3 /customer/share/avl20.db \
  "UPDATE config SET item = json_set(item, '\$.mqtt_server', 'de.ilifestyle-cloud.com') WHERE name='sip';"
echo -ne 'AT+B RELOAD icloud\r\n' | nc 127.0.0.1 60000
```

### Persistenz via `/customer/demo.sh`

```sh
ls -la /customer/demo.sh 2>&1
# wenn nicht da:
cat > /customer/demo.sh <<'EOF'
#!/bin/sh
# Backup CA bundle restore + augmentation
if [ ! -f /customer/share/ca-certificates.crt.orig ]; then
    cp /customer/share/ca-certificates.crt /customer/share/ca-certificates.crt.orig
fi
cp /customer/share/ca-certificates.crt.orig /customer/share/ca-certificates.crt
cat /customer/myCA.crt >> /customer/share/ca-certificates.crt
EOF
chmod +x /customer/demo.sh
```

Test über Reboot:

```sh
reboot
# warten, dann
ssh root@203.0.113.10 sha256sum /customer/share/ca-certificates.crt
```

## Risikoklasse 4 — destructive / nur als letzter Ausweg

- `dd if=/dev/mtdX of=…` → Firmware-Backup ziehen. Macht das Gerät unbenutzbar
  wenn falsch ausgeführt. Erst nach erfolgreichem Reboot-Test mit demo.sh.
- `purpose.bindSelf = 0` setzen (DB) → verhindert Auto-Re-Pairing (siehe
  [[database]] / [[cloud_sync]]). Effekt erst beim nächsten avlink-Restart.

## Ergebnisablage

Resultate dieser Forensik-Session **nicht** ins Git commiten, sondern in
`secrets.local.md` ablegen wenn sie Credentials enthalten, sonst als
Diff-Ergänzung zu den jeweiligen RE-Reports.

Verwandt: alle anderen RE-Reports — diese Datei ist das Live-Counterpart zur
statischen Analyse.
