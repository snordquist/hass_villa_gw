# Villa GW V3.0 — Filesystem & Persistenz (Reverse-Engineering)

**Gerät:** AVL20P @ `203.0.113.10` (root via Telnet, no-auth)
**Firmware:** `4.1.11` (`/etc/VERSION` = `/customer/VERSION`)
**Kernel:** `Linux 4.9.84 SMP PREEMPT armv7l` (Build 2025-05-09)
**SoC:** ARMv7 (Cortex-A7, 800 MHz, 2 cores), 89 MB RAM, kein Swap
**Stand:** 2026-05-22

---

## 1. Mount-Realität (live `mount` / `df -h`)

| Mountpoint | FS | Backend | Mode | Size | Used | Persistenz |
|---|---|---|---|---|---|---|
| `/` | **squashfs** | `/dev/root` (`mtdblock9`) | **ro,relatime** | 8.9 MB | 100% | RO — Reboot-fest, Update-fest (in `rootfs`-Image) |
| `/dev` | devtmpfs / tmpfs (mdev) | — | rw | 44.7 MB | 0 | RAM (verschwindet) |
| `/proc` | proc | — | rw | — | — | virtuell |
| `/sys` | sysfs | — | rw | — | — | virtuell |
| `/tmp` | tmpfs | RAM | rw | 44.7 MB | 0 | RAM (verschwindet) |
| `/var` | tmpfs | RAM | rw | 44.7 MB | 3.2 MB | RAM (verschwindet) |
| `/sys/kernel/debug` | debugfs | — | rw | — | — | virtuell |
| `/dev/pts` | devpts | — | rw | — | — | virtuell |
| `/config` | **ubifs** | `ubi0:miservice` (`ubi0_0`) | rw,relatime | 7.5 MB | 41% (3.1 MB) | **persistent** (Reboot-fest, Update-Risiko) |
| `/customer` | **ubifs** | `ubi0:customer` (`ubi0_1`) | rw,relatime | 55.3 MB | 64% (35.7 MB) | **persistent** (Reboot-fest, Update-Risiko) |
| `/data` | **ubifs** | `ubi0:data` (`ubi0_2`) | rw,relatime | 12.9 MB | 0% (24 KB) | **persistent**, leer ab Werk |
| `/misc` | **littlefs (lfs)** | `mtdblock9` letzte Blöcke | ro (faktisch RO im Userspace) | ~384 KB | — | persistent — kein normales Write |

Hinweis: `/misc` wird in `rcS` via `lfs` (FUSE-Variante mit `--block_size=32768 --block_count=12`) auf einem Bereich von `/dev/mtdblock9` gemountet — also _innerhalb_ der rootfs-MTD. Schreibversuch (`touch`) liefert "Read-only file system". Finger weg.

---

## 2. Partitionen

### `cat /proc/mtd`

```
mtd0: 000c0000  768K  IPL0
mtd1: 00060000  384K  IPL_CUST0
mtd2: 00060000  384K  IPL_CUST1
mtd3: 00060000  384K  UBOOT0
mtd4: 00060000  384K  UBOOT1
mtd5: 00040000  256K  ENV0
mtd6: 00500000  5M    KERNEL
mtd7: 00500000  5M    RECOVERY
mtd8: 00040000  256K  FACTORY
mtd9: 00f00000  15M   rootfs   <-- squashfs / (RO) + lfs /misc
mtd10: 00060000 384K  MISC
mtd11: 062a0000 98.8M UBI      <-- ubi0 (miservice + customer + data)
```

### `cat /proc/cmdline`

```
ubi.mtd=UBI,2048 root=/dev/mtdblock9 rootfstype=squashfs ro init=/linuxrc
LX_MEM=0xffe0000
mma_heap=mma_heap_name0,miu=0,sz=0x9E9C000
mma_heap=mma_heap_ipu,miu=0,sz=0x164000
cma=2M coherent_pool=1M
mtdparts=nand0:768k(IPL0),384k(IPL_CUST0),384k(IPL_CUST1),384k(UBOOT0),384k(UBOOT1),
         256k(ENV0),5m(KERNEL),5m(RECOVERY),256k(FACTORY),15m(rootfs),384k(MISC),-(UBI)
```

