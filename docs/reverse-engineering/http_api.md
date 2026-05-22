# Villa GW V3.0 — HTTP API Inventory (Reverse-Engineered)

**Quelle:** Lua-Handler unter `/customer/lua/*.lua` (Dump: `villa_gw_dump/customer/lua/`).
Die zugehörige `www.http.conf` ist im Dump nicht vorhanden — Routen werden anhand der
Handler-Namen und der HTTP-Konvention `/api/<filename>` rekonstruiert (`elock.lua` belegt
explizit ein URI-Suffix-Muster, siehe unten).

**Backend-Architektur (relevant):**
- Lua via OpenResty/nginx (`ngx.*` APIs, `resty.upload`, `lsqlite3`)
- Konfigurations-DB: **SQLite** `/customer/share/avl20.db`
- IPC zur Firmware: TCP `127.0.0.1:10086` (Hauptdaemon, `AT+B …\r\n`-Befehle) und
  `127.0.0.1:60000` (Netzwerk-/Watchdog-Daemon, ebenfalls `AT+B …`)
- Cloud-Backend (siehe `autoSync.lua`): `https://<server>/api/...` mit Bearer-Token im
  Header — **das ist nicht der GW selbst, sondern der Avilio/AVL-Cloud-Endpoint**.

---

## 1. Auth-Flow

### 1.1 Tokens (JWT)

- Bibliothek `ljwt` (C-Lib) via `jwt.lua` Wrapper.
- Algorithmus: **HS256**
- **Secret hardcoded:** `'hard to guess string device'` (`jwt.lua` Z. 38 + 44).
  Das Secret ist auf jedem GW identisch → wer es einmal hat, kann beliebige Tokens
  für jeden GW dieses Modells fälschen. Siehe §6.
- Token-Claims (gesetzt in `login.lua` ab Z. 60):
  - `iat` = `os.time() + 3600` (sic — keine echte „issued at"; effektiv Ablauf-Marker
    1 h in der Zukunft, aber **nirgendwo wird `iat` geprüft** — siehe `verify()`)
  - `name` (String, max. 20 Zeichen)
  - `device_id` (MAC eth0)
  - `model` = `'ACP-03'`
  - `type` = 2
  - `group` (Integer aus DB)
- Token kommt zurück sowohl im Response-Body (`{status:0, token:…}`) als auch als
  Set-Cookie: `token=<jwt>` (kein `HttpOnly`, kein `Secure`, kein `SameSite`).

### 1.2 Verifikation

- `verify.lua` ist als nginx `access_by_lua_file` für die geschützten `/api/*`-Pfade
  gedacht (entzieht sich dem Dump direkt, ergibt sich aus dem Pattern: in jedem
  geschützten Handler wird `ngx.ctx.group` bereits gesetzt vorgefunden).
- Verifikation prüft **nur**: Cookie `token` vorhanden, JWT-Signatur valid mit hartem
  Secret, `name` und `group` als Claims vorhanden.
- **Keine Ablaufprüfung** (`iat` wird gelesen aber nicht gegen aktuelle Zeit verglichen).
  Effektiv: **Tokens sind unbefristet gültig.**
- Bei fehlendem/ungültigem Token → `{"status":1}` und `ngx.OK` (200, nicht 401).

### 1.3 user-Tabelle / Rollen

Aus `login_sqlite3()` (Z. 45) und `password.lua`:
```sql
SELECT name, grp FROM user WHERE name='…' AND password='…' LIMIT 1;
```
- Passwörter sind **Plaintext** in SQLite (`avl20.db`).
- `grp` (Integer):
  - **`0` = admin/superadmin** (volle Schreibrechte; jede `*_sqlite3`-Funktion macht
    `if 0 ~= ngx.ctx.group then return -2 end`)
  - **`1`/sonstige = read-only / "device"** — kommt im GET-Pfad als `status=2`
    ("permission denied") zurück.
