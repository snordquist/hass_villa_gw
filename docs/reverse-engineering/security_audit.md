# Villa GW V3.0 (AVL20P) — Security Audit

**Scope:** Penetration-Test / Embedded-Security-Review der HHG/EGB Villa GW V3.0
(Firmware 4.1.11, Hardware ACP-03, Modell-String `AVL20P`).
**Setting:** Heim-LAN hinter FRITZ!Box, kein Inbound-Internet, kein Port-Forward.
**Quellen:** `villa_gw_dump/` (Lua/Binaries/init), Live-Telnet/SSH-Tests, plus
die parallelen RE-Reports `http_api.md`, `database.md`, `small_daemons.md`,
`cloud_sync.md`, `boot_init.md`, `architecture.md`.
**Constraint:** Türöffnung über `nc <gw> 10087 << 'AT+B UART unlock 1'` muss
funktional bleiben.

---

## 1. Angriffsflächen-Matrix (LAN-erreichbar)

| Port | Proto | Daemon | Auth | Read | Modify / Execute |
|---|---|---|---|---|---|
| **22** | TCP | dropbear sshd + OpenSSH (parallel) | **leeres root-PW** (ssh-dss/-rsa) | gesamtes FS als root | beliebige Shell-Commands, FW-Patch, Backdoor |
| **23** | TCP | busybox telnetd (2 Instanzen, `-l sh`) | **keine** | dito | dito |
| **80** | TCP | nginx + OpenResty Lua | Cookie-JWT (admin/admin Default); etliche Routen ohne `verify` | Web-UI + REST; `/api/backup` exfiltriert ganze DB | siehe http_api.md Abschnitt 2 — Tür öffnen, Reboot, FW-Upload (root-RCE) |
| **554** | TCP | mimedia | RTSP Basic-Auth `admin:admin` | Live-Video / -Audio von Außenstation | — |
| **1936** | TCP | mimedia | — | RTMPS-Endpoint (interner Stream-Push, im LAN i.d.R. nicht aktiv) | — |
| **5060** | UDP+TCP | pjsua | — | SIP-INVITE/REGISTER, kein TLS | Call-Spoofing möglich (kein 401 auf INVITE im LAN getestet) |
| **5061** | TCP | pjsua | TLS, aber Server-Auth-Modus unklar | SIPS | dto. |
| **6210 / UDP** | UDP | discovery (mcast 239.255.255.240) | **keine** | Geräte-Inventur: `{name,id,ip,mac,version,hardware,state,config}` per `{"command":"search"}` Broadcast | — (nur Read) |
| **9527 / UDP** | UDP | — | — | nicht im Dump-Code; Port wird von etlichen Hisilicon-OEMs für Vendor-Discovery (XMeye/IPC-Stil) genutzt — am Live-Gerät prüfen, eventuell durch andere Hisilicon-Komponente belegt | unklar |
| **10010** | TCP | firmware_upgrade.lua | **`0.0.0.0`, keine Auth** (Log sagt fälschlich `127.0.0.1`) | — | `AT+B UPGRADE 3` triggert HTTP-OTA-Download + `sh update.sh` als root → **RCE** |
| **10086** | TCP | avlink | Source-Filter (MQTT/localhost only) → `response=err` für direkte LAN-Clients | — (ohne MQTT-Forge) | über `/api/elock`, `/api/key`, `/api/testConnect` (HTTP-Wrapper) trotzdem erreichbar |
| **10087** | TCP | uart2d | **keine** | Bus-Stream lesen | **`AT+B UART unlock 1`** → Tür öffnen; `monitor` → stille Kamera-Aktivierung; `call` → Klingelton; Bus-Stör-Frames |
| **10600** | TCP | mimedia | — | RTSP-Backend (interner Port; Frames-Pipeline) | — |
| **33333** | TCP+UDP | pjsua | — | SIP RTP/Media-Relay | — |
| **localhost 60000** | TCP | custode2.lua | nur 127.0.0.1, keine Auth | — | über SSH-Tunnel: `AT+B RELOAD {wifi,address,icloud,himedia,NTP,eth0 start/stop}` |

**Anmerkung zu 9527:** In den Lua-Skripten und Binary-Strings im Dump taucht 9527
nicht auf. Wenn der Port am Live-Gerät offen ist, kommt er aus einem Hisilicon-
oder Vendor-Helper-Binary, das nicht im Dump enthalten ist (typisch:
`SearchDevice`/XMEye-Discovery). Mit `nc -uvz <gw> 9527` und `nmap -sU -p9527
<gw>` verifizieren; vermutlich Read-only-Inventory.

---

## 2. Default-Creds-Audit

| Service | User | Default | Status auf diesem Gerät | Verifikationskommando |
|---|---|---|---|---|
| Telnet (23, x2) | — | `-l sh` → direkte Shell, **keine Login-Prompt** | bestätigt | `nc <gw> 23` → `/  #` Prompt |
| SSH/Dropbear (22) | `root` | leeres Passwort, `ssh-dss`/`ssh-rsa` Host-Key | bestätigt | `ssh -oHostKeyAlgorithms=+ssh-rsa -oPubkeyAuthentication=no root@<gw>` → Enter ohne PW |
| SSH/OpenSSH (22, `/customer/ssh`) | `root` | konfiguriert in `/customer/ssh/etc/sshd_config` (im Dump nicht enthalten) | wahrscheinlich identisch zu dropbear | `ssh -v root@<gw>` Hostkey-Type prüfen |
| Web (80) | `admin` | `admin` (DB-Eintrag, Klartext, `grp=0`) | bestätigt (`secrets.local.md`) | `curl -d '{"name":"admin","password":"admin"}' http://<gw>/api/login` |
| Web (80) | `superadmin` | `super1314` (Hersteller-Master, `grp=0`) | bestätigt (DB-Default in `avl20.dump.sql`) | dito mit `name=superadmin,password=super1314` |
| Web (80) | `device` | `device` (`grp=1`, read-only) | bestätigt | dito |
| RTSP (554) | `admin` | `admin` (Basic) | bestätigt | `ffplay rtsp://admin:admin@<gw>/live.sdp` |
| MQTT (out → Cloud) | `<sip_id>` | `<sip_pw>` aus `config.sip` | Geräte-spezifisch, kein „Default" | — |
| Cloud-Account | `<email>` | `<user_pw>` aus `config.cloud_account.password` | User-spezifisch, im Klartext in DB | — |