### UBI-Volumes (`/sys/class/ubi/ubi0_*/name`)

| Volume | Name | data_bytes | Mount |
|---|---|---|---|
| `ubi0_0` | `miservice` | 10.5 MB | `/config` |
| `ubi0_1` | `customer`  | 67.2 MB | `/customer` |
| `ubi0_2` | `data`      | 16.9 MB | `/data` |

Kein eMMC, kein SD: nur NAND-Flash. `/dev/mmcblk*` existiert _nicht_ (`demo.sh` lädt aber `kdrv_sdmmc.ko` für eventuell extern angeschlossene SD — Hardware-Slot vermutlich vorhanden, aber leer / inaktiv).

---

## 3. Overlay-Stack

**Es gibt _keinen_ Overlay-FS-Stack.** `/overlay` existiert nicht. Kein `overlayfs`/`aufs` im Mount-Table.

Das System lebt von zwei Tricks:

1. **Squashfs-Rootfs als RO-Basis** (`/`, `/usr`, `/lib`, `/etc`, `/bin`, `/sbin`).
2. **Symlinks ins beschreibbare `/customer`** für alle Pfade, die Anpassung brauchen:

   | Read-only Pfad | Symlink-Ziel | Was es macht |
   |---|---|---|
   | `/etc/init.d/rc.local` | `/customer/etc/init.d/rc.local` | **User-Init-Hook (siehe §7)** |
   | `/etc/init.d/2019-nCoV` | `/customer/etc/init.d/2019-nCoV` | Doorbell-App-Wrapper |
   | `/etc/init.d/discovery` | `/customer/etc/init.d/discovery` | mDNS-Discovery |
   | `/etc/init.d/networking` | `/customer/networking` | Net-Setup |
   | `/usr/sbin/avlink` | `/customer/app/sbin/avlink` | Haupt-Daemon |
   | `/usr/sbin/pjsua` | `/customer/app/sbin/pjsua` | SIP-Client |
   | `/usr/sbin/uart2d` | `/customer/app/sbin/uart2d` | UART↔TCP-Bridge |
   | `/usr/sbin/2019-nCoV` | (vermutlich) `/customer/app/sbin/2019-nCoV` | siehe ls in `app/sbin` |
   | `/usr/sbin/mimedia` | `/customer/app/sbin/mimedia` | Media-Daemon |
   | `/usr/sbin/discovery` | `/customer/app/sbin/discovery` | Discovery-Daemon |

   → Effektiv ist `/customer` ein _faux-overlay_: jedes "wichtige" Binary wird über `/usr/sbin/<name>` per Symlink aus `/customer` geladen.

3. **`/etc/profile` setzt LD_LIBRARY_PATH** auf eine `/customer`-inkludierende Liste:
   ```sh
   export LD_LIBRARY_PATH=/lib:/customer:/customer/ssh/lib:/customer/minigui/lib:/customer/minigui/ts/
   source /customer/p2p/p2p.env
   ```
   `p2p.env` erweitert `LD_LIBRARY_PATH`, `GST_PLUGIN_PATH`, `PATH` mit `/customer/p2p/{lib,bin}`.

→ **Konsequenz für Mods:** alles, was wir in `/customer/...` ablegen, ist genauso "Teil der Firmware" wie die offiziellen Binaries — weil _alle_ wichtigen Binaries dort liegen.

---

## 4. Test-Schreibmatrix (live verifiziert)

Schreibtest (`touch <pfad>/x`) und Reboot-Persistenztest (`echo > /<pfad>/_perstest.txt` → `reboot` → erneut lesen).

