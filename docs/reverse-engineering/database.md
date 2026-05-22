# avl20.db — Forensische Analyse

**Quelle:** `/customer/share/avl20.db` (SQLite 3) auf Villa GW V3.0 (AVL20-P)
**Dump-Stand:** 2026-05-22 — produktive Daten eines real registrierten Gateways.
**Engine:** SQLite ohne Foreign-Keys, ohne Trigger, ohne Views.
**Größe:** 5 User-Tables (`config`, `user`, `sipServer`, `callList` + `sqlite_sequence`).

## TL;DR — Verdikt zum „Config-Store"-Review

Der frühere Reviewer hatte fast recht — **aber nicht ganz**. Die Datenbank ist primär ein Config-Store, aber:

1. Sie enthält **Klartext-Credentials** (JWT-Token, SIP-Passwort, Cloud-Account-Passwort).
2. Sie hostet den **kompletten Cloud-Endpoint-Bindung** in editierbaren JSON-Blobs — kein DNS-Override nötig, ein simples `UPDATE` genügt.
3. Bei einigen Keys (`callList`, `wifi`, `av_link`, `icloud`) löst nur ein nachgeschaltetes `AT+B RELOAD` über `127.0.0.1:60000`/`:10086` das tatsächliche Live-Update aus — die DB ist also kein Hot-Reload-Store.

**Empfohlener Hijack-Pfad:** `UPDATE` auf `config.sip.mqtt_server` + `AT+B RELOAD` (siehe Abschnitt 5).

---

## 1. Schema-Übersicht

| Tabelle | Zeilen | Zweck |
|---|---|---|
| `config` | 13 | Key/Value-JSON-Store für alle Daemon-Konfigurationen |
| `user` | 3 | Lokale REST/Web-Login-Accounts (Klartext-Passwörter) |
| `sipServer` | 0 | Phonebook-Sub-Tabelle für externe SIP-Server (Multi-Provider) |
| `callList` | 1 | Phonebook: Bus-/IP-/SIP-Adressen der erreichbaren Gegenstellen |
| `sqlite_sequence` | 1 | SQLite-interner AUTOINCREMENT-Counter |

Die Original-Spalten-Kommentare in `callList` sind chinesisch. Übersetzung:

```sql
CREATE TABLE callList(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  callNo    TEXT,         -- Anruf-Nummer (Sprechstellen-ID, dial number)
  name      TEXT,         -- Anzeigename
  address   TEXT,         -- Postalische Adresse (Free-Text)
  key       TEXT UNIQUE,  -- Bus-Adresse (eindeutiger Identifier, z.B. "2" = Innenstation 2)
  userType  INTEGER,      -- 0=normaler User, 1=Türstation, 2=Wachstation
  callType  INTEGER,      -- Netzwerk-Adresstyp: 0=bus, 1=IP, 2=SIP
  ipAddr    TEXT,         -- IP-Adressliste (bei callType=1)
  serverId  TEXT,         -- FK auf sipServer.id (bei callType=2)
  enable    INTEGER,      -- 1=aktiv, 0=deaktiviert
  shareCode TEXT          -- QR-Code / Share-Token für Pairing
);
```

---

## 2. Table-Details

### 2.1 `config` — Key/Value-Store (13 Keys)

Format: `name TEXT` (Key) + `item TEXT` (JSON-encoded Value). UPDATE per Lua-Handler unter `/customer/lua/*.lua`.

