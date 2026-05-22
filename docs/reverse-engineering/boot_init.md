# Villa GW V3.0 — Boot & Service Architecture

Reverse-engineered from the on-device filesystem dump
(`villa_gw_dump/`, extracted from a running AVL20P unit).
All paths in this document are **absolute as seen on the device**.

> **Sources analyzed**
> - `etc/init.d/rcS`, `etc/init.d/discovery`, `etc/init.d/monitor`, `etc/init.d/nginx`
> - `usr/sbin/monitor.lua`
> - `customer/app/sbin/{avlink,uart2d,mimedia,pjsua,discovery,custode2.lua}` (strings + Lua source)
> - `customer/share/firmware_upgrade.lua`
> - `etc/nginx/conf.d/*.conf` (XOR-encrypted blob, not parseable, see §7)

---

## 1. `rcS` — step-by-step

`/etc/init.d/rcS` is a flat **BusyBox-init** style boot script (no SysV `rc?.d`,
no procd, no systemd). Inittab calls `rcS` once at boot; it does not return
because line 43 is `exec /etc/init.d/rc.local`.

| # | Line | Action |
|---|------|--------|
| 1 | `mount -a` | Mount everything in `/etc/fstab` |
| 2 | `mkdir /dev/shm` + `mkdir /dev/pts` | tmpfs mount points |
| 3 | `mount devpts` | PTY layer |
| 4 | `echo /sbin/mdev > /proc/sys/kernel/hotplug` | hotplug handler = BusyBox mdev |
| 5 | `mdev -s` | populate `/dev` from sysfs |
| 6 | `sysctl -p` | apply `/etc/sysctl.conf` |
| 7 | **`telnetd -l sh`** | **Telnet server, login = raw `sh` (no auth!)** |
| 8 | `mount -t sysfs none /sys`, `tmpfs mdev /dev`, `debugfs /sys/kernel/debug` | extra mounts |
| 9 | `mount -t ubifs ubi0:miservice /config` | SigmaStar firmware partition |
|10 | `mount -t ubifs ubi0:customer /customer` | **app + config partition** |
|11 | `mount -t ubifs ubi0:data /data` | persistent user data |
|12 | `mdev -s` again | rescan after UBI mounts |
|13 | `lfs … /dev/mtdblock9 /misc` | LittleFS mount on raw NAND (calibration?) |
|14 | `mount -t devpts devpts /dev/pts` | PTY again (idempotent) |
|15 | **`busybox telnetd &`** | **second telnetd (background)** |
|16 | `if [ -e /customer/demo.sh ]; then /customer/demo.sh; fi` | optional vendor hook |
|17 | `/customer/ssh/sbin/sshd -f /customer/ssh/etc/sshd_config` | OpenSSH (vendor-bundled) |
|18 | `export LD_LIBRARY_PATH=…:/customer/ssh/lib` | for sshd libs |
|19 | `mkdir /var/empty` | sshd privsep dir |
|20 | `/etc/init.d/dropbear start` | **third SSH server** (dropbear), source not in dump |
|21 | `ifconfig lo 127.0.0.1` | loopback |
|22 | `route add default gw 192.168.1.1` | static default GW (overridden later by DHCP/custode2) |
|23 | `ifconfig eth0 up` | NIC up |
|24 | `mkdir /var/run`, `/var/lib/misc`, `touch udhcpd.leases` | runtime dirs |
|25 | `echo "kernel patch 2023-10-17"` | banner |
|26 | **`exec /etc/init.d/rc.local`** | hand off to vendor stage 2 — **not present in dump** |

### Observations
- **No `respawn` keyword anywhere.** rcS launches every daemon exactly once
  (and `&`-backgrounds them). There is no inittab-style auto-restart.
- **Three independent shells on the network** (`telnetd -l sh`, `busybox
  telnetd`, `dropbear`, plus `sshd`). All run before any network filter is
  applied. This is the chief reason the device is trivially rootable on LAN.
- The real per-daemon startup happens in `/etc/init.d/rc.local`, which is **on
  the device but not in this dump** (it lives on the `miservice` UBI volume
  and was not extracted). The init.d/{discovery,monitor,nginx} scripts are
  invoked from there.

