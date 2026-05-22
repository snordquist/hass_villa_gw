# TLS / Cert-Pinning auf dem Villa GW

Status: static-only (Dump-Analyse). Live-Checks (`/customer/share/ca-certificates.crt`,
laufende TLS-Sessions, OpenResty-Cosockets) sind in [`live_forensics.md`](live_forensics.md)
nachzutragen.

Diese Datei beantwortet eine einzige Frage:
**Kann der MQTT-Cloud-Traffic an einen lokalen Broker umgebogen werden, ohne dass
TLS den Switch erkennt?**

## TL;DR

| Komponente | TLS-Lib | Verify-Stand (statisch erkennbar) | Hijack-Hürde |
|---|---|---|---|
| `avlink` MQTT-Client | libmosquitto.so.1 (TLSv1.2) | cafile gesetzt → Verify aktiv (cert_reqs unklar) | **Hoch** — Cert mit gültigem CN/SAN für `de.ilifestyle-cloud.com` nötig, oder eigene CA in `/customer/share/ca-certificates.crt` einschmuggeln |
| `mimedia` RTMP/RTMPS | OpenSSL (libssl.so.1.0.0) | RTMP_TLS_Accept-Path da, aber `video.rtmp` URL ist `rtmp://…` (kein `s`) → Klartext | trivial — kein TLS aktiv |
| `pjsua` SIP-TLS | OpenSSL | Default `--tls-verify-server=no` (laut Hilfe) — Konfiguration der laufenden Instanz unbekannt | wahrscheinlich aus, aber prüfen |
| `nginx` (Web-API) | OpenResty/OpenSSL | conf.d nicht im Dump (Blob-encrypted gem. `boot_init.md`) | nicht relevant für Hijack |

**Bottom line:** Für den MQTT-Hijack-Pfad (Cloud-Broker auf den HA-Host umlenken)
müssen wir entweder

1. ein Server-Zertifikat mit gültigem Hostname `de.ilifestyle-cloud.com` (oder
   was wir per DB in `mqtt_server` schreiben) und Trust durch die ca-Bundle-Datei
   bereitstellen, **oder**
2. `/customer/share/ca-certificates.crt` auf dem GW gegen unsere eigene Root-CA
   austauschen (Hook: `/customer/demo.sh`, siehe [`boot_init.md`](boot_init.md)).

Option 2 ist robuster, weil sie unabhängig vom MQTT-Hostnamen funktioniert.

---

## avlink — die einzige Bus-Komponente mit TLS-Pinning-Relevanz

Linkage (aus `strings avlink | grep ^lib`):

```
libc.so.6
libjansson.so.4
liblua5.1.so
libmosquitto.so.1
libpthread.so.0
```

Kein direktes `libssl`/`libcrypto` — TLS läuft ausschließlich über libmosquittos
eigene Anbindung (intern via OpenSSL oder mbedTLS, je nach Build des Geräts).

### Fundstellen TLS

```
mosquitto_tls_set        → PLT @ 0x11fbc
mosquitto_tls_opts_set   → PLT @ 0x122bc
```

`.rodata`-Strings:

```
0x1fe84  "/customer/share/ca-certificates.crt"
0x1fea4  "tlsv1.2"
0x1feb4  " [%s %s:%d]: Could not set TLS configuration: %d"
0x1ff1c  " [%s %s:%d]: mqtt_client_config server=%s, username=%s"
0x1ff4c  " [%s %s:%d]: self->config.mqtt_server=%s, self->config.mac=%s, self->config.token=%s"
```

Funktionsname (aus dem Disasm-Listing der Aufrufer): `mqtt_client_main`.

### Was bedeuten die Aufrufe

`mosquitto_tls_set(mosq, cafile, capath, certfile, keyfile, pw_callback)` —
die `cafile`-Pflicht-Position wird mit `/customer/share/ca-certificates.crt`
besetzt. Eine gesetzte cafile aktiviert **per Default Server-Cert-Verify** in
libmosquitto (cert_reqs = SSL_VERIFY_PEER).

`mosquitto_tls_opts_set(mosq, cert_reqs, tls_version, ciphers)` —
das `tlsv1.2`-Literal landet in der `tls_version`-Position.
Den `cert_reqs`-Integer können wir statisch nicht zweifelsfrei lesen (Wert wird
in ein Register geschrieben kurz vor dem Aufruf). Live-Check siehe unten.

### Was statisch NICHT da ist

- **Kein Fingerprint-Pinning.** Keine `mosquitto_tls_psk_set`, kein
  `mosquitto_tls_set_certificate_verify_callback`, keine SHA-Strings, keine
  hard-coded `fingerprint`/`hash` Vergleiche im avlink-Binary.
- **Kein SNI-Override.** Wir finden keine `mosquitto_string_option`-Aufrufe.
- **Kein zweites cacert-Path.** Nur die eine Datei `/customer/share/ca-certificates.crt`.

→ Der einzige Pinning-Anker ist also die Datei `ca-certificates.crt` plus die
implizite Hostname-Prüfung von OpenSSL (Standard-libmosquitto-Verhalten:
`tls_insecure_set` defaultet auf false, d.h. Hostname wird verifiziert).

### Live-Verifikation (TODO in `live_forensics.md`)

Aus dem GW telnetten und ausführen:

```sh
ls -la /customer/share/ca-certificates.crt
wc -l /customer/share/ca-certificates.crt
md5sum /customer/share/ca-certificates.crt
# fingerprint des prod brokers ziehen:
echo | openssl s_client -connect de.ilifestyle-cloud.com:8883 -showcerts 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256 -subject -issuer
# zur Bestätigung: schlägt unser eigener Broker mit Default-CA fehl?
openssl s_client -connect 203.0.113.14:8883 -CAfile /customer/share/ca-certificates.crt
```