| Key | REST-Handler | Beispiel-Inhalt | Wirkung |
|---|---|---|---|
| `device` | `device.lua` | `{"type":"AVL10","address":"","id":"001010001","name":"","language":"en"}` | Device-Identität, Sprache, ID. Wird auch beim Booten zum Setzen von Hostname/MAC herangezogen. |
| `sip` | `sip.lua` + `cloudDevice.lua` | `{"password":"REDACTED_SIP_PW","mqtt_server":"de.ilifestyle-cloud.com","nickname":"Villa GW","server":"de.ilifestyle-cloud.com","name":"s00c0000DEVICE_ID","contact":"000000"}` | **Kritisch.** SIP-Account UND MQTT-Endpoint in einem JSON. `server`/`name`/`password` = SIP-Cloud-Login. `mqtt_server` = MQTT-Broker-Hostname (Port 1883 hardcoded in `avlink`). `nickname` = vom User vergebener Geräte-Name. |
| `p2p` | `p2p.lua` | `[]` | P2P-Helper-Server-Liste (leer im Default). |
| `wifi` | `wifi.lua`, `wifilist.lua` | `{"enable":false,"ssid":"","password":""}` | WLAN-Zugangsdaten, Klartext. |
| `video` | `video.lua` + `cloudDevice.lua` | `{"enable":true,"rtsp":"rtsp://%s/live.sdp","rtmp":"rtmp://rtmp.de.ilifestyle-cloud.com/live/RTMP_STREAM_KEY_REDACTED","p2p_server":"p2p.de.ilifestyle-cloud.com","transfer":1}` | RTSP-Template (`%s` = lokale IP), **RTMP-Upload-URL mit Push-Token im Pfad**, P2P-Server, Transfer-Modus (1=P2P, 2=RTMP-Push). |
| `relay` | `relay.lua` | `{"duration_1":3, "duration_2":3}` | Türöffner-Relay-Zeiten (Sekunden) für E-Lock 1 / E-Lock 2. |
| `parameter` | `parameter.lua` | `{"ringtime":60,"elock_holdtime":3,"volume_ring":6,"volume_voice":6}` | Klingel-Dauer, E-Lock-Hold, Lautstärken (0–10). |
| `cloud_account` | `account.lua` + `autoSync.lua` | `{"password":"REDACTED_CLOUD_PW","token":"eyJhbGciOi...","name":"Villa GW","server":"de.ilifestyle-cloud.com","account":"REDACTED@example.com"}` | **Sehr kritisch.** Cloud-Account-Klartext-Passwort + langlebiges **JWT** für die ilifestyle-Cloud-API. |
| `device_update` | `cloudDevice.lua` | `{"update_server":"c1.ilifestyle-cloud.com"}` | OTA-Update-Server. **Hijack-Vektor für Firmware-Replacement.** |
| `key_setting` | `userKey.lua` | `{}` | Custom Key-Bindings für die Hardware-Taster (Default leer). |
| `av_link` | `address.lua` | `{"button":"2"}` | Default-Bus-Adresse, die beim Drücken der Klingel-Taste gerufen wird. Triggert `AT+B RELOAD address`. |
| `datetime` | `device.lua` | `{"auto":1,"timezone":"Europe/Berlin"}` | NTP-Auto / TZ. `auto=1` → `ntpdate pool.ntp.org`. |
| `av_setting` | `avSetting.lua` | `{"audio_format":"G.711","video_transmission":1,"video_format":"H.264","transmission_pto":1}` | Audio-/Video-Codec + Transport-Profil. |
| `purpose` | `purpose.lua` + `autoSync.lua` | `{"state":0,"purpose":0,"bindSelf":1}` | Gerätezweck (0=Default Villa-GW). `bindSelf=1` heißt: bei Boot Self-Binding gegen Cloud durchführen. |

### 2.2 `user` — REST-/Web-Auth-Accounts

Drei hartcodierte Accounts, **Passwörter im Klartext**:

| name | password | grp | Rolle |
|---|---|---|---|
| `superadmin` | `super1314` | `0` | Vollzugriff (Hersteller-Master) |
| `admin` | `admin` | `0` | Admin (Default-Werkspasswort!) |
| `device` | `device` | `1` | Eingeschränkter Device-Account (Gruppe 1) |

`login.lua` liest aus dieser Tabelle (`SELECT … FROM user WHERE name=? AND password=?`). Group-ID-Check in jedem Handler: `if 0 ~= ngx.ctx.group then return -2 end` — heißt: alles außer Group `0` darf nur lesen. **Schreibender Vollzugriff schon mit `admin/admin` möglich**, solange das Default-PW nicht geändert wurde.

### 2.3 `sipServer` — externe SIP-Provider

Leer im Default-Dump (es gibt keinen Drittanbieter-SIP-Account). Wird gefüllt sobald in `callList` Einträge mit `callType=2` (SIP) angelegt werden. Pro `callList.serverId` ein Eintrag.