- Bekannter User: `admin`. Password-Change verlangt nur für `name=='admin'` das
  alte Passwort (Z. 16 `password.lua`); andere admin-Accounts könnten ohne
  Old-Password-Check geändert werden — irrelevant solange nur `admin` existiert.

### 1.4 Login-Endpoint

- `POST /api/login` mit `{name, password}` → `{status:0, token:…}` + Cookie.
- `GET  /api/login` → falls Cookie valide: `{status:0, name, group}`, sonst `{status:1}`.
- Name & Password werden **direkt** ohne Escaping in den SQL-String formatiert
  (`string.format("SELECT … WHERE name='%s' and password='%s'…", name, password)`)
  → **SQL-Injection** trivial (siehe §6).

---

## 2. Komplette REST-API-Inventory

**Spalten-Legende:**
- **Auth?** = Cookie-JWT (`verify`) — fast alle Routen außer `/api/login` selbst.
- **Group=0?** = Handler prüft zusätzlich `ngx.ctx.group == 0` (Admin only).
- **AT+B / DB** = Side-Effects.

### 2.1 Authentifizierung & Account

| Route | Method | Auth? | Group=0? | Body / Query | Antwort | Side-Effect |
|---|---|---|---|---|---|---|
| `/api/login` | POST | nein | — | `{name, password}` | `{status, token?}` + Cookie | DB SELECT user; setzt JWT-Cookie |
| `/api/login` | GET | Cookie | — | — | `{status, name?, group?}` | nur Validate |
| `/api/password` | POST | ja | nein (admin-Spezialcheck) | `{oldpassword?, password}` | `{status}` (0/2/3/4/5) | `UPDATE user SET password WHERE name='admin'` |
| `/api/account` | GET | ja | ja | — | cloud_account-Config + `status` | DB SELECT `config[cloud_account]` |
| `/api/account` | POST | ja | ja | `{server, account, password, name, token?}` | `{status}` | `UPDATE config[cloud_account]` + **`AT+B RELOAD`** (10086) |
| `/api/userKey` | GET | ja | ja | — | key_setting + `status` | DB SELECT `config[key_setting]` |
| `/api/userKey` | POST | ja | ja | beliebiges JSON | `{status}` | `UPDATE config[key_setting]` + **`AT+B RELOAD`** |
| `/api/key` | GET | ja | nein | — | `{status}` | **`AT+B KEY`** (10086) — triggert Door-Open/Key-Press |

### 2.2 Geräte- / System-Info

| Route | Method | Auth? | Group=0? | Body | Antwort | Side-Effect |
|---|---|---|---|---|---|---|
| `/api/system` | GET | ja | ja | — | Antwort von `AT+B SYSTEM` (Firmware-Info) | **`AT+B SYSTEM`** |
| `/api/application` | GET | ja | ja | — | Antwort von `AT+B APPLICATION` (laufende Apps/SIP-Status) | **`AT+B APPLICATION`** |
| `/api/mac` | GET | ja | ja | — | `{status:0, mac}` | `ifconfig eth0` parse |
| `/api/device` | GET | ja | ja | — | `{name, address, language, timezone, auto, datetime, type, status}` | DB SELECT `config[device]`, `config[datetime]`; `date` |
| `/api/device` | POST | ja | ja | `{name, address, language, timezone, auto, datetime}` | `{status}` | `os.execute("date -s …")` (!), `UPDATE config[device]`, `UPDATE config[datetime]`, **`AT+B RELOAD`** (10086), `AT+B RELOAD NTP` (60000) |
| `/api/parameter` | GET/POST | ja | ja | beliebiges JSON | parameter-Config / `{status}` | DB `config[parameter]`, **`AT+B RELOAD`** |
| `/api/purpose` | GET/POST | ja | ja | `{purpose, bindSelf, state}` | purpose-Config / `{status}` | DB `config[purpose]`, **`AT+B RELOAD`** |
| `/api/address` | GET/POST | ja | ja | `{button, …}` | av_link-Config / `{status}` | DB `config[av_link]`, **`AT+B RELOAD address`** (60000) |