`md5sum`-Vergleich mit einer frischen Mozilla CA-Bundle zeigt, ob der GW
einfach das Standard-Bundle benutzt (dann ist Option 2 von oben relativ
unauffällig, weil die Originaldatei gut bekannt ist) oder ob der Vendor eine
gekürzte/getuschte Variante eingebaut hat.

---

## Hijack-Pfade

### Pfad A: DB-Switch + selbst signierte CA via Hook

**Eingriff (einmalig):**

1. eigene Root-CA generieren (z.B. `myCA.crt`), Broker-Cert mit SAN
   `de.ilifestyle-cloud.com` UND `203.0.113.14` signieren
2. `/customer/demo.sh` ergänzen:
   ```sh
   cp /customer/share/ca-certificates.crt /customer/share/ca-certificates.crt.bak
   cat /customer/myCA.crt >> /customer/share/ca-certificates.crt
   ```
3. DB-Eintrag flippen (siehe [`database.md`](database.md)):
   ```sql
   UPDATE config SET item = json_set(item, '$.mqtt_server', '203.0.113.14') WHERE name='sip';
   ```
4. `echo -ne 'AT+B RELOAD icloud\r\n' | nc 127.0.0.1 60000`

Vorteil: Cert-Pinning wird **nicht** umgangen, sondern legal erweitert. Auch
Hostname-Verify klappt, weil unser Broker auf `203.0.113.14` antwortet und das
Cert eine matching SAN hat.

Risiko: Bei einem Firmware-Upgrade wird die Datei ggf. überschrieben (siehe
[`boot_init.md`](boot_init.md) → OTA-Path). Das demo.sh-Hook muss überleben.

### Pfad B: nur DB-Switch, ohne CA-Eingriff (riskant)

Funktioniert nur wenn entweder
- `cert_reqs = 0` (SSL_VERIFY_NONE) → muss live geprüft werden, oder
- der GW akzeptiert SNI mit Default-CA (z.B. wenn `mqtt_server` der Hostname
  eines unter Public-CA signierten Hosts wäre — dann brauchten wir aber einen
  öffentlich signierten Host, kein lokales HA-Mosquitto).

Mit hoher Wahrscheinlichkeit reicht das nicht: cafile ist gesetzt, also
ist Verify aktiv → ohne passendes Cert bricht die TLS-Session.

### Pfad C: hostname-verify abschalten via libmosquitto patch

LD_PRELOAD-Shim, der `mosquitto_tls_insecure_set(mosq, true)` aufruft, sobald
ein Mosquitto-Handle erstellt wurde. Ineffizient verglichen mit Pfad A;
nicht empfohlen.

---

## Andere TLS-Konsumenten — alle uninteressant für den Bus-Use-Case

### `mimedia` (RTSP/RTMP)

Hat `RTMP_TLS_Accept`, `RTMP_Connect`, `SSL_connect` Symbole — also rtmps-fähig.
Aber `video.rtmp` Konfig ist `rtmp://rtmp.de.ilifestyle-cloud.com/live/...`
(siehe avl20.dump.sql) — kein `s`, kein TLS für den Live-Upstream. Lokal nur
unverschlüsseltes RTSP/H.264 auf Port 554.

### `pjsua` (SIP)

Kompiliert mit TLS-Support (`--tls-ca-file`, `--tls-cert-file`,
`--tls-verify-server` etc.) aber die Hilfetexte sagen `default=no` für
`--tls-verify-server` und `--tls-verify-client`. Welche Flags zur Laufzeit
gesetzt sind, hängt vom Startup-Wrapper aus `/customer/app/sbin/` ab — Telnet-Check
nachschlagen mit `ps -ef | grep pjsua` und `cat /proc/$(pidof pjsua)/cmdline`.

Für unseren HA-Use-Case unkritisch: wir kapern den **MQTT-Bus**, nicht SIP.

### `discovery` (UDP 6210 multicast)

Plain JSON, kein TLS — siehe [`small_daemons.md`](small_daemons.md).

### `nginx` (Web-API auf 80/443)

Configs liegen verschlüsselt in `/etc/nginx/conf.d/` (siehe
[`boot_init.md`](boot_init.md) — encrypted blob, decrypted at boot). Das ist
für den MQTT-Hijack irrelevant, weil avlink die DB direkt liest und nicht via
HTTP-API kommuniziert.

---

## Offene Punkte für `live_forensics.md`

- [ ] `md5sum /customer/share/ca-certificates.crt` vs Mozilla baseline
- [ ] `openssl s_client` Fingerprint von prod-Broker speichern (für Vergleich)
- [ ] `lsof -p $(pidof avlink) | grep ca-cert` — bestätigt nur diese eine Datei
- [ ] `strace -f -e trace=open,connect -p $(pidof avlink)` 5s lang → sehen ob
      andere Cert-Pfade gelesen werden
- [ ] Live-Probe: mosquitto_sub gegen `127.0.0.1:8883` mit gespoofter CA → ob avlink
      sich connectet
- [ ] `cat /proc/$(pidof pjsua)/cmdline` — finale TLS-Flags von SIP

Verwandt: [[boot_init]] (Persistenz für CA-Tausch), [[database]] (mqtt_server-Switch),
[[avlink]] (mqtt_client_main Aufrufkette), [[security_audit]] (Pinning-Erkenntnisse
in Security-Bericht aufnehmen).