| Spalte | Bedeutung |
|---|---|
| `id` | PK / FK-Target für `callList.serverId` |
| `server` | SIP-Domain/IP |
| `account` | SIP-User-ID |
| `password` | **Klartext-SIP-Passwort** |
| `callee` | Calle-To (Standard-Rufziel auf diesem Server) |

`addCallList.lua` legt einen Server-Eintrag bei Bedarf an; `cleanCallList.lua` löscht alles und setzt die AUTOINCREMENT-Sequenz auf 0 zurück.

### 2.4 `callList` — Phonebook

Default-Inhalt (1 Eintrag):

```sql
INSERT INTO callList VALUES(1,'1','','','2',NULL,0,NULL,NULL,1,NULL);
--                            id,callNo,name,addr,key,uType,cType,ip,srv,en,share
```

Heißt: ein Bus-Eintrag mit Bus-Adresse `"2"`, `callType=0` (=Bus), `enable=1`. `userType` ist `NULL` (im Schema 0=User/1=Türstation/2=Wache).

**Bus-Adresse-Mapping:**
`callList.key` wird im Daemon `avlink` direkt in die SIP-URI eingesetzt: `<sip:bus%d@…>` (siehe Binärsuche: `<sip:bus%d@` ist im `avlink` als Format-String hinterlegt). Heißt: die Bus-Hausstation mit Key `2` wird intern als `sip:bus2@<gateway>` adressiert — auch bei reinem Bus-Call läuft alles durch den lokalen PJSIP-Stack.

### 2.5 `sqlite_sequence`

SQLite-Verwaltungstabelle; nach `DELETE` wird die `seq` per Hand auf 0 zurückgesetzt (siehe `cleanCallList.lua`), damit der nächste `INSERT` wieder bei id=1 startet.

---

## 3. `AT+B RELOAD`-Targets — Mapping DB → Daemon

Der UART-Daemon `uart2d` (Listener-Ports `127.0.0.1:10086` und `:60000`) ist der einzige Konsument der DB. `RELOAD`-Subkommandos triggern selektives Re-Reading (siehe `custode2.lua:586–631`):

| AT+B-Kommando | Re-liest aus DB | Action | Aufrufer |
|---|---|---|---|
| `AT+B RELOAD` (global) | gesamte `config`-Tabelle | Soft-Reload aller Daemons via IPC | `sip.lua`, `relay.lua`, `avSetting.lua`, `userKey.lua`, `account.lua`, `purpose.lua`, `device.lua`, `video.lua`, `parameter.lua`, `p2p.lua`, `cloudDevice.lua` |
| `AT+B RELOAD wifi` | `config[name='wifi']` | `killall uard2d mimedia` + `check_mode_2()` (Re-Connect wpa_supplicant) | `wifi.lua`, `wifilist.lua`, `apk.lua` |
| `AT+B RELOAD himedia` | `config[name='video']` | `media_reload()` — RTSP/RTMP-Pipeline neustart | `video.lua` |
| `AT+B RELOAD address` | `config[name='av_link']` + `callList` | `killall uart2d` + `uart2d &` — Bus-Phonebook neu laden | `address.lua`, `apk.lua`, **`callList`-Update via `addCallList.lua`/`updateCallList.lua`/`delCallList.lua` (die nutzen `RELOAD callList` was im Handler nicht existiert → fällt durch zu `AT+B RELOAD address` Pfad? siehe ⚠ unten)** |
| `AT+B RELOAD NTP` | `config[name='datetime']` | `cp zoneinfo/<tz> /customer/localtime` + `ntpdate pool.ntp.org` | `device.lua` |
| `AT+B RELOAD icloud` | `config[name='sip']` + `config[name='video']` + `config[name='cloud_account']` | Setzt `R.p2p.reload = true` → `p2p_reload()` in `custode2.lua:999` → MQTT-Client + P2P-Tunnel werden neu verbunden | `cloudDevice.lua`, `autoSync.lua` |
| `AT+B RELOAD eth0 start` | — | Bringt `wlan0` runter, `eth0` hoch | `network.lua` |
| `AT+B RELOAD eth0 stop` | — | `/etc/init.d/networking restart` | `network.lua` |
| `AT+B RELOAD network` | — | Generisches Netz-Reload | `network.lua` |