| Pfad | Schreibbar? | FS-Typ | Backend | Reboot-fest? | Update-fest? (§5) |
|---|---|---|---|---|---|
| `/usr/` | NEIN — `Read-only file system` | squashfs | mtd9 | n/a | n/a |
| `/usr/sbin/` | NEIN — Read-only FS | squashfs | mtd9 | n/a | n/a |
| `/usr/local/` | NEIN — Read-only FS | squashfs | mtd9 | n/a | n/a |
| `/etc/` | NEIN — Read-only FS | squashfs | mtd9 | n/a | n/a |
| `/opt/` | n/a — **existiert nicht** | — | — | — | — |
| `/overlay/` | n/a — **existiert nicht** | — | — | — | — |
| `/misc/` | NEIN — Read-only FS (lfs) | littlefs | mtd9-Bereich | n/a | n/a |
| `/tmp/` | JA | tmpfs | RAM | **NEIN** (Reboot leert) | n/a |
| `/var/` | JA | tmpfs | RAM | **NEIN** (Reboot leert) | n/a |
| `/data/` | JA | ubifs | `ubi0_2` | **JA** (live verifiziert) | wahrscheinlich JA |
| `/config/` | JA | ubifs | `ubi0_0` (`miservice`) | **JA** (live verifiziert) | **kritisch** (Kernelmodule!) |
| `/customer/` | JA | ubifs | `ubi0_1` | **JA** (live verifiziert) | **Update-überschrieben** (§5) |
| `/customer/share/` | JA | ubifs | `ubi0_1` | JA | wahrscheinlich JA — `firmware_upgrade.lua.log` zeigt Einträge ab 2024-09-25 |
| `/customer/app/` | JA | ubifs | `ubi0_1` | JA | **Update-überschrieben** (App-Binaries) |

Verifizierte Persistenz: vor dem ersten Reboot wurden `/customer/_perstest.txt` und `/data/_perstest.txt` geschrieben, vor dem zweiten Reboot `/config/_perstest.txt`. Nach jeweils einem `reboot`-Zyklus (uptime = 0:00) waren alle drei Dateien noch da, Inhalt unverändert. `/tmp/x` und `/var/x` waren _erwartungsgemäß_ weg.

---

## 5. Firmware-Update-Verhalten

### Update-Trigger (aus `firmware_upgrade.lua`)

- Lua-Daemon lauscht auf `127.0.0.1:10010` (TCP).
- Cloud sendet via `AT+B UPGRADE 3\r\n` → setzt `Parm.image_update = true`.
- `get_download_url()` ruft `http://<update_server>/download_path?product_name=AVL20P` → bekommt URL zurück.
- `dowamload_update_image()`:
  1. lädt nach `/tmp/update.tar.bz2` (tmpfs!)
  2. `cd /tmp/ && tar -xjf update.tar.bz2 && cd update && sh update.sh`

### Was `update.sh` macht

`update.sh` selbst liegt _nicht_ im Image-Dump (er ist Teil des Update-Tarballs). Aus dem Aufruf-Kontext und dem squashfs-/UBI-Layout ist aber klar:

- Das Update wird in **`/tmp/update/`** entpackt (tmpfs, Größe ≤ 44 MB → das limitiert die Update-Größe).
- `update.sh` schreibt die neuen Images via `flash_eraseall` / `nandwrite` / `ubiupdatevol` direkt auf die MTD- bzw. UBI-Volumes.
- Mindestens betroffen sind: **`mtd9` (rootfs squashfs)** und **`ubi0_1 customer`**, ggf. auch `KERNEL` (mtd6) und `ENV0` (mtd5).
- `/etc/VERSION` und `/customer/VERSION` beide auf `4.1.11` → werden _zusammen_ aktualisiert, d.h. **`/customer` wird mit-überschrieben** (sonst würden die VERSION-Files driften).

### Konsequenz: was überlebt ein Firmware-Update?