### 2.3 Netzwerk / WiFi

| Route | Method | Auth? | Group=0? | Body | Antwort | Side-Effect |
|---|---|---|---|---|---|---|
| `/api/network` | GET | ja | ja | — | `{mac, ip, netmask, gateway, dns, dhcp, status}` | `ifconfig`, `ip route`, `/etc/network/interfaces`, `/etc/resolv.conf` lesen |
| `/api/network` | POST | ja | ja | `{ip, netmask, gateway, dns, dhcp}` | `{status, msg}` early-flushed | Schreibt `/etc/network/interfaces`, `killall udhcpc` od. `udhcpc -i eth0`, `/etc/init.d/networking restart`, **`AT+B RELOAD eth0 start/stop`** (60000), **`AT+B RELOAD network`** (10086); deaktiviert WiFi |
| `/api/wifi` | GET | ja | ja | — | wifi-Config + `connected` (COMPLETED/DISABLED via `wpa_cli`) | DB `config[wifi]`, `wpa_cli status` |
| `/api/wifi` | POST | ja | ja | `{ssid, password, enable}` | `{status}` | `UPDATE config[wifi]`, **`AT+B RELOAD wifi`** (60000) |
| `/api/wifilist` | GET | ja | ja | — | JSON aller WiFi-Scans (raw) | `wpa_cli scan` + `sleep 5` + `scan_result` |
| `/api/wifilist` | POST | ja | ja | wifi-config-JSON | `{status}` | wie `/api/wifi` POST (clean-config-Pfad) |

### 2.4 SIP / Cloud / Calls

| Route | Method | Auth? | Group=0? | Body | Antwort | Side-Effect |
|---|---|---|---|---|---|---|
| `/api/sip` | GET | ja | ja | — | sip-Config + `online` (aus `AT+B APPLICATION`) | DB SELECT + **`AT+B APPLICATION`** |
| `/api/sip` | POST | ja | ja | sip-config-JSON | `{status}` | `UPDATE config[sip]`, **`AT+B RELOAD`** |
| `/api/p2p` | GET | ja | ja | — | Antwort von **`AT+B CONTACTS`** + status | **`AT+B CONTACTS`** |
| `/api/p2p` | POST | ja | ja | p2p-JSON | `{status}` | `UPDATE config[p2p]`, **`AT+B RELOAD`** |
| `/api/cloudDevice` | POST | ja | ja | volle Cloud-Bind-Config (`contact, mqtt_server, name, nickname, password, server, rtmp, rtsp, enable, transfer, p2p_server, update_server`) | `{status}` | `UPDATE config[sip]`, `[video]`, `[device_update]` (INSERT falls fehlt), **`AT+B RELOAD`** (10086) + **`AT+B RELOAD icloud`** (60000) |
| `/api/avSetting` | GET/POST | ja | ja | av_setting-JSON | / `{status}` | DB `config[av_setting]`, **`AT+B RELOAD`** |
| `/api/video` | GET/POST | ja | ja | video-JSON | / `{status}` | DB `config[video]`, **`AT+B RELOAD`** + **`AT+B RELOAD himedia`** (60000) |
| `/api/relay` | GET/POST | ja | ja | relay-JSON | / `{status}` | DB `config[relay]`, **`AT+B RELOAD`** |
| `/api/testConnect` | POST | ja | ja | `{server, account, password}` | `{status}` | **`AT+B CHECKSIP 1 s=… u=… p=…`** (10086) — startet Register-Test |
| `/api/getConnect` | POST | ja | ja | (Body ignoriert) | `{res:<raw>, status:0}` | **`AT+B CHECKSIP 2`** (10086) — fragt Ergebnis ab (Body-Version `CHECKSIP 2 s=… u=… p=…` ist im Code, aber auskommentiert/durch `get_connect_1` ersetzt) |