**Konsequenz:** Drei voneinander unabhängige Pfade zur Root-Shell ohne ein
einziges Passwort zu kennen: Telnet (no-auth), Dropbear (empty-PW), `/api/upload`
(Multipart + `sh update.sh` ohne Verify) — siehe §6.1.

---

## 3. Klartext-Secrets-Inventar

Alle in dieser Liste sind **nicht-gehasht / nicht-verschlüsselt** und per Telnet
oder `/api/backup` (kein Auth!) in unter 30 Sekunden abgreifbar.

### 3.1 In `/customer/share/avl20.db` (SQLite)

| Tabelle / Spalte | Inhalt | Verwendung |
|---|---|---|
| `user.password` (3 Zeilen) | `superadmin/super1314`, `admin/admin`, `device/device` | Web-UI-Login |
| `config[cloud_account].password` | Cloud-Account-Klartext-Passwort (= **derselbe** PW, den die iLifestyle-App benutzt — Reuse-Risiko bei E-Mail-Wiederverwendung) | REST-Login gegen `de.ilifestyle-cloud.com/api/login` |
| `config[cloud_account].token` | langlebiges HS256-JWT, **kein `exp`-Claim**, ewig gültig bis Cloud-Revoke | `Authorization`-Header für Cloud-API |
| `config[sip].password` | Geräte-SIP-Passwort (= MQTT-Password, identische Cred) | SIP-REGISTER + MQTT-Connect |
| `config[sip].name` | SIP-User-ID (= MQTT-Username) | dto. |
| `config[wifi].password` | WPA-PSK des WLANs | wpa_supplicant |
| `sipServer.password` | externe SIP-Provider-PWs | bei Multi-Provider-Setup |
| `callList.shareCode` | QR-Pairing-Token | App-Multi-User-Sharing |

### 3.2 Im Filesystem

| Pfad | Inhalt | Notiz |
|---|---|---|
| `/customer/share/ca-certificates.crt` | System-CA-Bundle (Standard Mozilla-Set) | kein Pinning gesetzt; siehe §4 |
| `/customer/share/firmware_upgrade.lua` | Klartext-Lua mit (auskommentierten) Debug-JWT-Tokens (Z. 193–198) | nicht produktiv aktiv (`Parm.debug = false`), aber bestätigt Format |
| `/customer/lua/jwt.lua` Z. 38, 44 | **JWT-HS256-Secret `'hard to guess string device'`** (hardcoded, geräteübergreifend identisch) | Web-Auth-Forge: jeder mit FW-Image kann beliebige Tokens für jedes GW signen |
| `/customer/lua/login.lua` Z. 41 | SQLite-Pfad + Direktzugriff | trivial via Telnet auslesbar |
| `/customer/mac` | MAC eth0 | Identität gegenüber Cloud |
| `/var/run/avlink-mqtt.heartbeat` | Tmpfs-Datei | Liveness-Marker, kein Geheimnis |
| `/usr/local/nginx/logs/{access,error}.log` | Zugriffslog (kann Cookie-Token-Header enthalten) | bei Verdacht auf Token-Leak prüfen |

### 3.3 In Binaries (`strings`)

| Binary | Konstante | Wert |
|---|---|---|
| `avlink` | MQTT-CA-Pfad | `/customer/share/ca-certificates.crt` |
| `avlink` | MQTT-Port (Format-String) | `mqtt port=1883 socket=%d` (Klartext-Default — TLS-Port nur über `mosquitto_tls_set` Codepfad, siehe §4) |
| `firmware_upgrade.lua` | Vendor-OEM-Fallback-Host | `tj.systec-pbx.net`, `c1.systec-pbx.net` |
| `avlink` | Factory-Reset-Trigger | `sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql` |

---

## 4. TLS-Schwächen

Es gibt kein separates `tls_pinning.md` — die TLS-Befunde sind in `cloud_sync.md`
§5 konsolidiert. Zusammenfassung:

| Komponente | Setup | Schwäche |
|---|---|---|
| MQTT-Client (`avlink` → Cloud) | `mosquitto_tls_set(ca_file="/customer/share/ca-certificates.crt", tls_version="tlsv1.2")` | **Kein Pinning** — komplettes System-CA-Bundle wird akzeptiert. Wer die CA-Datei beschreiben kann (= jeder mit Telnet/SSH-Zugriff), trustet jeden gewählten Replacement-Broker. Auch: kein Client-Cert. |
| REST-Cloud (`autoSync.lua` `socket.http`) | `https://de.ilifestyle-cloud.com/api/*` über `lua-socket` + (vermutlich) `luasec` | luasec-Default macht TLS-Verify, **aber kein Pinning**. Replacement-CA in `/customer/share/ca-certificates.crt` reicht ebenfalls. |
| Firmware-Update (`firmware_upgrade.lua`) | `http://c1.ilifestyle-cloud.com/download_path?...` + `http://...update.tar.bz2` | **Komplett HTTP**, kein TLS überhaupt, kein Signatur-Check. MITM/DNS-Hijack → arbiträrer Root-Code. |
| RTMP-Push (Cloud-Live-Stream) | `rtmp://rtmp.de.ilifestyle-cloud.com/live/<key>` | Plain RTMP, kein RTMPS. Stream-Key in DB im Klartext. |
| P2P-Tunnel | proprietär, kein öffentlich dokumentiertes TLS | nicht weiter analysiert |
| SIP (5060) | UDP, kein TLS | SIPS auf 5061 vorhanden, aber Default-Pfad bleibt UDP/5060 |
| Web-UI (80) | **HTTP, kein HTTPS** | Cookie ohne `HttpOnly`/`Secure`/`SameSite` (siehe §5.2) → MITM-Token-Klau im selben LAN trivial |