| Pfad | Update-Survival | Begründung |
|---|---|---|
| `/` (squashfs) | **NEIN** | wird komplett ersetzt |
| `/customer/app/sbin/*` | **NEIN** | App-Binaries Teil des Updates (Symlinks aus `/usr/sbin/` zeigen drauf) |
| `/customer/etc/init.d/*` | **NEIN** | `rc.local`-Setup Teil des Updates |
| `/customer/html`, `/customer/lua` | **NEIN** | Web-UI Teil des Updates |
| `/customer/p2p`, `/customer/ssh`, `/customer/wifi` | **vermutlich NEIN** | Pakete des Vendors |
| `/customer/demo.sh` | **vermutlich JA** | siehe §7 — als Vendor-Hook konzipiert, aber nicht garantiert |
| `/customer/share/avl20.db` | wahrscheinlich JA | Account/SIP-Config, würde User-Setup zerstören |
| `/customer/share/firmware_upgrade.lua.log` | JA — Log-Datei | logisch persistent |
| `/customer/config/snap.jpg` | JA | User-Daten |
| `/customer/backup/interfaces` | JA | Backup |
| `/config/*` (`miservice`) | wahrscheinlich JA | enthält Kernelmodule + Board-Config — Update könnte aber `/config/modules/4.9.84/` mit-aktualisieren wenn Kernel neuer wird |
| `/data/*` (leer) | JA | dediziertes User-Daten-Volume |
| `/misc` (lfs) | unklar | kleine Calibration-Partition — wahrscheinlich nicht überschrieben |

**Faustregel:** `/customer/` ist als "App-Partition" zu betrachten, _nicht_ als "User-Data". Ein Firmware-Update überschreibt die meisten Inhalte. Nur User-erzeugte Files (Snapshots, Configs in DB, Logs, `/customer/backup/`) bleiben.

---

## 6. Mod-Ablage-Empfehlung

Ziel: Mods (LD_PRELOAD-`.so`, strace-Binary, MQTT-Config, Wrapper-Scripts) sollen **Reboots überleben**. Firmware-Updates können wir nur eingeschränkt überleben — siehe Strategie unten.

### Wo welche Datei?

| Asset | Empfohlener Pfad | Begründung |
|---|---|---|
| **Eigene Binaries** (z.B. `strace`, `mosquitto_pub`) | `/data/bin/` | leer ab Werk, dediziertes User-Volume, größte Update-Survival-Wahrscheinlichkeit |
| **Eigene `.so`-Libs** (LD_PRELOAD-Wrapper) | `/data/lib/` | gleiche Begründung; mit `LD_LIBRARY_PATH=/data/lib:$LD_LIBRARY_PATH` aktivieren |
| **MQTT-Config** (Broker-URL, Topics, Credentials) | `/data/etc/mqtt.conf` (oder `/customer/share/mqtt.conf` wenn Daemon direkt dort schaut) | persistent, parseable |
| **Wrapper-Skripte** (Daemon-Launcher) | `/data/bin/` | nicht-prominent, nicht in `/customer/app/` |
| **Persistenter Mod-Hook** (Boot-Einsprung) | **`/customer/demo.sh`** (anhängen) ODER `/customer/etc/init.d/rc.local` (anhängen) | beide laufen bei jedem Boot — siehe §7 |
| **State / Daten** (z.B. MQTT-Cache) | `/data/var/` | persistent |
| **Logs** (eigene) | `/customer/share/` (passt zum bestehenden Vendor-Log-Stil) oder `/var/log/` (tmpfs — verschwindet) | je nach Rotation |

### Warum NICHT `/customer/...`?

`/customer/` _ist_ Teil des Firmware-Bundles. Ein Update klatscht das tar-Image drüber. Während die UBIFS-Volumes _nicht_ neu formatiert werden (`/customer/backup/` existiert noch über Versionen hinweg → starkes Indiz), werden vorhandene Dateien ersetzt und _neue_ Vendor-Dateien hinzugefügt. Mod-Files in `/customer/...` können durch Namens-Kollision mit zukünftigen Vendor-Updates kollidieren.

### Update-Survival-Strategie

Da `update.sh` nicht selbst vorliegt, ist die _einzige_ verlässliche Methode:

1. **Mods auf `/data` ablegen** (höchste Wahrscheinlichkeit, dass es überlebt).
2. **Boot-Hook in `/customer/demo.sh`** (selbst wenn `demo.sh` überschrieben wird, sieht man das beim nächsten Boot sofort — und kann es per Telnet/SSH wieder reinpatchen).
3. **Idempotente Hook-Logik:** Hook prüft `if [ -x /data/bin/our-init ]; then /data/bin/our-init; fi` — so ist `demo.sh` nur ein Mini-Trampolin, die echte Logik liegt in `/data/`.
4. **Backup von `demo.sh`** in `/data/backup/demo.sh.original` halten + Restore-Skript für nach-Update-Wiederherstellung.

