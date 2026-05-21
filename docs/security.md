# Security Audit & Recommendations

The Villa GW firmware (4.1.11) ships with multiple severe defaults that make integration easy for us — and very dangerous for anyone exposing it. This page documents what we found and what you should do.

## Critical findings

### 1. Telnet root shell, no authentication

```
$ nc 192.0.2.10 23
[Welcome banner]
/  #     ← already root
```

**Impact**: Anyone on the LAN gets a root shell. Full filesystem access, can read all credentials, modify firmware, install backdoors.

**Mitigation**: None possible without modifying the firmware boot script (`/etc/init.d/networking` chain). Treat the LAN as trusted.

### 2. SSH (dropbear) accepts empty root password

```
$ ssh -oHostKeyAlgorithms=+ssh-dss root@192.0.2.10
[no prompt for password, or accept empty]
```

**Impact**: Same as Telnet. Slightly mitigated because modern OpenSSH refuses `ssh-dss`, so script-kiddies with stock tools won't get in. Anyone with `paramiko` or `dropbear`-client will.

**Mitigation**: None without firmware modification.

### 3. Web admin uses default credentials `admin / admin`

The web UI at `http://<gw>/` exposes:
- All cloud account details
- SIP/MQTT server credentials
- Network configuration
- Reboot, firmware upgrade

**Mitigation**: Change the password in the web UI (Advanced settings → Change password). This integration uses `admin/admin` by default but lets you override in Config Flow.

### 4. Cloud credentials in plaintext

`GET /api/account` (any authenticated session) returns:

```json
{
  "account": "<email>",
  "password": "<cleartext password>",      ← !!
  "token": "<JWT>",
  "server": "de.ilifestyle-cloud.com",
  ...
}
```

Also stored in plaintext at:
- `/customer/share/avl20.db` → `config.cloud_account.item`
- `/customer/share/avl20.db` → `config.sip.password` (device-token, less sensitive but still)

**Impact**: Anyone with web access (default `admin/admin`) gets the cloud account password — the same one used in the iLifestyle smartphone app. If reused on other services: account takeover risk.

**Mitigation**: Don't reuse the iLifestyle password anywhere else. Change the web admin password.

### 5. uart2d Bus-control on `0.0.0.0:10087`, no authentication

```
$ printf "AT+B UART unlock 1\r\n" | nc <gw> 10087
# door opens
```

Same threat surface as Telnet — but specific to the bus. Anyone on the LAN can:
- Open the door (`unlock`)
- Make outdoor station ring (`call`)
- Activate camera silently (`monitor`) — privacy concern
- Disrupt bus traffic

**This is what this integration depends on.** Closing it would break our work, but it _is_ a real risk for anyone with a hostile LAN guest.

**Mitigation**:
- Put the Villa GW on a dedicated VLAN with only HA and the bus controller as members
- Don't allow guest WiFi to route to the IoT VLAN
- Firewall port 10087 inbound from anywhere except the HA host

### 6. SQL injection in `addCallList.lua` / `updateCallList.lua`

The Lua REST handlers build SQL via `string.format("UPDATE callList SET name = '%s' …", name, …)` without escaping. A crafted `name` field with a `'` character could inject SQL.

**Impact**: Limited — requires authenticated web session. Could be used to corrupt the call list or escalate to other configs.

**Mitigation**: Don't expose the web admin to untrusted users.

### 7. Cloud MQTT credentials are extractable

Anyone with shell access to the GW can dump `/customer/share/avl20.db` and recover:
- iLifestyle email/password
- Device JWT (rolling, ~24 h lifetime)
- SIP credentials

Combined with finding 1/2, this means: anyone on the LAN can impersonate the device against the cloud.

### 8. Firmware update is unsigned (suspected)

The firmware update routine in `/customer/share/firmware_upgrade.lua` doesn't appear to verify package signatures (didn't fully audit). If true, a MITM during update could install arbitrary firmware.

## Recommended hardening

If you deploy this integration in production:

1. **Network isolation**: dedicated VLAN, firewall rules:
   - HA → GW: ports 23 (telnet, for log tail), 80 (REST), 554 (RTSP), 10087 (uart2d) — allow
   - GW → internet: only `de.ilifestyle-cloud.com:1883/8883` (MQTT) and HTTPS — allow if you want the app to keep working; block otherwise
   - LAN → GW: deny by default; carve out only the HA host
2. **Change the web admin password** (admin/admin → strong password)
3. **Disable WiFi** in the GW (we don't need it; eth0 is enough)
4. **Monitor `/customer/share/usr-log.log`** via this integration's tail anyway — unusual access patterns will show up
5. **Consider disconnecting from the iLifestyle cloud entirely** once you're happy with the local integration. This involves overriding `config.sip.mqtt_server` in the DB — see [`protocol.md`](protocol.md). Note: app stops working.

## Disclosure status

These findings are publicly known among intercom-tinkerers (see [iobroker forum thread #46995](https://forum.iobroker.net/topic/46995/einbindung-sprechanlage-hgg-villa)). HHG/EGB has not, to our knowledge, addressed them in 4.x firmware. The device is sold as a residential intercom — the threat model is presumably "trusted LAN only" — but in practice many homes have hostile-LAN scenarios (WiFi guests, IoT-malware-infected devices).

We do not consider these findings a 0-day; they are documented here to help users make informed deployment decisions.