---

## 2. Per-daemon init scripts

All three scripts (`discovery`, `monitor`, `nginx`) follow the same
**LSB-headered shell-script** template with `start|stop|restart` cases and
use BusyBox `start-stop-daemon`. They are **not** procd UCI services and
they have **no respawn loop**.

### 2a. `discovery`
```sh
start-stop-daemon -b -S -x /usr/sbin/discovery
```
- `-b` = background, `-S` = start, `-x` = executable path.
- **Important path mismatch:** the *init script* starts `/usr/sbin/discovery`,
  but the binary in the dump lives at `/customer/app/sbin/discovery`. Either
  `rc.local` symlinks it into `/usr/sbin/` at boot, or the binary is mirrored
  via `cp` from `customer/` to `/usr/sbin/` on first boot. (The avlink binary
  contains the string `/etc/init.d/discovery restart`, confirming the init.d
  path is canonical.)

### 2b. `monitor`
```sh
uptime_damemon 15.0 60 &
monitor.lua 0>/dev/null 1>/dev/null 2>&1 &
```
- Starts **two** processes:
  1. `uptime_damemon` — vendor-internal hardware watchdog kicker (binary
     present at `/usr/sbin/uptime_damemon` based on PATH, not in dump).
     Args `15.0 60` likely = watchdog interval / timeout in seconds.
  2. `monitor.lua` — see §4.
- Stop branch is `start-stop-daemon -K -p $PIDFILE`, but `start` is
  commented out from writing the PID — the PIDFILE never exists, so `stop`
  is a no-op. (Comments in the script preserve the dead code.)

### 2c. `nginx`
```sh
start-stop-daemon -S -x /usr/local/sbin/nginx
```
- Plain nginx start, no `-b` (nginx daemonises itself).
- Master nginx pidfile lives at `/usr/local/nginx/logs/nginx.pid`
  (referenced by `monitor.lua`, see §4).

---

## 3. Who starts what

Daemon launches deduced from rcS, init.d scripts, lua sources, and binary
strings:

| Component | Type | Launched by | How |
|-----------|------|-------------|-----|
| `telnetd` (2x) | Shell server | `rcS` | inline `&` + `-l sh` |
| `sshd` (OpenSSH) | Shell server | `rcS` | inline `/customer/ssh/sbin/sshd` |
| `dropbear` | Shell server | `rcS` | `/etc/init.d/dropbear start` |
| `mdev` | hotplug | `rcS` | one-shot + as hotplug handler |
| `nginx` | HTTP server | `rc.local` → `init.d/nginx` | `start-stop-daemon -S` |
| `discovery` | LAN/MQTT bridge | `rc.local` → `init.d/discovery` | `start-stop-daemon -b -S` |
| `monitor.lua` | log rotator / nginx supervisor | `rc.local` → `init.d/monitor` | direct `&` |
| `uptime_damemon` | HW watchdog kicker | `init.d/monitor` | direct `&` |
| `custode2.lua` | network/wifi/p2p orchestrator | `rc.local` (not in dump) | direct lua invocation |
| `avlink` | SIP+MQTT app controller | `rc.local` (not in dump) | bare exec |
| `uart2d` | RS-485/UART bus daemon | `rc.local` first time; then **`custode2.lua`** restarts it (`uart2d uart2d &`) | via `os.execute()` in Lua |
| `mimedia` | RTSP/RTP video pipeline | **`custode2.lua`** (`mimedia master &`) | on-demand, restarted on `AT+B RELOAD himedia` |
| `pjsua` | SIP UA (PJSIP-based) | **`avlink`** itself (`killall pjsua; pjsua &`) | child of avlink |
| `media-server` (P2P) | external P2P relay client | **`custode2.lua`** | `start-stop-daemon -S $p2p_server -x media-server &` |
| `firmware_upgrade.lua` | OTA worker | **`avlink`** (`lua /customer/share/firmware_upgrade.lua &`) | spawned on update trigger from MQTT/REST |
| `voip-server` | legacy (?) SIP server | **referenced only** in `monitor.lua` (dead code, commented out) | n/a, deprecated |
| `wpa_supplicant` | WiFi STA | `custode2.lua` | `wpa_supplicant -B -i wlan0 -c …` |
| `udhcpc` / `udhcpd` | DHCP | `custode2.lua` | per interface |