---

## 7. Auto-Start-Hooks

Drei Boot-Einsprungpunkte (Reihenfolge gemäß `/etc/init.d/rcS`):

### (a) `/customer/demo.sh` — Kernel-Module + User-Hook

`rcS` Zeilen 29-31:
```sh
if [ -e /customer/demo.sh ]; then
    /customer/demo.sh
fi;
```

Aktueller Inhalt: lädt 35 Kernel-Module aus `/config/modules/4.9.84/` (USB-Storage, NFS, CIFS, FAT/VFAT/NTFS, MMC/SD, MStar Multimedia-Stack, NVP6124B-Video-Decoder). Letzte Zeile: `chmod 600 /customer/ssh/etc/*`.

**Anhängen am Ende = einfachster Hook-Punkt** — wird _vor_ rc.local ausgeführt, _nach_ dem UBIFS-Mount.

### (b) `/etc/init.d/rc.local` → `/customer/etc/init.d/rc.local`

`rcS` letzte Zeile:
```sh
exec /etc/init.d/rc.local
```

`/customer/etc/init.d/rc.local` startet die Daemons:
```sh
/etc/init.d/2019-nCoV start    # Doorbell-Hauptdaemon (siehe small_daemons.md)
/etc/init.d/monitor start
/etc/init.d/nginx start         # Web-UI
/usr/sbin/ntp start
/etc/init.d/wifi start
/etc/init.d/discovery start
pjsua &                         # SIP-Client
uart2d uart2d &                 # UART↔TCP-Bridge (ttyS1 — siehe Memory-Eintrag!)
avlink usr-log >/dev/null 2>&1 &
avlink avlink-master >/dev/null 2>&1 &
```

**Anhängen ist möglich** (Datei lebt auf UBIFS), aber riskanter als `demo.sh`: rc.local wird mit `exec` aufgerufen — eigene Logik muss vor den Daemons stehen oder im Hintergrund laufen.

### (c) `/etc/profile`

Wird bei jeder interaktiven Shell ausgeführt (Telnet, SSH-Login), _nicht_ bei Daemons. Setzt `LD_LIBRARY_PATH` und sourct `/customer/p2p/p2p.env`. Kein guter Daemon-Hook, aber für Telnet/SSH-Debugging hilfreich.

### Empfohlener Hook (für Mod-Deployment)

In `/customer/demo.sh` _am Ende_ anhängen:
```sh
# --- BEGIN MOD HOOK ---
if [ -x /data/bin/villa-mod-init ]; then
    /data/bin/villa-mod-init &
fi
# --- END MOD HOOK ---
```

`villa-mod-init` (in `/data/bin/`) setzt dann `LD_PRELOAD`, startet MQTT-Bridge, etc. — alles aus `/data`.

---

## 8. Restore / Recovery

### Bootloader & Recovery-Partition

- **mtd0** (`IPL0`, 768 KB): Initial Program Loader.
- **mtd1/mtd2** (`IPL_CUST0/1`, je 384 KB): Customer-IPL (dual-bank).
- **mtd3/mtd4** (`UBOOT0/1`, je 384 KB): U-Boot (dual-bank).
- **mtd5** (`ENV0`, 256 KB): U-Boot env (Boot-Args, Boot-Counter, evtl. Recovery-Flag).
- **mtd6** (`KERNEL`, 5 MB): Linux-Kernel (aktiv).
- **mtd7** (`RECOVERY`, 5 MB): **separates Recovery-Kernel-Image** — vermutlich abgespecktes System für Notfall-Reflash.
- **mtd8** (`FACTORY`, 256 KB): Werks-Kalibrierungsdaten (MAC?, Sensor-Cal?). _Nicht_ löschen.

### Aktivierungs-Mechanismus