⚠ **`AT+B RELOAD callList`** wird von 3 Lua-Skripten gesendet (`addCallList.lua:83`, `updateCallList.lua:90`, `delCallList.lua:43`), **ist aber im `custode2.lua`-Handler nicht implementiert**. Vermutung: das Kommando wird vom anderen Daemon (`uart2d` direkt) konsumiert — möglich ist auch dass es schweigend ignoriert wird und der nächste reguläre DB-Read es eh aufgreift. Quirk bei Hijack-Versuchen beachten: nach Phonebook-Änderung evtl. `AT+B RELOAD address` (das auf der Konsumenten-Seite definitiv `uart2d` neustartet) zur Sicherheit hinterher schicken.

| `RELOAD wifi` → liest neu | `RELOAD address` → liest neu | `RELOAD icloud` → liest neu |
|---|---|---|
| `config[wifi].ssid`, `.password`, `.enable` | `config[av_link]`, `callList.*`, `sipServer.*` | `config[sip]`, `config[video]`, `config[cloud_account]`, `config[device_update]` |

---

## 4. Security — Klartext-Secrets

Vier sicherheitsrelevante Stellen, alle ungeschützt:

### 4.1 JWT-Token (Cloud-Auth)

```
INSERT INTO config VALUES('cloud_account',
'{"password":"REDACTED_CLOUD_PW",
  "token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.
           eyJhcHAiOjEsInVpZCI6InUwMGMwMDAwMDAwMDIyY2QiLCJ1Z3AiOjQsImRpZCI6IkE4QjU4RTg1MzU2RSIsImRtZCI6IkFWTDIwUCIsImR0cCI6NiwiaWF0IjoxNzc5MzY4MjI3fQ.
           AG_buL-q0mOy2g7hlclzP3YUJ4iB9NafNcOzsGPHNbw",
  …}')
```

Decode payload: `{"app":1,"uid":"u00c0000000022cd","ugp":4,"did":"AA:BB:CC:DD:EE:FF","dmd":"AVL20P","dtp":6,"iat":1779368227}`
**Kein `exp`-Claim** → Token ist ewig gültig bis Server-seitig revoked. Algo `HS256` — wer den Server-Secret kennt, kann beliebige Tokens fälschen.

### 4.2 Cloud-Account-Passwort: `REDACTED_CLOUD_PW` (Klartext).

### 4.3 SIP-Passwort: `REDACTED_SIP_PW` (Klartext im `sip`-Blob).

### 4.4 Lokale Web-Logins:

`superadmin/super1314` und `admin/admin` (Werks-Default, nicht gehasht). Wer Shell-Zugriff auf das Gerät hat, kann mit einem einzigen `sqlite3` alle Web-Passwörter rotieren — und wer Web-Zugriff hat, kann genauso die DB indirekt ändern.

**Backup-Quirk:** `backup.lua` packt die komplette `avl20.db` als `.tar.bz2` ins `/var/www/html/`-Verzeichnis (web-erreichbar!). D.h. wenn die Backup-Funktion missbraucht werden kann, sind alle Secrets via HTTP-Download abgreifbar.

---

## 5. MQTT-Hijack-Pfad — konkrete Anleitung

`avlink` (Binary) liest beim Start `config[sip].mqtt_server` und öffnet TCP/1883 dorthin (verifiziert per `strings`: `self->config.mqtt_server=%s` und `mqtt port=1883 socket=%d`). Kein TLS-Default, Port hardcoded.

**Ziel:** MQTT auf `203.0.113.14` (Home-Assistant-Broker) umlenken.

```sql
-- aktuellen sip-Blob holen
SELECT item FROM config WHERE name='sip';
-- ergibt JSON, mqtt_server-Feld ersetzen:
UPDATE config
   SET item = json_set(item, '$.mqtt_server', '203.0.113.14')
 WHERE name = 'sip';
```