### Process tree (typical runtime)

```
init (BusyBox)
└─ /etc/init.d/rcS
   ├─ telnetd (sh, no auth)
   ├─ telnetd (busybox, &)
   ├─ sshd (OpenSSH, /customer/ssh)
   ├─ dropbear
   └─ /etc/init.d/rc.local      [stage 2, NOT IN DUMP]
      ├─ /etc/init.d/nginx start
      │  └─ nginx master
      │     └─ nginx worker(s)
      ├─ /etc/init.d/discovery start
      │  └─ /usr/sbin/discovery        (UDP 239.255.255.240:6210 + unix:/var/run/discovery.socket)
      ├─ /etc/init.d/monitor start
      │  ├─ uptime_damemon 15.0 60     (kicks HW watchdog)
      │  └─ monitor.lua                (log rotation, nginx -USR1)
      ├─ lua /customer/app/sbin/custode2.lua  &
      │  ├─ wpa_supplicant -B
      │  ├─ udhcpc -i ethX
      │  ├─ uart2d uart2d                      (restartable)
      │  ├─ mimedia master                     (restartable; RTSP)
      │  └─ start-stop-daemon -S … -x media-server  (P2P)
      └─ avlink                                 (MQTT 1883 + IPC sockets)
         ├─ pjsua                              (SIP UA, restartable)
         └─ lua firmware_upgrade.lua &         (on OTA trigger only)
```

---

## 4. Watchdog & respawn

There is **no `respawn` mechanism in the BusyBox-init style**. The
recovery story is layered:

1. **Hardware watchdog**: `uptime_damemon` (kicks `/dev/watchdog`).
   If the user-space init or kernel hangs, the SoC watchdog will reset the
   board after ~60 s — **brute-force reboot**, not per-daemon respawn.

2. **`monitor.lua` (`/usr/sbin/monitor.lua`)**:
   - Rotates `/usr/local/nginx/logs/{access,error}.log` when they exceed
     512 KB, then sends `kill -USR1 $(cat /usr/local/nginx/logs/nginx.pid)`
     to nginx (graceful log re-open).
   - Has **commented-out code** to restart `voip-server` if its RSS exceeds
     3.9× its baseline (`voip_monitor()` reads `/proc/$pid/stat`). This was
     the original watchdog mechanism but is disabled in current firmware.
   - No respawn of avlink/uart2d/mimedia/pjsua here.

3. **`avlink` self-supervision** (binary strings):
   - Embeds the literal `killall pjsua; pjsua &` — avlink restarts pjsua
     when registration / RTP fails.
   - Spawns `lua /customer/share/firmware_upgrade.lua &` only when OTA
     requested.
   - Calls `/etc/init.d/discovery restart` itself when MQTT topology
     changes.

4. **`custode2.lua` (network event loop)** keeps daemons alive *only on
   network state transitions*:
   - When link comes up: `media_start()` → `mimedia master &` + p2p.
   - When link goes down: `media_stop()` → `killall mimedia`.
   - On `AT+B RELOAD himedia`: `media_reload()` (stop + sleep 3 + start).
   - On `AT+B RELOAD address`: `killall uart2d uard2d; uart2d uart2d &`.
   - On `AT+B RELOAD wifi`: `killall uard2d uard2d; killall mimedia`.

5. **Hard factory reset** (string embedded verbatim in avlink):
   ```
   killall uart2d; killall lua; sleep 0.5;
   sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql;
   cp /customer/share/zoneinfo/Europe/Berlin /customer/localtime;
   cp /customer/backup/interfaces /customer/interfaces;
   sync;sync;sync;sync;sync;sync; reboot
   ```
   This is the only path that touches `/customer/backup/interfaces` —
   confirming the device ships from the factory pre-localized to Berlin
   (Systec).