`mtd1/2` und `mtd3/4` als Dual-Bank deuten auf **A/B-Boot mit Boot-Counter** in `ENV0`. U-Boot wechselt bei Watchdog-Reset / fehlschlagendem Boot zum anderen Bank-Slot. Kernel/Recovery ist _kein_ A/B (nur einzelnes `KERNEL`-Image), dafür gibt es das separate `RECOVERY`-Image (mtd7).

### Factory-Reset

Aus dem Lua-Code (`firmware_upgrade.lua`, Kommentar Zeile 168): "数据库新增字段，未恢复出厂可能取不出值" ("DB neues Feld, bei nicht-Werkseinstellung evtl. kein Wert"). Es gibt also einen offiziellen Factory-Reset-Pfad. Vermutlich:
- HTTP-API-Call (siehe `http_api.md`)
- Oder physischer Reset-Knopf am Gerät (lange drücken)
- Oder via `AT+B`-Befehl (siehe `villa_gw_internals` Memory-Eintrag, kein expliziter `RESET`-Befehl im `tcp_table` gesehen, aber `pjsua`/`uart2d` könnten welche haben)

Effekt vermutlich: `ubiformat` oder `ubirmvol`+`ubimkvol` für `customer` und `data` + Re-Population aus dem RECOVERY-Image. `miservice` (`/config`) bleibt evtl. — enthält die Kernelmodule, die ohne Factory-Reset auch nach Update funktionieren müssen.

### Bricking-Risiko + Recovery

- `rootfs` (mtd9) zerstören → Recovery-Kernel (mtd7) bootet, vermutlich mit eingebauter TFTP- oder USB-Reflash-Logik (siehe Module in `demo.sh`: USB-Storage, FAT/NTFS — der Recovery-Pfad könnte ein USB-Stick mit `update.tar.bz2` sein).
- `U-Boot` (mtd3/4) zerstören → JTAG/UART-Bootloader-Recovery nötig (kein Software-Weg).
- `/customer` zerstören → wahrscheinlich noch bootbar (rootfs ro), aber kein SIP/Web-UI — Telnet via `busybox telnetd` aus `rcS` läuft trotzdem (mit `-l sh`, no-auth).

**Wichtig für Mod-Dev:** solange Mods nur in `/customer/demo.sh` und `/data/` leben, ist das maximale Risiko ein nicht-funktionierender Daemon — niemals ein Brick. Telnet (`203.0.113.10:23`) bleibt verfügbar weil `rcS` ihn _vor_ `demo.sh` startet.

---

## 9. Zusammenfassung — Quick Reference

```
schreibbar + persistent:   /customer (Vendor-überschrieben bei Update),
                           /config (UBIFS, Module + Board-Config),
                           /data (leer, ideale Mod-Ablage)
schreibbar + flüchtig:     /tmp, /var (beide tmpfs)
RO:                        /, /usr, /etc, /usr/sbin, /usr/local, /misc
existiert nicht:           /opt, /overlay
Boot-Hook:                 /customer/demo.sh  (vor rc.local)
                           /customer/etc/init.d/rc.local  (Daemon-Start)
Wichtige Symlinks:         /etc/init.d/rc.local → /customer/etc/init.d/rc.local
                           /usr/sbin/{avlink,pjsua,uart2d,...} → /customer/app/sbin/*
LD_LIBRARY_PATH:           /lib:/customer:/customer/ssh/lib:/customer/minigui/lib:
                           /customer/minigui/ts/ + (via p2p.env) /customer/p2p/lib
```

**Mod-Deployment-Pattern:**
1. Echte Logik in `/data/bin/`, `/data/lib/`, `/data/etc/` (Update-resistent).
2. Trampolin in `/customer/demo.sh` (Append, idempotent, prüft `/data/bin/villa-mod-init`).
3. Backup von Vendor-`demo.sh` in `/data/backup/demo.sh.orig` für Post-Update-Restore.
4. Telnet (`203.0.113.10:23`, root, no-auth) ist immer als Brick-Recovery-Pfad da — `rcS` startet ihn _vor_ jedem User-Hook.