**Konsequenz:** TLS-Verifikation existiert für MQTT/REST-Cloud, aber das System-
CA-Bundle ist beschreibbar → bei Shell-Zugriff trivial überstimmbar; Pinning gibt es
nirgends. **OTA-Pfad ist Plain-HTTP** — größter Single-Point-of-Failure.

---

## 5. Code-Schwächen (Datei:Zeile)

### 5.1 SQL-Injection (Severity: HIGH bei `login.lua` — pre-auth)

| Datei | Zeile | Pattern | Pre-Auth? |
|---|---|---|---|
| `customer/lua/login.lua` | 45 | `string.format("SELECT name, grp FROM user WHERE name='%s' and password='%s' LIMIT 1;", name, password)` | **JA** — kein Cookie nötig |
| `customer/lua/password.lua` | 24, 36 | `name='%s' and password='%s'`, `UPDATE user SET password='%s' WHERE name='admin'` | nein (post-auth) |
| `customer/lua/apk.lua` | 52 + zahlreiche `string.format` für Config-Writes | DB-Writes ungeschützt | **JA** (apk-Endpoint hat kein verify!) |
| `customer/lua/addCallList.lua`, `updateCallList.lua`, `cloudDevice.lua` | überall | identisches Anti-Pattern | nein (post-auth) |

**Beispiel-Exploit (pre-auth Login-Bypass):**
```bash
curl -X POST http://<gw>/api/login \
  -d $'{"name":"admin","password":"x\' OR 1=1--"}'
# liefert valid JWT als grp=0 (admin)
```
Längenlimit 20 reicht aus. **Hardcoded JWT-Secret macht die SQL-Injection
optional** — wer das Secret kennt, signt das Token selbst.

### 5.2 Authentifizierungs-Defekte

| Datei | Zeile | Defekt |
|---|---|---|
| `customer/lua/jwt.lua` | 38, 44 | Hardcoded HS256-Secret `'hard to guess string device'` (geräteübergreifend identisch) |
| `customer/lua/login.lua` | 9–25 (`verify()`) | Liest `iat`-Claim, **prüft aber nicht gegen aktuelle Zeit** → Tokens ewig gültig |
| `customer/lua/login.lua` | 80 | `ngx.header['Set-Cookie'] = {'token='..ret}` — **kein `HttpOnly`, kein `Secure`, kein `SameSite`** → XSS-Auth-Bypass möglich, CSRF auf alle non-GET Routen möglich |
| `customer/lua/elock.lua` | gesamtes File | **Kein `group=0`-Check** — jeder authentifizierte User (auch `device/device` `grp=1`) öffnet die Tür |
| `customer/lua/key.lua` | gesamtes File | dto. |
| `customer/lua/reboot.lua` | gesamtes File (9 LoC) | **Kein `verify()`-Call, keine `group`-Prüfung** — `os.execute('reboot')` Plain |
| `customer/lua/backup.lua` | gesamtes File | dto. — exfiltriert DB nach web-erreichbaren `/var/www/html/` |
| `customer/lua/upload.lua` | gesamtes File | dto. — multipart-Upload + `sh update.sh` als root |
| `customer/lua/test.lua` | 55 | dto. — multipart-Upload nach `/dev/factory.tar.bz2` + `sh factory.sh` |
| `customer/lua/sync.lua` | gesamtes File | dto. — entpackt nach `/customer/share/` + `reboot` |
| `customer/lua/apk.lua` | gesamtes File | App-Pairing-Endpoint ohne Auth (per Design — aber lebenslange Cloud-Bind-Übernahme via einzelnem POST möglich) |

> ⚠ **Caveat:** Ob `backup.lua`, `reboot.lua`, `upload.lua`, `test.lua`, `sync.lua`
> tatsächlich pre-auth aufrufbar sind, hängt von der nginx-Location-Config ab
> (`access_by_lua_file verify.lua`). Die Conf-Datei im Dump (`etc/nginx/conf.d/*.conf`)
> ist XOR-/RC4-verschlüsselt und nicht lesbar (siehe `boot_init.md` §7). **Live-
> Verifikation am Gerät zwingend** mit `curl -X POST http://<gw>/api/reboot`
> ohne Cookie. Auf einem Default-FW-Stand schickt der Server üblicherweise die
> Lua-Datei nur durch wenn die Location das `verify` enthält — bei diesem OEM
> hat sich aber gezeigt (Forum-Hinweise), dass die genannten Endpoints
> öffentlich sind.

### 5.3 Command-Injection / Pattern-Schwächen

| Datei | Zeile | Risiko |
|---|---|---|
| `customer/lua/device.lua` | 32 | `os.execute("date -s "..datetime)` — Pattern-Filter `(%d%d%d%d-...%d%d:%d%d:%d%d)` begrenzt zwar Format, aber `os.execute` läuft als root; bei Pattern-Mismatch wird der Pfad eh nicht ausgeführt — **niedriges direktes Risiko**, aber: System-Uhr-Manipulation bricht TLS-Cert-Validation (`notBefore`/`notAfter` der Cloud-Certs) → Hebel für MITM auf den Cloud-Sync. |
| `customer/lua/network.lua` | überall | schreibt `/etc/network/interfaces` aus User-Input, `killall udhcpc`, `/etc/init.d/networking restart` — keine Quote-/Escape-Filterung; Body kann beliebige Strings durchschleusen |
| `customer/lua/testConnect.lua` | 28 | `'AT+B CHECKSIP 1 s='..server..' u='..account..' p='..password..'\r\n'` — **CRLF-Injection** in den AT-Stream möglich; ein `server` mit `\r\nAT+B ELOCK OPEN` würde an avlink:10086 zwei AT-Befehle posten und der zweite läuft ohne Group-Check. |
| `customer/lua/elock.lua` | 15–16 | `action` kommt aus URI direkt in `'AT+B ELOCK %s\r\n'`; nginx-URI-Parser blockiert `\r\n`, aber jeder andere ELOCK-Subcommand ist via URI durch (Sub-Pfade möglich) |
| `customer/lua/wifilist.lua` | im Code | `wpa_cli scan` + `sleep 5` Blocking — DoS-Vektor (Worker pro Aufruf 5 s belegt) |

### 5.4 Path-Traversal