### TL;DR
There is **no daemon respawn**. If avlink, mimedia, or uart2d crash, they
stay dead until either (a) `custode2.lua` observes a network event and
restarts them, (b) avlink restarts its own children, or (c) the HW
watchdog reboots the whole box.

---

## 5. Persistence

| Path | Backing store | Survives reboot? | Survives OTA? |
|------|---------------|------------------|---------------|
| `/` (rootfs) | squashfs in `miservice` UBI | yes | yes (rewritten by `update.sh`) |
| `/etc/`, `/usr/`, `/bin/`, `/sbin/` | rootfs | yes | yes (rewritten) |
| `/customer/` | UBI vol `customer` | **yes** | partially — `update.sh` typically rewrites `/customer/app/` and `/customer/share/` but spares `/customer/share/avl20.db` |
| `/customer/share/avl20.db` | UBI (customer) | **yes** | yes (config DB — wiped only on factory reset) |
| `/customer/app/sbin/{avlink,uart2d,mimedia,pjsua}` | UBI (customer) | yes | **rewritten** by OTA |
| `/customer/lua/*.lua` | UBI (customer) | yes | rewritten by OTA |
| `/customer/ssh/`, `/customer/wifi/` | UBI (customer) | yes | usually preserved |
| `/customer/backup/interfaces` | UBI (customer) | yes | preserved (factory-reset source) |
| `/customer/mac`, `/customer/localtime` | UBI (customer) | yes | preserved |
| `/data/` | UBI vol `data` | **yes** | yes (intended for user data; appears empty in current build) |
| `/config/` | UBI vol `miservice` | yes | yes |
| `/misc/` | LittleFS on raw `/dev/mtdblock9` | yes | yes (calibration, not touched by OTA) |
| `/tmp/` | tmpfs | **no** | no |
| `/var/`, `/var/run/`, `/var/log/` | tmpfs | **no** | no |
| `/dev/shm/`, `/dev/pts/` | tmpfs / devpts | no | no |

**Rule of thumb:** anything not under `/customer/`, `/data/`, `/config/`,
or `/misc/` is gone after reboot.

---

## 6. Modification points (hook a wrapper / LD_PRELOAD splitter)

If we want to insert a custom daemon, MITM-shim, or `LD_PRELOAD` between
the existing services, the order of preference is:

### Best: 6a. Patch `/etc/init.d/rc.local`
- Single point that launches all five vendor daemons after rcS.
- **Caveat: not in the dump.** Lives on `miservice` UBI. You will need
  telnet/SSH and `cat /etc/init.d/rc.local` from a live device, then
  re-flash via OTA tarball or in-place edit on the writable UBI.
- Add lines *before* the daemon launches:
  ```sh
  export LD_PRELOAD=/customer/local/lib/avl_splitter.so
  export AVL_SPLIT_TARGET=203.0.113.140:5060
  ```

### Good: 6b. Wrap individual init.d scripts
- `etc/init.d/discovery`, `…/nginx` are easy to edit (they live on the
  squashfs rootfs in `miservice` — writable via UBI mount in rcS).
- Risk: a firmware update **will overwrite** them. Make changes idempotent
  and prepare a post-OTA re-apply path (e.g. add a hook to `/customer/demo.sh`,
  which rcS line 30 sources unconditionally if present).

### Great: 6c. **`/customer/demo.sh`** (officially supported escape hatch!)
- `rcS` line 29–31:
  ```sh
  if [ -e /customer/demo.sh ]; then
      /customer/demo.sh
  fi
  ```
- This file is **persistent** (on the `customer` UBI), **executed at every
  boot**, runs as root, and is **not touched by OTA** unless the OTA
  payload explicitly removes it.
- **This is the canonical injection point for a wrapper daemon.**

### Alternative: 6d. Replace a binary in `/customer/app/sbin/`
- E.g. rename `pjsua` → `pjsua.real`, drop a shell wrapper at `pjsua`
  that `exec`s with `LD_PRELOAD=…`. avlink calls `pjsua &` via
  `os.execute`, so a shell wrapper works transparently.
- Same OTA-overwrite caveat as 6b.