### 2.5 Call-List / sipServer

| Route | Method | Auth? | Group=0? | Body | Antwort | Side-Effect |
|---|---|---|---|---|---|---|
| `/api/getCallList` | POST | ja | ja | `{rpp, page}` | `{status, ret:[…], total}` | DB SELECT `callList LEFT JOIN sipServer` paginiert |
| `/api/addCallList` | POST | ja | ja | `{callNo, name, address, key, userType, callType, ipAddr, server, account, password, callee, enable, shareCode}` | `{status}` (0/2/4/5) | INSERT in `sipServer` (falls `callType==2`) + INSERT in `callList`, **`AT+B RELOAD callList`** |
| `/api/updateCallList` | POST | ja | ja | wie oben + `id, serverId` | `{status}` (0/2/4/5) | UPDATE/INSERT in `sipServer`/`callList`, **`AT+B RELOAD callList`** |
| `/api/delCallList` | POST | ja | ja | `{id, callType, serverId}` | `{status}` | DELETE `callList` (+ `sipServer` falls callType==2), **`AT+B RELOAD callList`** |
| `/api/cleanCallList` | POST | ja | ja | `{purpose, key}` | `{status}` | DELETE alle `sipServer` + `callList`, reseed `callList(callNo='1', key)` falls purpose==0 |
| `/api/getServerList` | POST | ja | ja | (Body ignoriert) | `{status, ret:[…]}` | DB SELECT alle `sipServer` |
| `/api/updateCloudEnable` | POST | ja | ja | `{id, enable}` | `{status}` | `UPDATE callList SET enable WHERE id=…` |
| `/api/updateShareCode` | POST | ja | ja | `{id, shareCode}` | `{status}` | `UPDATE callList SET shareCode WHERE id=…` |

### 2.6 Aktoren / "Door"-Steuerung

| Route | Method | Auth? | Body | Antwort | Side-Effect |
|---|---|---|---|---|---|
| `/api/elock/<action>` | GET | ja, **kein Group-Check** | — | `{status}` | **`AT+B ELOCK <action>`** (10086) — action kommt **ungeprüft** aus URI |
| `/api/key` | GET | ja, **kein Group-Check** | — | `{status}` | **`AT+B KEY`** (10086) |

`elock` matched `ngx.var.uri:match('/api/elock/(.*)')` → action wird direkt in
`'AT+B ELOCK %s\r\n'` formatiert. Newline-Injection in URI ist durch nginx-URI-Parser
blockiert, aber `action` darf alles enthalten was nginx als URI durchlässt
(Sub-Pfade möglich, z.B. `/api/elock/OPEN`, `/api/elock/CLOSE`, `/api/elock/foo bar`).

### 2.7 Backup / Restore / Upload / Reboot

| Route | Method | Auth? | Group=0? | Body | Antwort | Side-Effect |
|---|---|---|---|---|---|---|
| `/api/backup` | GET (impl.) | **NEIN** | nein | — | `{status:0, ret:"backup_<ts>.tar.bz2"}` | `tar -cjf /var/www/html/<file> ./avl20.db` — DB im Web-Root abrufbar! |
| `/api/sync` (vermutl. Pfad) | POST multipart | **NEIN** | nein | multipart file | `{status:0}` | schreibt `/customer/share/backup.tar.bz2`, **`tar -xjf`**, **`reboot`** |
| `/api/upload` | POST multipart | **NEIN** | nein | multipart file | `{status:0}` | schreibt `/tmp/update.tar.bz2`, **`tar -xjf` & `sh update/update.sh`** — beliebiges Firmware-Update |
| `/api/test` | POST multipart | **NEIN** | nein | multipart file | `{status:0}` | schreibt `/dev/factory.tar.bz2`, **`tar -xjf` & `sh factory/factory.sh`** — factory-reset Hook |
| `/api/reboot` | GET | **NEIN** (lt. Code keine Verify-Call!) | — | — (kein Body, kein status) | `os.execute('reboot')` |