Falls `json_set()` in dieser SQLite-Build nicht verfügbar ist (älteres SQLite ohne JSON1), Brute-Force-Variante:

```sql
UPDATE config
   SET item = '{"password":"REDACTED_SIP_PW","mqtt_server":"203.0.113.14","nickname":"Villa GW","server":"de.ilifestyle-cloud.com","name":"s00c0000DEVICE_ID","contact":"000000"}'
 WHERE name = 'sip';
```

Wichtig: `server` (SIP-Server) **nicht mitändern**, sonst bricht die Cloud-Telefonie. Nur `mqtt_server`!

Danach Reload triggern (eine der beiden Varianten — `RELOAD icloud` ist die korrektere weil sie genau `sip+video+cloud_account` re-liest):

```bash
echo -ne 'AT+B RELOAD icloud\r\n' | nc 127.0.0.1 60000
```

Oder via HTTP (wenn Web aktiv): `POST /api/cloudDevice` mit unveränderten Werten triggert intern beide RELOADs (`AT+B RELOAD` global + `AT+B RELOAD icloud`, siehe `cloudDevice.lua:76,89`).

**Risiko:** der `discovery_config`-Pfad in `avlink` publiziert MQTT-Discovery-Configs an den Broker (siehe Strings `update_mqtt_discovery_client_config` und `mqtt_client_config server=%s, username=%s`). Heißt: bei Hijack auf HA-Broker bekommt Home Assistant automatisch eine MQTT-Auto-Discovery — bevor die ACL die unauthentifizierten Topics blockt. **Pre-Auth via ACL einrichten** oder Broker mit User/Pass absichern (`config[sip]` hat kein MQTT-User-Feld — der Client connectet anonym, also Auth muss broker-seitig erzwungen werden).

---

## 6. DNS-Hijack-Pfad (Alternative)

Da alle Cloud-Endpoints als **Hostnamen** in der DB stehen, ist DNS-Hijack möglich, aber unnötig kompliziert. Betroffene Felder:

| DB-Stelle | Wert | Genutzt von |
|---|---|---|
| `config[sip].server` | `de.ilifestyle-cloud.com` | SIP-REGISTER (UDP/TCP 5060) |
| `config[sip].mqtt_server` | `de.ilifestyle-cloud.com` | MQTT-Connect (1883) |
| `config[cloud_account].server` | `de.ilifestyle-cloud.com` | REST-API für Auto-Sync / Phonebook-Pull |
| `config[video].rtmp` | `rtmp://rtmp.de.ilifestyle-cloud.com/...` | RTMP-Push für Live-Video |
| `config[video].p2p_server` | `p2p.de.ilifestyle-cloud.com` | P2P-Tunnel-Helper |
| `config[device_update].update_server` | `c1.ilifestyle-cloud.com` | OTA-Firmware-Download |

**Per-Feld-DB-Update ist sauberer als DNS-Hijack**, weil:

- DNS-Hijack braucht entweder einen kontrollierten Resolver am Gateway (FRITZ!Box) oder einen `/etc/hosts`-Override am Gerät selbst.
- DB-Update wirkt nur auf dieses eine Gerät, ohne kollaterale Auswirkungen auf andere Clients im LAN.

---

## 7. Hidden / Future-Use Tables

Keine echten Hidden-Tables. Folgende Felder/Keys deuten auf ungenutzte oder geplante Features:

- **`callList.shareCode`** — QR-Code-Pairing. Default `NULL`, nur über `updateShareCode.lua` befüllbar. Ist offenbar für eine geplante App-Pairing-Funktion vorgesehen, die im aktuellen Firmware-Stand kaum genutzt wird.
- **`config[key_setting]`** — Custom-Hardware-Taster-Bindings (default `{}`); im Customer-Image keine UI dafür.
- **`config[p2p]`** — Array-Typ (`[]`), separat vom `video.p2p_server`-Endpoint. Vermutlich für eine zweite P2P-Server-Liste mit Failover; im Default leer.
- **`config[purpose].state`/`bindSelf`** — `state=0` ist „uninitialized", `bindSelf=1` triggert beim Boot ein Auto-Re-Pairing gegen die Cloud (`autoSync.lua:421`). Wer das auf `bindSelf=0` setzt, **deaktiviert die ungewollte Selbst-Wiederherstellung** der Cloud-Bindung nach einem Faktory-Reset — relevant um den Hijack persistent zu halten.
- **`device_update`-Key** — wird in `cloudDevice.lua:53–63` erst **erzeugt wenn er fehlt** (`INSERT INTO 'config' VALUES('device_update', …)`). D.h. die Tabelle ist Schema-frei was die Key-Liste angeht — beliebige Custom-Keys dürften technisch insertbar sein, werden aber nur konsumiert wenn ein Daemon den Key-Namen kennt.