| Datei | Zeile | Hinweis |
|---|---|---|
| `customer/lua/upload.lua` | 30 | Filename hardcoded auf `/tmp/update.tar.bz2` → kein klassischer Traversal über Multipart-`filename`. **ABER:** der Tarball selbst wird mit `tar -xjf` entpackt **ohne `--no-same-permissions`/`--no-overwrite-dir`/`--anchored`** → Tarball kann beliebige Pfade enthalten (`../../etc/passwd`, symlinks). Standard-GNU-tar entpackt das ohne weitere Optionen relativ. → bei Tarball-Kontrolle volle FS-Schreibrechte. |
| `customer/lua/sync.lua` | 58 | dto., Tarball entpackt nach `/customer/share/` |
| `customer/lua/test.lua` | 30, 55 | Schreibt Multipart-Upload nach **`/dev/factory.tar.bz2`**, entpackt in `/dev/factory/` — `/dev` ist tmpfs, aber wenn der Tarball `../etc/...`-Pfade enthält, landet das im rootfs |
| `customer/lua/backup.lua` | 10 | `os.execute('cd /customer/share/ && tar -cjf /var/www/html/'..fileName..' ./avl20.db')` — `fileName` ist `os.date("%Y%m%d%H%M%S")`-formatiert, kein User-Input → niedriges Risiko |

### 5.5 Hardcoded Secrets

| Datei | Zeile | Secret |
|---|---|---|
| `customer/lua/jwt.lua` | 38, 44 | `'hard to guess string device'` (HS256-Key) |
| `customer/share/firmware_upgrade.lua` | 193–198 | (auskommentiert) Debug-JWT-Beispiele; aktive Token kommen aus DB |
| `avl20.dump.sql` (Factory-Default) | INSERT-Zeilen | `superadmin/super1314`, `admin/admin`, `device/device` |

---

## 6. CVE-Style-Schwachstellen-Liste

> Severity-Skala: CRITICAL (Pre-Auth-RCE/Door), HIGH (Post-Auth-RCE oder Pre-Auth
> Tür/Privacy), MEDIUM (Info-Disclosure / Post-Auth-DoS), LOW (Hardening-Defekte).

### CVE-LOCAL-001 — Telnet `-l sh` ohne Authentifizierung (CRITICAL)

- **Wo:** `etc/init.d/rcS:14` (`telnetd -l sh`) und Z. 28 (`busybox telnetd &`).
- **Beweis:** `nc <gw> 23` → sofortiger root-Shell-Prompt, keinerlei Login.
- **Impact:** Vollzugriff auf alles. Alle anderen Befunde sind danach trivial.
- **Härtung:** Im `rcS` die zwei `telnetd`-Zeilen entfernen. Da `rcS` auf
  rw-mounted UBI liegt, ist das per Telnet/SSH-Session **persistent editierbar**.
  Alternativ: per `/customer/demo.sh` ein Post-Boot-`killall telnetd` setzen
  (überlebt OTA, weil `demo.sh` von OTA nicht angefasst wird).

### CVE-LOCAL-002 — Dropbear akzeptiert leeres Root-Passwort (CRITICAL)

- **Wo:** `/etc/init.d/dropbear start` (rcS Z. 35), Konfiguration in `/etc/dropbear/`
  (im Dump nicht enthalten, Verhalten am Live-Gerät bestätigt).
- **Beweis:** `ssh -oHostKeyAlgorithms=+ssh-rsa root@<gw>` → ohne PW eingeloggt.
- **Härtung:** `passwd root` setzen → speichert in `/etc/shadow` (auf rootfs);
  überlebt Reboot. Wird durch Firmware-OTA potenziell überschrieben — daher
  zusätzlich Pubkey-Auth einrichten (`/root/.ssh/authorized_keys`) und in der
  dropbear-Config `PasswordAuth no` bzw. `-s`-Flag setzen.

### CVE-LOCAL-003 — Hardcoded JWT-HS256-Secret (CRITICAL)

- **Wo:** `customer/lua/jwt.lua:38,44` — `'hard to guess string device'`.
- **Beweis:** geräteübergreifend identisch (siehe `http_api.md` §6.1).
- **Impact:** Jeder, der irgendeine AVL20P-Firmware besitzt, kann für **jedes**
  produktiv laufende Gerät dieses Modells gültige Web-Admin-Tokens signen.
- **Härtung:** Per-Device-Random-Secret einbauen ist firmware-tief. Praktikabel:
  `/customer/lua/jwt.lua` patchen, Secret aus `/customer/share/jwt.key`
  (gefüllt aus `/dev/urandom` bei Erststart in `demo.sh`) einlesen. Bricht
  iLifestyle-App-Login **nicht**, weil App-Login direkt gegen die Cloud geht,
  nicht gegen das GW.

### CVE-LOCAL-004 — Default Web-Creds `admin/admin` (CRITICAL bis sie geändert sind)

- **Wo:** `avl20.dump.sql`, im Klartext in `user`-Tabelle.
- **Beweis:** `curl -d '{"name":"admin","password":"admin"}' http://<gw>/api/login`.
- **Härtung:** Web-UI → "Advanced → Change password" mit langem PW.
  `superadmin/super1314` kann **nicht** über das normale UI geändert werden —
  bleibt persistent! → zusätzlich per SQL:
  ```sql
  UPDATE user SET password='<random32>' WHERE name='superadmin';
  ```

### CVE-LOCAL-005 — Pre-Auth SQL-Injection im Login (CRITICAL)

- **Wo:** `customer/lua/login.lua:45`.
- **Beweis:** `name=admin'--` → Login als admin ohne PW.
- **Härtung:** Lua-File patchen, `db:prepare(...)` mit Bind-Parameters statt
  `string.format`. Risiko: OTA überschreibt den Patch.
  Workaround (persistent): identische Logik in `/customer/demo.sh`
  als Post-Boot-Hook `sed`-en, oder gleich `nginx`-`access_by_lua_block` davorhängen,
  der `name` gegen `^[A-Za-z0-9_]{1,20}$` filtert.

### CVE-LOCAL-006 — `/api/upload` unauthenticated RCE (CRITICAL — sofern Location-Block kein verify hat)