**Wichtige Beobachtung:** `backup.lua`, `sync.lua`, `upload.lua`, `test.lua`, `reboot.lua`
rufen `verify` **nicht** auf und referenzieren `ngx.ctx.group` nicht. Ob sie geschützt
sind, hängt ausschließlich davon ab, ob nginx die Location mit
`access_by_lua_file verify.lua` belegt. Bei `reboot.lua` (9 Zeilen, kein JSON, kein
verify) und `backup.lua` (kein verify) ist das im Code nicht erkennbar — siehe §6.

---

## 3. AT+B-Befehle, die via HTTP exponiert sind

| AT+B-Befehl | Port | Triggernde Routen |
|---|---|---|
| `AT+B RELOAD` | 10086 | account, userKey, sip, p2p, video, parameter, purpose, relay, avSetting, device, cloudDevice |
| `AT+B RELOAD wifi` | 60000 | wifi (POST), wifilist (POST), apk |
| `AT+B RELOAD eth0 start` / `stop` | 60000 | network (POST) |
| `AT+B RELOAD network` | 10086 | network (POST) |
| `AT+B RELOAD address` | 60000 | address (POST), apk |
| `AT+B RELOAD NTP` | 60000 | device (POST) |
| `AT+B RELOAD himedia` | 60000 | video (POST) |
| `AT+B RELOAD icloud` | 60000 | cloudDevice (POST), autoSync |
| `AT+B RELOAD callList` | 10086 | addCallList, updateCallList, delCallList |
| `AT+B KEY` | 10086 | key |
| `AT+B ELOCK <action>` | 10086 | elock (action aus URI) |
| `AT+B SYSTEM` | 10086 | system (GET) |
| `AT+B APPLICATION` | 10086 | application, sip (GET) |
| `AT+B CONTACTS` | 10086 | p2p (GET) |
| `AT+B CHECKSIP 1 s=… u=… p=…` | 10086 | testConnect |
| `AT+B CHECKSIP 2` | 10086 | getConnect |

**Zusätzliche AT+B-Befehle aus HTTP, die nicht im Binary-String-Dump des
Daemons stehen würden**, weil sie aus Lua dynamisch formatiert werden:
- `AT+B ELOCK <action>` — `<action>` ist ein freier String aus der URI; jeder
  ELOCK-Subcommand, den der Daemon kennt, ist via `/api/elock/<X>` aufrufbar
  (z.B. `OPEN`, `CLOSE`, `STATUS`, ...).
- `AT+B CHECKSIP 1|2 s=<srv> u=<user> p=<pass>` — Parameter aus User-Input
  konstruiert; **keine Quote-/Newline-Filterung** (CRLF-Injection in den
  AT-Protokoll-Stream möglich, siehe §6).

---

## 4. Hidden / Undokumentierte Endpoints

Aus Lua-Skripten und Aufrufpfaden ableitbar, ohne dass sie in einer offiziellen
API-Doc stehen:

| Route (vermutet) | Quelle | Anmerkung |
|---|---|---|
| `/api/apk` | `apk.lua` | App-Pairing-Endpoint: bekommt `{ssid, psk, server, account, password, name, button, readd, purpose, bindSelf}`, schreibt WiFi+Cloud+Address+Purpose in einer Operation; ruft anschließend `lua autoSync.lua` (Cloud-Login & Provisioning) als CLI-Subprozess. **Antwortet mit `mac`.** Wird vom Pairing-Flow der mobilen App genutzt. |
| `/api/backup` | `backup.lua` | erzeugt `backup_<ts>.tar.bz2` direkt in `/var/www/html/` — anschließend als statische Datei `https://<gw>/<filename>` lesbar. Enthält die komplette `avl20.db` (inkl. Klartext-Passwörter). |
| `/api/sync` | `sync.lua` | nginx-Pfad nicht eindeutig (Dateiname suggeriert „sync"). Restore-Endpoint mit anschließendem Reboot. |
| `/api/upload` | `upload.lua` | Firmware-Update via multipart, anschließend `sh update.sh` als root. |
| `/api/test` | `test.lua` | Factory-Tarball Upload → `sh factory.sh` als root. **Nur via /dev/ statt /tmp/.** |
| `/api/reboot` | `reboot.lua` | Sofort-Reboot. |
| `/api/autoSync` | nicht über nginx — `apk.lua` Z. 133 `os.execute("cd /customer/lua && lua autoSync.lua")` | Nur lokal als Subprozess; nicht direkt HTTP-erreichbar. |

---

## 5. Power-User-Features (Antwort auf die explizite Frage)

| Feature | Endpoint | Vorhanden? |
|---|---|---|
| Firmware-Upgrade | `POST /api/upload` (multipart) | ✅ vorhanden, ausführt `sh update.sh` als root. |
| Reboot | `GET /api/reboot` | ✅ vorhanden, ohne Body-Response, ohne sichtbaren Auth-Check. |
| Factory-Reset | `POST /api/test` (multipart, Datei wird unter `/dev/factory.tar.bz2` abgelegt und `sh factory.sh` ausgeführt) | ✅ vorhanden — Name irreführend. |
| Backup-Export | `GET /api/backup` → JSON mit Dateinamen, Datei dann unter `/<file>` statisch (`/var/www/html/`) abrufbar. | ✅ vorhanden. |
| Backup-Restore | `POST /api/sync` (multipart) → entpackt nach `/customer/share/` + `reboot`. | ✅ vorhanden. |
| Door-Open / E-Lock | `GET /api/elock/<action>` (action z.B. `OPEN`) | ✅ vorhanden, **ohne Admin-Check**. |
| Key-Trigger | `GET /api/key` | ✅ vorhanden, **ohne Admin-Check**. |
| Raw AT+B | nicht direkt — nur über die oben genannten Wrapper. | — |

---

## 6. Sicherheit — Befunde aus dem Code

### 6.1 Kritisch

1. **Hardcoded JWT-Secret** `hard to guess string device` (`jwt.lua:38,44`).
   Identisch auf allen GWs → Token-Forgery für beliebigen Account, beliebigen GW,
   sobald jemand ein Firmware-Image hat. → wahrscheinlich auch in der Cloud
   wiederverwendet (selbe Strings im Daemon-Binary erwarten).

2. **JWT ohne Ablaufprüfung**. `iat` wird gesetzt, aber `verify.lua` prüft
   nichts gegen die aktuelle Zeit — Tokens leben „forever".

3. **SQL-Injection im Login** (`login.lua:45`):
   ```lua
   string.format("SELECT name, grp FROM user WHERE name='%s' and password='%s' …", name, password)
   ```
   `name = "admin'--"` führt direkt zum Admin-Login. Längen-Cap 20 reicht aus für
   `' OR 1=1--`.
   Dasselbe Muster zieht sich durch fast jeden Handler (`addCallList`,
   `updateCallList`, `apk`, `cloudDevice` etc.) — überall fließen User-Strings
   ungeschützt in SQL.

4. **Passwörter im Klartext** in SQLite (`user.password`, `cloud_account.password`,
   `sipServer.password`, `wifi.password`). Über `/api/backup` (kein Auth!)
   exfiltrierbar.

5. **Kein Auth auf `/api/upload`, `/api/test`, `/api/backup`, `/api/reboot`,
   `/api/sync`** — die Lua-Skripte rufen `verify` nicht selbst auf und referenzieren
   `ngx.ctx.group` nicht. Schutz hinge ausschließlich an einer nginx-Location-Block-
   Konfiguration, die wir im Dump nicht haben. Best-Case: nginx erzwingt `verify`;
   Worst-Case (üblich bei diesem Codestil): die Pfade sind ohne Cookie aufrufbar.
   `upload.lua` exekutiert ungeprüft `sh update.sh` aus dem Tarball → **RCE-Pfad**.

6. **`os.execute("date -s …")`** in `device.lua:32` baut den String per
   `:match('(%d%d%d%d-…%d%d:%d%d:%d%d)')` — der Pattern-Match wirkt wie Sanitizing,
   begrenzt aber nur das Format, nicht die Bedeutung; weniger kritisch, aber das
   Pattern matched aus jedem String (z.B. der überstrige Body kann arbiträr sein),
   und die Zeit wird als root gesetzt → triggert TLS-Cert-Validation-Probleme der
   Cloud-Sync.

7. **`AT+B CHECKSIP 1 s=<server> …`** (`testConnect.lua:28`) — `server, account,
   password` werden ungeschützt in den CRLF-getrennten AT-Stream serialisiert.
   `\r\n` im `server` ermöglicht **AT-Protocol-Injection** und somit das Absetzen
   beliebiger `AT+B …`-Kommandos am Daemon (z.B. ELOCK, RELOAD), die normalerweise
   nicht über HTTP zugreifbar sind.

8. **WiFi-Scan-Endpoint** triggert `os.execute('wpa_cli -i wlan0 scan')` und
   blockiert per `sleep 5` — DoS-Vektor (Worker-Thread 5 s belegt pro Aufruf).

### 6.2 Mittelschwer

- Cookie `token` ohne `HttpOnly`/`Secure`/`SameSite` → trivially via XSS lesbar.
  Da das GW eine eigene Web-UI aus `/var/www/html` ausliefert, ist eine
  Reflected-XSS-Lücke der UI direkt Auth-Bypass.
- `apk.lua` antwortet **vor** dem `RELOAD` und führt `lua autoSync.lua` als
  Subprozess (Z. 133) — race condition wenn Client mehrfach pairt.
- `network.lua` macht `ngx.eof()` und führt anschließend `os.execute('killall
  udhcpc')` / `/etc/init.d/networking restart` aus — kein Mutex, paralleler Aufruf
  bringt das System in inkonsistenten Netz-Zustand.

### 6.3 Sonstige Beobachtungen

- `elock.lua` und `key.lua` **prüfen kein `group`** — jeder authentifizierte User
  (selbst `grp != 0`) kann die Tür öffnen.
- `verify.lua` shadowt `name`/`group` per `ngx.ctx`; alle nachfolgenden Handler
  vertrauen blind diesem Kontext.
- `backup.lua` schreibt das Backup nach `/var/www/html/` — wenn nginx das
  Verzeichnis als Document-Root serviert (Standard für eine Web-UI), ist die
  unverschlüsselte DB öffentlich auflesbar **bis** ein anderer Backup-Aufruf sie
  überschreibt; die Endpoint löscht alte Backups nicht.

---

## 7. API → Lua-File → AT+B / DB-Op Konsolidiert

| API-Route | Lua-File | DB-Op | AT+B-Kommandos |
|---|---|---|---|
| `/api/login` | login.lua | SELECT user | — |
| `/api/password` | password.lua | UPDATE user | — |
| `/api/account` | account.lua | SELECT/UPDATE config[cloud_account] | AT+B RELOAD (10086) |
| `/api/userKey` | userKey.lua | SELECT/UPDATE config[key_setting] | AT+B RELOAD |
| `/api/key` | key.lua | — | AT+B KEY |
| `/api/system` | system.lua | — | AT+B SYSTEM |
| `/api/application` | application.lua | — | AT+B APPLICATION |
| `/api/mac` | mac.lua | — | — (ifconfig) |
| `/api/device` | device.lua | SELECT/UPDATE config[device,datetime] | AT+B RELOAD + AT+B RELOAD NTP |
| `/api/parameter` | parameter.lua | config[parameter] | AT+B RELOAD |
| `/api/purpose` | purpose.lua | config[purpose] | AT+B RELOAD |
| `/api/address` | address.lua | config[av_link] | AT+B RELOAD address |
| `/api/network` | network.lua | SELECT/UPDATE config[wifi], schreibt `/etc/network/interfaces` | AT+B RELOAD eth0 start/stop, AT+B RELOAD network |
| `/api/wifi` | wifi.lua | config[wifi] | AT+B RELOAD wifi |
| `/api/wifilist` | wifilist.lua | config[wifi] | AT+B RELOAD wifi |
| `/api/sip` | sip.lua | config[sip] | AT+B APPLICATION (GET), AT+B RELOAD (POST) |
| `/api/p2p` | p2p.lua | config[p2p] | AT+B CONTACTS (GET), AT+B RELOAD (POST) |
| `/api/avSetting` | avSetting.lua | config[av_setting] | AT+B RELOAD |
| `/api/video` | video.lua | config[video] | AT+B RELOAD + AT+B RELOAD himedia |
| `/api/relay` | relay.lua | config[relay] | AT+B RELOAD |
| `/api/cloudDevice` | cloudDevice.lua | config[sip,video,device_update] | AT+B RELOAD + AT+B RELOAD icloud |
| `/api/testConnect` | testConnect.lua | — | AT+B CHECKSIP 1 s=… u=… p=… |
| `/api/getConnect` | getConnect.lua | — | AT+B CHECKSIP 2 |
| `/api/getCallList` | getCallList.lua | SELECT callList+sipServer | — |
| `/api/addCallList` | addCallList.lua | INSERT callList(+sipServer) | AT+B RELOAD callList |
| `/api/updateCallList` | updateCallList.lua | UPDATE callList(+sipServer) | AT+B RELOAD callList |
| `/api/delCallList` | delCallList.lua | DELETE callList(+sipServer) | AT+B RELOAD callList |
| `/api/cleanCallList` | cleanCallList.lua | DELETE All + reseed | — (kein RELOAD!) |
| `/api/getServerList` | getServerList.lua | SELECT sipServer | — |
| `/api/updateCloudEnable` | updateCloudEnable.lua | UPDATE callList.enable | — |
| `/api/updateShareCode` | updateShareCode.lua | UPDATE callList.shareCode | — |
| `/api/elock/<action>` | elock.lua | — | AT+B ELOCK <action> |
| `/api/apk` | apk.lua | UPDATE config[wifi,cloud_account,av_link,purpose] + sipServer/callList wipe | AT+B RELOAD wifi, AT+B RELOAD address (+ `lua autoSync.lua` subprocess) |
| `/api/backup` | backup.lua | (liest avl20.db) | — (tar+`/var/www/html/`) |
| `/api/sync` | sync.lua | (überschreibt `/customer/share/`) | — (`reboot`) |
| `/api/upload` | upload.lua | — | — (`sh update.sh`) |
| `/api/test` | test.lua | — | — (`sh factory.sh`) |
| `/api/reboot` | reboot.lua | — | — (`reboot`) |

---

## 8. Cloud-API (zur Abgrenzung)

`autoSync.lua` ruft den **Cloud-Server** (nicht den GW selbst) an:
- `POST   https://<server>/api/login` — `{user_id, password, device_type=6, device_model="AVL20P", device_id=<MAC>, device_name?}`
- `GET    https://<server>/api/device?id=<MAC>` mit `Authorization: <token>` —
  liefert `dialplan, mqtt_server, sip_id, name, password, sip_server, video_url,
  update_server, video_transfer, p2p_server`.
- `PUT    https://<server>/api/device/<MAC>` — `{video_transfer}`
- `PUT    https://<server>/api/device/<MAC>/conf` — `{path:["device_button"], data:<button>}`
- `POST   https://<server>/api/v2/devices/<MAC>/keys` — `{key, single:true, bind_self}`
- `DELETE https://<server>/api/v2/devices/<MAC>/keys/0?delete_all=1`

Diese Endpoints leben in der Hersteller-Cloud, nicht auf dem GW.