### Where to splice the RTSP/SIP path
- **uart2d** owns the AVL bus (RS-485 ↔ JSON-over-TCP). To intercept bus
  traffic: wrap `uart2d` (path: `/customer/app/sbin/uart2d`).
- **mimedia** owns RTSP. The factory-reset string `mimedia master &`
  tells us its only argument is `master`. To splice the stream, wrap it
  the same way and `LD_PRELOAD` the listen-socket setup.
- **avlink** owns MQTT-broker connection (port 1883) and the
  IPC socket `/var/run/avlink-mqtt.heartbeat` plus `/var/run/discovery.socket`.
- Easiest in-band point: introduce a fake `discovery` binary at
  `/usr/sbin/discovery` (init.d already references this path) that
  speaks to the real one over a unix socket — `start-stop-daemon` does
  not validate it.

---

## 7. Firmware update behavior

### From `rcS` / `init.d/nginx`
- nginx serves an internal HTTP API on the local LAN (port 80 by default,
  config blob `etc/nginx/conf.d/*.conf` is XOR-/RC4-encrypted — the literal
  filename **`*.conf`** is on disk because nginx loads it via `include
  conf.d/*.conf` and decrypts the contents in a custom patched build).
  We cannot statically extract listen ports / FastCGI routes without
  decrypting; from `monitor.lua` we know the logs live at
  `/usr/local/nginx/logs/{access,error}.log` (custom build path —
  *not* `/var/log/nginx`).
- The nginx process talks to Lua scripts under `/var/www/lua/*.lua`
  (referenced in custode2: `cd /var/www/lua/;lua autoSync.lua;`). All the
  `/customer/lua/*.lua` files mirror the REST endpoints documented in
  `docs/api-rest.md`.

### From `customer/share/firmware_upgrade.lua`
- Spawned by avlink on OTA trigger (`lua /customer/share/firmware_upgrade.lua &`).
- Polls `http://${update_server}/download_path?product_name=AVL20P`.
  Default debug server: `c1.systec-pbx.net`.
- Response format:
  ```json
  {"code": 0, "download_path": "http://c1.systec-pbx.net/software/update.tar.bz2"}
  ```
- Downloads to `/tmp/update.tar.bz2` (RAM, **tmpfs**).
- Executes:
  ```sh
  cd /tmp/ && tar -xjf update.tar.bz2 && cd update && sh update.sh
  ```
- The `update.sh` inside the tarball is therefore **the actual updater**:
  it has full root privileges and can write any UBI volume.
- Paths touched by `update.sh` (inferred from factory-reset string + Lua
  references):
  - `/customer/app/sbin/*` — replaced
  - `/customer/lua/*` — replaced
  - `/customer/share/*.lua` — replaced (firmware_upgrade.lua itself)
  - rootfs UBI (`miservice`) — replaced for kernel/init updates
  - **Preserved** by convention: `/customer/share/avl20.db`,
    `/customer/wifi/wpa_supplicant.conf`, `/customer/mac`,
    `/customer/backup/interfaces`, `/customer/demo.sh`

### Security implications
- OTA over plain HTTP, no signature in client (the lua only checks
  `code == 0`). Anyone able to spoof DNS for `update_server` can ship
  arbitrary `update.sh` → root code execution.
- The `update_server` is a DB-stored config item — settable via the
  documented REST API (`/customer/lua/parameter.lua`).

---

## 8. Service diagram