- **Wo:** `customer/lua/upload.lua:56` — `os.execute("cd /tmp/ && tar -xjf update.tar.bz2 && cd update && sh update.sh")`.
- **Beweis:** `curl -F 'f=@evil.tar.bz2' http://<gw>/api/upload` ohne Cookie
  (live-zu-prüfen — siehe Caveat in §5.2).
- **Härtung:** im LAN-Firewall Port 80 von allem außer HA-Host blockieren;
  zusätzlich Web-Admin-PW ändern (falls Location doch verify hat).

### CVE-LOCAL-007 — `/api/test` unauthenticated factory-script RCE (CRITICAL)

- **Wo:** `customer/lua/test.lua:55` — `sh factory.sh`.
- **Beweis:** wie CVE-006.
- **Härtung:** wie CVE-006.

### CVE-LOCAL-008 — `/api/sync` unauthenticated restore + reboot (HIGH)

- **Wo:** `customer/lua/sync.lua:58,60`.
- **Beweis:** wie CVE-006.
- **Härtung:** wie CVE-006.

### CVE-LOCAL-009 — `/api/backup` unauthenticated DB-Exfiltration (HIGH)

- **Wo:** `customer/lua/backup.lua:10`.
- **Beweis:** `curl http://<gw>/api/backup` → liefert `{status:0,ret:"backup_YYYY...tar.bz2"}`;
  Datei dann unter `http://<gw>/backup_YYYY....tar.bz2` (Web-Root) abrufbar →
  enthält Klartext-Cloud-PW, JWT, SIP-PW, WPA-PSK.
- **Härtung:** wie CVE-006. Zusätzlich: `find /var/www/html -name 'backup_*.tar.bz2' -delete`
  als regelmäßiger cron/`monitor.lua`-Hook.

### CVE-LOCAL-010 — `/api/reboot` unauthenticated (MEDIUM, DoS)

- **Wo:** `customer/lua/reboot.lua:6`.
- **Beweis:** `curl http://<gw>/api/reboot`.
- **Härtung:** wie CVE-006.

### CVE-LOCAL-011 — TCP 10010 `0.0.0.0` ohne Auth — OTA-Trigger-RCE (CRITICAL)

- **Wo:** `customer/share/firmware_upgrade.lua` — `linux.tcpserver("0.0.0.0", "10010", 5)`
  (Log-String lügt `127.0.0.1`).
- **Beweis:** `printf 'AT+B UPGRADE 3\r\n' | nc <gw> 10010` → GW lädt von
  `update_server` (DB-Wert, **Plain-HTTP**) tar-bzip2, entpackt, `sh update.sh`.
  In Kombination mit DNS-Hijack auf `c1.ilifestyle-cloud.com` oder
  `update_server`-DB-Patch → triviales Root-RCE.
- **Härtung:** Port am LAN-Firewall blockieren. Lua-Patch (persistente
  Lösung): `linux.tcpserver("127.0.0.1", "10010", 5)`. Datei ist Plain-Lua, einfach
  per `sed -i 's/0.0.0.0/127.0.0.1/'` editierbar; OTA-Reset-Robust via `demo.sh`-
  Re-Apply.

### CVE-LOCAL-012 — TCP 10087 (uart2d) ohne Auth — Türöffner aus dem LAN (CRITICAL für privacy/security, **NICHT zu härten**)

- **Wo:** `uart2d` Binary, bindet `0.0.0.0:10087`.
- **Beweis:** `printf 'AT+B UART unlock 1\r\n' | nc <gw> 10087` → Tür öffnet.
- **Impact:** Jeder im LAN: Tür öffnen, stille Kamera-Aktivierung (`monitor` →
  Privacy-Eingriff), Klingeln auslösen, Bus stören.
- **Härtung — NICHT auf dem Gerät:** das ist der Pfad, den die HA-Integration
  benutzt. Stattdessen LAN-seitig härten:
  - **VLAN-Segmentierung**: GW in eigenes IoT-VLAN, nur HA-Host und Bus-PSU
    Inbound-erlaubt.
  - FRITZ!Box „Gastnetz" oder dediziertes 802.1q-VLAN, ACL: nur
    `203.0.113.12` (HA) ↔ `203.0.113.10` (GW) auf Port 10087.
  - Falls nur ein Subnet existiert: nbftables/ufw am HA-Host als „Reverse-Proxy"
    (HA stellt eine schlanke Auth-Schicht vor `nc :10087` → keiner spricht
    direkt mit dem Port), GW-IP per ARP-Hardening nur HA erreichbar.

### CVE-LOCAL-013 — JWT ohne Ablaufprüfung (HIGH)

- **Wo:** `customer/lua/login.lua:9–25` (`verify()`).
- **Beweis:** Token aus altem Capture (Monate alt) gegen frisches Gerät spielen →
  akzeptiert (Code prüft kein `iat`/`exp` gegen `os.time()`).