`sqlite_sequence` ist kein Hidden-Table sondern SQLite-Standard.

---

## 8. Empfehlung — sicherster Hijack-Pfad

| Pfad | Aufwand | Reversibel | Persistenz | Risiko |
|---|---|---|---|---|
| **DB-`UPDATE` + RELOAD icloud** auf `sip.mqtt_server` | **niedrig** | ja (UPDATE zurück) | überlebt Reboot, **nicht** Factory-Reset (siehe `avlink`-String mit `sqlite3 … < avl20.sql`) | sehr niedrig |
| DNS-Hijack via FRITZ!Box | mittel | ja | überlebt Reboot + Factory-Reset (am Gateway-Router) | mittel — wirkt LAN-weit |
| `/etc/hosts`-Override am Gerät | mittel | ja | überlebt Reboot, **nicht** Firmware-Update | niedrig |
| MITM-TCP-Redirect via Switch-VLAN | hoch | ja | switch-konfiguration-abhängig | mittel |

**Klare Empfehlung: DB-`UPDATE` auf `config[sip].mqtt_server`** in Kombination mit `AT+B RELOAD icloud`. Begründung:

1. **Atomar**: einzige Zeile, einzige Tabelle, einziges Feld.
2. **Daemon-getrieben**: der Reload-Mechanismus existiert genau für diesen Zweck — wir nutzen den Hersteller-Pfad, kein Hack.
3. **Reversibel** durch zweites UPDATE zurück auf `de.ilifestyle-cloud.com`.
4. **Kein Einfluss** auf SIP, RTMP, P2P, OTA (die haben separate Felder).
5. **HA-MQTT-Auto-Discovery profitiert** — Gerät meldet sich von selbst beim HA-Broker an.

**Factory-Reset-Caveat:** in `avlink` gefunden:
```
killall uart2d; killall lua; sleep 0.5;
sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql;
…
reboot
```
heißt: die `avl20.sql` (= `avl20.dump.sql`) wird bei Factory-Reset re-importiert. Wer die Änderung wirklich permanent will, muss zusätzlich `/customer/share/avl20.sql` patchen. Andernfalls: zweimal-Reset = Cloud wieder live.

---

## 9. Quellen-Cross-Reference

| Behauptung | Quelle |
|---|---|
| `avlink` verbindet MQTT auf Port 1883 mit `mqtt_server`-Wert | `strings customer/app/sbin/avlink`: `mqtt port=1883 socket=%d`, `self->config.mqtt_server=%s` |
| SIP-URI für Bus-Calls | `strings avlink`: `<sip:bus%d@` |
| Factory-Reset re-importiert `avl20.sql` | `strings avlink`: `sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql` |
| `RELOAD icloud` setzt `R.p2p.reload = true` | `customer/app/sbin/custode2.lua:614–615` und `:999–1001` |
| Web-Backup leakt komplette DB | `customer/lua/backup.lua:10` |
| Login-Check nutzt `user`-Tabelle ohne Hash | `customer/lua/login.lua:41` |
| Group-Check `0` nötig für Schreibzugriff | jeder Handler, z.B. `wifi.lua:26` |

---

**Fazit:** Die DB ist ein Config-Store **mit ausreichend Hijack-Oberfläche**, dass externe Tools (Nmap, MITM-Proxy) für eine MQTT-/Cloud-Umlenkung nicht nötig sind. Ein SQL-Statement + ein `nc`-Aufruf reichen.