```
                              ┌─────────────────────────┐
                              │  HW watchdog (60 s TO)  │
                              └────────────▲────────────┘
                                           │ kicked by
                                           │
   ┌──────────────────┐         ┌──────────┴───────────┐
   │  init (busybox)  │────────▶│  uptime_damemon      │
   └────────┬─────────┘         └──────────────────────┘
            │
            ▼
   ┌──────────────────────────────────────────────────────────┐
   │  /etc/init.d/rcS                                         │
   │   ├─ mount ubi: /customer /data /config                  │
   │   ├─ telnetd  (TCP 23, no auth) ◄── DANGER               │
   │   ├─ busybox telnetd &                                   │
   │   ├─ sshd  (/customer/ssh, TCP 22)                       │
   │   ├─ dropbear (TCP 22 alt)                               │
   │   ├─ /customer/demo.sh  ◄── INJECTION POINT (§6c)        │
   │   └─ exec /etc/init.d/rc.local                           │
   └──────────────────────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────── stage 2 (rc.local, not in dump) ───┐
   │                                                            │
   │  init.d/nginx start ───► nginx                             │
   │                          ├─ TCP 80  (REST, /customer/lua/) │
   │                          ├─ logs /usr/local/nginx/logs/    │
   │                          └─ FastCGI/Lua → avl20.db         │
   │                                                            │
   │  init.d/discovery start ─► discovery                       │
   │                            ├─ UDP 239.255.255.240:6210     │
   │                            └─ unix /var/run/discovery.socket
   │                                                            │
   │  init.d/monitor start ───► monitor.lua  (log rotator)      │
   │                            └─ kill -USR1 nginx on rollover │
   │                                                            │
   │  lua custode2.lua &  ────► network/wifi state machine      │
   │           │                                                │
   │           ├─ wpa_supplicant, udhcpc/udhcpd                 │
   │           ├─ uart2d uart2d &  ◄─── unix /var/run/uart-log.socket
   │           ├─ mimedia master &  (RTSP on demand)            │
   │           └─ media-server (P2P client, vendor cloud)       │
   │                                                            │
   │  avlink &  ──────────────► main controller                 │
   │           ├─ MQTT TCP 1883 (broker.systec-pbx.net or local)│
   │           ├─ unix /var/run/avlink-mqtt.heartbeat           │
   │           ├─ pjsua &  (SIP UA, supervised by avlink)       │
   │           │            ├─ UDP/TCP 5060 (SIP)               │
   │           │            └─ RTP dynamic                      │
   │           └─ lua firmware_upgrade.lua &  (on OTA trigger)  │
   │                                                            │
   └────────────────────────────────────────────────────────────┘
```

### Port summary

| Port      | Proto  | Process        | Purpose                              |
|-----------|--------|----------------|--------------------------------------|
| 22        | TCP    | sshd, dropbear | shell                                |
| 23        | TCP    | telnetd (x2)   | shell (no auth!)                     |
| 80        | TCP    | nginx          | REST API + web UI                    |
| 1883      | TCP    | avlink         | MQTT client (outbound to broker)     |
| 5060      | UDP/TCP| pjsua          | SIP                                  |
| 6210      | UDP    | discovery      | mcast 239.255.255.240 LAN discovery  |
| 554 (?)   | TCP    | mimedia        | RTSP (not confirmed in strings, but standard) |
| dyn       | UDP    | pjsua/mimedia  | RTP/RTCP                             |
| unix      | -      | discovery      | /var/run/discovery.socket            |
| unix      | -      | avlink         | /var/run/avlink-mqtt.heartbeat       |
| unix      | -      | uart2d         | /var/run/uart-log.socket             |
| unix      | -      | avlink/discov. | /customer/share/mqtt-client.socket   |

---

## Appendix: noteworthy strings

From `customer/app/sbin/avlink`:
```
lua /customer/share/firmware_upgrade.lua &
/etc/init.d/discovery restart
killall pjsua;                         pjsua &
killall uart2d; killall lua; sleep 0.5; sqlite3 /customer/share/avl20.db < /customer/share/avl20.sql; …  reboot
```

From `customer/app/sbin/discovery`:
```
/var/run/discovery.socket
239.255.255.240
6210
```

From `customer/app/sbin/uart2d`:
```
/var/run/uart-log.socket
/customer/share/avl20.db
```

From `customer/app/sbin/mimedia`:
```
/customer/config/snap.jpg
/customer/share/avl20.db
update config set item = '{"enable":%s,"rtsp":"%s","rtmp":"%s"}' where name = 'video';
```

From `customer/app/sbin/pjsua`:
```
--local-port=port   Set TCP/UDP port. …
```
(stock PJSIP `pjsua` CLI; arguments are passed by avlink at spawn time —
not currently extractable from the strings dump because avlink uses a
runtime-built argv array, not a string literal.)