- **Härtung:** in `verify()` nach Z. 17 ergänzen:
  ```lua
  local iat = jwt:get_grant_int('iat')
  if not iat or iat < os.time() then jwt:free(); return false end
  ```
  (Das aktuelle `login.lua` setzt `iat = os.time()+3600` — wirkt also wie ein
  „expires_at"; nach Patch wäre der Token nach 1 h tot.)

### CVE-LOCAL-014 — `/api/elock` und `/api/key` ohne `group=0`-Check (HIGH)

- **Wo:** `customer/lua/elock.lua`, `customer/lua/key.lua` — gesamt.
- **Beweis:** Login als `device/device` (`grp=1`), dann `GET /api/elock/OPEN` → Tür auf.
- **Härtung:** in beide Files `if 0 ~= ngx.ctx.group then ngx.say('{"status":2}'); return end`
  einfügen. Patch ist OTA-anfällig — `demo.sh`-Reapply.

### CVE-LOCAL-015 — Cookie ohne HttpOnly/Secure/SameSite (HIGH)

- **Wo:** `customer/lua/login.lua:80`.
- **Beweis:** `Set-Cookie: token=<jwt>` ohne weitere Flags.
- **Impact:** Reflected-XSS im Web-Frontend → Token-Klau → Auth-Bypass. CSRF
  auf jeden non-GET-Endpoint (z.B. `POST /api/password` aus fremder Seite).
- **Härtung:** Patch:
  ```lua
  ngx.header['Set-Cookie'] = {'token='..ret..'; HttpOnly; SameSite=Strict'}
  ```
  `Secure` nur sinnvoll wenn HTTPS aktiv (Web ist HTTP-only auf GW).

### CVE-LOCAL-016 — Web-UI nur HTTP, keine TLS (HIGH)

- **Wo:** nginx-Config (verschlüsselt im Dump, am Live-Gerät bestätigt).
- **Impact:** Login-PW + JWT-Cookie wandern Klartext über LAN. WLAN-Sniffer im
  selben Netz → Auth-Übernahme.
- **Härtung:** Self-Signed-Cert auf das GW kopieren, nginx-Conf editieren ist
  schwierig (verschlüsselt) — pragmatischer: HA als Reverse-Proxy mit eigenem
  TLS davor; GW-Port 80 nur lokal erreichen lassen (Firewall).

### CVE-LOCAL-017 — `discovery` Daemon Plain-JSON Multicast (LOW, Info-Disclosure)

- **Wo:** `customer/app/sbin/discovery`, UDP 239.255.255.240:6210.
- **Impact:** Jedes LAN-Device kennt MAC/IP/Version/Hardware/State/Config des GW
  ohne Auth.
- **Härtung:** im `rc.local` (= miservice UBI, nicht in Dump) den Init-Eintrag
  deaktivieren — bricht „Discovery via Companion-App" und unsere eigene
  HA-Auto-Discovery (akzeptable Trade-off im paranoiden Setup).

### CVE-LOCAL-018 — OTA über Plain-HTTP, unsigned Tarball (CRITICAL)

- **Wo:** `customer/share/firmware_upgrade.lua` (DL via `http://`), `update.sh`
  ohne Signaturprüfung.
- **Beweis:** Code-Read; kein `openssl dgst -verify` o.ä.
- **Härtung:** OTA-Endpoint via DB blockieren:
  ```sql
  UPDATE config SET item = json_set(item, '$.update_server', '127.0.0.1')
   WHERE name = 'device_update';
  ```
  → OTA-Calls schlagen lokal fehl, FW bleibt eingefroren. Erfordert nichts
  weiter; ist auch reversibel.

### CVE-LOCAL-019 — Klartext-Cred-Persistenz in SQLite (HIGH)

- **Wo:** siehe §3.1.
- **Härtung:** Web-UI-User-PWs auf `mkpasswd -m sha-512` umstellen wäre tief
  invasiv (Lua-Login-Pfad anpassen). Pragmatischer: **Cloud-Account-Reuse
  verhindern** — Cloud-Passwort ist nur in der Cloud relevant; auf der Geräte-
  Seite reicht ein **leerer `cloud_account.password`** sobald das `token` einmal
  gesetzt ist (autoSync prüft nur `token == ''`). →
  ```sql
  UPDATE config SET item = json_set(item, '$.password', '')
   WHERE name = 'cloud_account';
  ```
  Token bleibt funktional bis Cloud-Revoke; PW ist nicht mehr im FS.

### CVE-LOCAL-020 — CRLF-Injection in `AT+B CHECKSIP` Stream (MEDIUM)

- **Wo:** `customer/lua/testConnect.lua:28`.
- **Beweis:** Body `{"server":"x\r\nAT+B ELOCK OPEN","account":"u","password":"p"}` →
  schickt zwei AT-Befehle, der zweite ohne Group-Check.
- **Härtung:** Lua-Patch:
  ```lua
  if server:find('[\r\n]') or account:find('[\r\n]') or password:find('[\r\n]')
  then return -1 end
  ```

### CVE-LOCAL-021 — `wifilist.lua` 5s-Blocking DoS (LOW)

- **Wo:** `customer/lua/wifilist.lua` (`wpa_cli scan` + `sleep 5`).
- **Härtung:** Rate-Limiting im nginx (`limit_req_zone`), oder Endpoint entfernen.

### CVE-LOCAL-022 — Tar-Extract ohne Path-Sanitizing (HIGH, post-Upload)

- **Wo:** `upload.lua:56`, `sync.lua:58`, `test.lua:55`.
- **Impact:** Tarball-Pfade können `../` enthalten, `tar -xjf` läuft als root.
- **Härtung:** `tar -xjf` → `tar --no-overwrite-dir --no-same-permissions
  --no-same-owner --no-absolute-names -xjf` (Lua-Patch). OTA-anfällig.

---

## 7. Härtungs-Empfehlungen (priorisiert)

### CRITICAL — sofort umsetzen

1. **Web-Admin-PW ändern** (Web-UI → admin/admin → langes Random-PW). 30 Sekunden,
   schließt CVE-004 + indirekt CVE-009/CVE-010 für externe Web-Scanner.
   Zusätzlich per Telnet:
   ```sh
   sqlite3 /customer/share/avl20.db \
     "UPDATE user SET password='$(head -c20 /dev/urandom|base64|tr -dc 'A-Za-z0-9'|head -c20)' WHERE name='superadmin';"
   ```
2. **Telnet abschalten** (rcS-Edit) und **SSH-Key statt empty-PW** einrichten:
   ```sh
   # rcS Zeilen 14 und 28 löschen (`telnetd -l sh`, `busybox telnetd&`)
   sed -i '/telnetd/d' /etc/init.d/rcS
   # OR persistenter via demo.sh (überlebt rootfs-OTA):
   cat >> /customer/demo.sh <<'EOF'
   (sleep 5; killall telnetd 2>/dev/null) &
   EOF
   chmod +x /customer/demo.sh

   # SSH Pubkey
   mkdir -p /root/.ssh; chmod 700 /root/.ssh
   echo 'ssh-ed25519 AAAA... your-key' >> /root/.ssh/authorized_keys
   chmod 600 /root/.ssh/authorized_keys
   passwd root  # nicht-leeres PW
   ```
   Trade-off: dropbear-Config in `/etc/dropbear/` ist OTA-anfällig — Pubkey-File
   in `/root/.ssh/authorized_keys` bleibt (rootfs wird durch OTA überschrieben!).
   Daher zusätzlich: `/customer/demo.sh` legt nach Boot den Pubkey wieder ab,
   falls verloren — `customer/` überlebt OTA.
3. **LAN-Firewall**: GW in eigenes VLAN, ACL `nur HA → GW:{23,80,554,10087}`.
   Schließt CVE-006/007/008/009/010/011 + reduziert CVE-012 auf „HA-Host darf
   Tür öffnen" — was wir wollen.
4. **TCP 10010 schließen**: `sed -i 's/0\.0\.0\.0/127.0.0.1/' /customer/share/firmware_upgrade.lua`
   und `reboot`. Schließt CVE-011 final.
5. **OTA-Endpoint neutralisieren** (CVE-018): `update_server` auf `127.0.0.1`
   setzen (siehe SQL oben).

### HIGH — diese Woche

6. **`elock.lua` + `key.lua`** mit `group=0`-Check patchen (CVE-014).
7. **JWT `iat`-Expiry-Check** in `login.lua` ergänzen (CVE-013).
8. **Cookie-Flags** in `login.lua` setzen (`HttpOnly`, `SameSite=Strict`) — CVE-015.
9. **CRLF-Filter** in `testConnect.lua` (CVE-020).
10. **`apk.lua` deaktivieren** wenn Pairing abgeschlossen: einfach das File
    in `/customer/lua/` löschen → 404 für `/api/apk`. App kann nicht mehr
    pairen, aber für unsere HA-Integration egal.
11. **Tar-Extract-Härtung** in upload/sync/test (CVE-022).

### MEDIUM — wenn Zeit ist

12. **Cloud-Account-PW aus DB löschen** sobald `token` einmal gesetzt
    (CVE-019, SQL siehe oben). Token-Refresh-Pfad gibt's eh nicht.
13. **Eigene CA / lokaler MQTT-Broker** (siehe `cloud_sync.md` §6):
    Replacement-Broker auf 192.168.x.y, DNS-Override `de.ilifestyle-cloud.com`
    → eigener Host, eigene CA in `/customer/share/ca-certificates.crt`. Trennt
    GW komplett vom Vendor-Backend (siehe §8 Konsequenzen).
14. **Backup-Datei-Cleanup**: monitor.lua hook `find /var/www/html -name 'backup_*.tar.bz2' -mmin +5 -delete`.
15. **Web-UI nur über HA-Reverse-Proxy mit TLS** (Caddy/nginx in HA) — schließt
    CVE-016.

### LOW

16. `wifilist.lua` Rate-Limit oder Entfernung (CVE-021).
17. `discovery` Daemon deaktivieren (CVE-017) — nur paranoid.

### NICHT zu härten ohne Funktionalitätsverlust

| Was | Warum nicht |
|---|---|
| **TCP 10087 (uart2d) am Gerät schließen** | Das ist der Pfad, über den die HA-Integration die Tür öffnet. Statt am Gerät: VLAN/Firewall davor. Siehe CVE-012. |
| **Hardcoded JWT-Secret per FW-Patch ersetzen** | Möglich (siehe CVE-003), aber tiefer Eingriff in `/customer/lua/jwt.lua`; OTA-anfällig. Pragmatisch: PW ändern + Cookie-Flags reichen für Hobby-Setup. |
| **Cloud-MQTT komplett deaktivieren** | Bricht iLifestyle-App (Push-Klingel auf iPhone). Wer App nicht braucht: ja, geht. |
| **Discovery (UDP 6210) abschalten** | Bricht App-LAN-Onboarding und ggf. HA-Auto-Discovery. |
| **OpenSSH (`/customer/ssh`) zusätzlich abschalten** | Sicherheits-Plus minimal (dropbear bleibt), aber `customer/ssh`-Binaries verbrauchen nur Platz; kann man entfernen, bringt aber nichts. |

---

## 8. Cloud-Trust-Konsequenzen — Was kann iLifestyle bei Backend-Breach?

Modell: Der Angreifer hat die Hersteller-Cloud (`de.ilifestyle-cloud.com` =
Re-Skin von `systec-pbx.net`, Alibaba-Cloud 198.51.100.50) kompromittiert.

### Was das Backend kann

| Fähigkeit | Wirkmechanismus |
|---|---|
| **Tür öffnen** | MQTT-Publish `{"action":"CTRL","event":{"relay":"<id>"}}` an `devices/<MAC>/cmd` → `avlink` triggert Relay (siehe `cloud_sync.md` §4). Kein Pre-Shared-Token, nur MQTT-Username/PW, die der Server selbst vergibt. |
| **Kamera stumm einschalten** | dito, andere Action; Stream wird per RTMP-Push zur Cloud geliefert (`config.video.rtmp` = `rtmp://rtmp.de.ilifestyle-cloud.com/live/<key>`) — Cloud sieht/speichert Video. |
| **Mikrofon mithören** | Cloud kann SIP-INVITE an das GW initiieren; bei aktiver Cloud-Bindung registriert sich `pjsua` gegen den Cloud-SIP-Server. Eingehende Calls von der Cloud → Mikro live. |
| **Firmware ersetzen** | MQTT-Publish `UPGRADE` → `firmware_upgrade.lua` lädt von `update_server` (DB-Wert) per Plain-HTTP → `sh update.sh` als root. Backend kontrolliert beides → **persistenter Root-Implant**. |
| **Klartext-Cred-Exfiltration** | Backend kennt Cloud-Account-PW (User hat es ihm gegeben) und SIP-PW (Cloud hat es generiert). MAC + Modell aus Login bekannt. Token signierbar (Backend kennt sein eigenes HS256-Secret). |
| **Persistenz nach Factory-Reset** | Solange `purpose.bindSelf=1` (Default), führt das Gerät bei Boot Auto-Re-Pairing gegen die Cloud durch (`autoSync.lua:421`). Cloud kann die Bindung beliebig oft erneuern. |

### Was das Backend NICHT kann (ohne lokalen Hebel)

| Nicht erreichbar | Warum |
|---|---|
| Bus-Frames ohne Cloud-Roundtrip lesen | RS-485-Bus ist physisch lokal. Cloud kann nur Befehle senden, nicht eingehenden Bus-Traffic abhören (außer über RTMP-Stream/Mic-Forward). |
| Den lokalen `admin/admin` Web-Login | Cloud kennt den nicht (separater Auth-Store). Aber: Backend kann via FW-Push den Web-Login durch beliebigen ersetzen. |
| RTSP-Stream `:554` direkt | Nur LAN-erreichbar; Cloud bekommt nur den abgeleiteten RTMP-Push. |

### Realer Threat-Level für unser Setup

- **Eintrittswahrscheinlichkeit:** mittel — Alibaba-Cloud-PaaS ist okay
  abgesichert, aber Systec/iLifestyle-Application-Layer ist klassischer
  IoT-OEM-Code ohne sichtbare Audits.
- **Impact bei Breach:** **Tür auf ohne Anwesenheit**, Mikrofon-Eingriff,
  Persistenz. Single-Point-of-Failure ist die Cloud-MQTT-Verbindung.
- **Mitigation für Paranoide:** MQTT-Hijack auf lokalen Broker (siehe
  `cloud_sync.md` §6). Bricht App, schließt aber den Cloud-→-Gerät-Pfad
  vollständig.

---

## 9. Quick-Win-Checkliste (Copy-Paste)

```sh
# 1. Web-PW ändern (per Telnet)
NEWPW=$(head -c20 /dev/urandom|base64|tr -dc 'A-Za-z0-9'|head -c20)
sqlite3 /customer/share/avl20.db \
  "UPDATE user SET password='$NEWPW' WHERE name='admin';"
sqlite3 /customer/share/avl20.db \
  "UPDATE user SET password='$(head -c20 /dev/urandom|base64|tr -dc 'A-Za-z0-9'|head -c20)' WHERE name='superadmin';"
echo "Neues admin-PW: $NEWPW   (notieren!)"

# 2. Telnet abschalten + SSH-Pubkey aufsetzen
sed -i '/telnetd/d' /etc/init.d/rcS
mkdir -p /root/.ssh && chmod 700 /root/.ssh
echo 'ssh-ed25519 AAAA... your-key' >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
passwd root   # nicht-leeres PW

# 3. Persistenter Killer (überlebt OTA)
cat > /customer/demo.sh <<'EOF'
#!/bin/sh
(sleep 5
 killall telnetd 2>/dev/null
 # Pubkey wiederherstellen falls FS überschrieben wurde
 if [ ! -s /root/.ssh/authorized_keys ]; then
   mkdir -p /root/.ssh
   cp /customer/.authkeys.bak /root/.ssh/authorized_keys 2>/dev/null
   chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
 fi
) &
EOF
chmod +x /customer/demo.sh
cp /root/.ssh/authorized_keys /customer/.authkeys.bak

# 4. OTA neutralisieren + Port 10010 schließen
sed -i 's/0\.0\.0\.0/127.0.0.1/' /customer/share/firmware_upgrade.lua
sqlite3 /customer/share/avl20.db \
  "UPDATE config SET item = json_set(item, '\$.update_server', '127.0.0.1') WHERE name='device_update';"

# 5. Cloud-PW aus DB tilgen (Token bleibt aktiv)
sqlite3 /customer/share/avl20.db \
  "UPDATE config SET item = json_set(item, '\$.password', '') WHERE name='cloud_account';"

# 6. Reboot zum Aktivieren
sync; sync; reboot
```

LAN-seitig zusätzlich (FRITZ!Box / Switch):

- GW-MAC `A8:B5:8E:85:35:6E` in eigenes Gast-/IoT-Netz isolieren
- Firewall: nur HA-Host (`203.0.113.12`) ↔ GW (`203.0.113.10`) auf
  TCP 23, 80, 554, 10087 erlauben
- Outbound vom GW: nur 1883/8883 + 443 zu `de.ilifestyle-cloud.com` und
  RTMP nach `rtmp.de.ilifestyle-cloud.com` falls App weiter genutzt; sonst alles dicht

---

## 10. Offene Verifikationspunkte (To-Do)

| Item | Methode |
|---|---|
| `/api/upload`, `/api/test`, `/api/sync`, `/api/reboot`, `/api/backup` tatsächlich pre-auth? | `curl -X POST http://<gw>/api/reboot` ohne Cookie; falls 200 + Reboot → bestätigt |
| TCP 10010 wirklich extern erreichbar? | `nmap -p10010 <gw>` von HA-Host |
| MQTT-Port: 1883 oder 8883 in Produktion? | `tcpdump -i <iface> -nn host <gw> and dst port 1883 or dst port 8883` |
| `luasec`-Default-Verify aktiv für `socket.http`? | MITM-Proxy mit eigenem Cert auf `de.ilifestyle-cloud.com` versuchen — wenn `autoSync.lua` durchgeht → kein Verify |
| OpenSSH-Config (`/customer/ssh/etc/sshd_config`) | per Telnet `cat` |
| UDP 9527 belegt? | `nmap -sU -p9527 <gw>` |
| Reverse-Engineering `rc.local` für vollständigen Boot-Pfad | `cat /etc/init.d/rc.local` per Telnet |
| nginx-Config-Decryption (`/etc/nginx/conf.d/*.conf`) | reicht: `ps -ef | grep nginx` + `ls /usr/local/nginx/conf/` per Telnet, dann effektive Location-Blocks bestimmen |

---

**Fazit:** Das Gerät ist „IoT-Standard-Brutto" — schwach ab Werk, aber alle
Schwächen sind durch sechs Befehle und eine VLAN-Regel auf ein für ein
trusted-LAN-Hobby-Setup akzeptables Niveau reduzierbar. Die Türöffnungs-
Funktionalität via `:10087` bleibt erhalten, weil sie LAN-seitig (VLAN+FW)
gehärtet wird und nicht am Gerät. Der einzige nicht-mitigable Resi-Risk ist
**Cloud-Backend-Breach** — wer das nicht akzeptieren kann, muss die
Cloud-Bindung trennen (`cloud_sync.md` §6 Goldweg).
