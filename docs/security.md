# Security — Disclosure Status

**Status:** Coordinated disclosure in progress with HHG GmbH (manufacturer).

The previous version of this file contained detailed reproduction steps for
multiple severe security defaults in the Villa GW firmware (4.1.x). Out of
respect for coordinated-disclosure practice these details have been temporarily
withdrawn.

---

## Timeline

| Date (UTC) | Event |
|---|---|
| 2026-05-22 | Findings re-verified against live device (firmware 4.1.12) |
| 2026-05-22 | Public detail withdrawn from this repository (commit `29a1fd8`) |
| 2026-05-24 | Detailed disclosure sent to HHG GmbH (`info@hhg-elektro.de`) |
| 2026-08-22 | Planned public disclosure (90 days after vendor notification), unless an agreement with HHG is reached |

## Scope of the findings (summary only)

Without disclosing reproduction steps, the issue classes affecting **firmware
4.x as shipped** include:

- Unauthenticated remote shell access on the LAN
- Default credentials documented by the manufacturer without instructions to
  change them
- A second hardcoded administrative account present in the factory database
- Unauthenticated control of the proprietary intercom bus (door relay,
  microphone/camera) on the LAN
- Cleartext storage and API retrieval of cloud-service credentials
- SQL injection in REST endpoints, including authentication bypass
- Unsigned firmware updates accepted via the web UI

The user manual (V7.160524) does not discuss any IT-security or network-isolation
aspects, instructs users to log in with default credentials without recommending
a change, and treats an active router firewall as a troubleshooting symptom
(p. 8).

## Deployment recommendations (for users of this integration)

Until the vendor addresses these issues:

1. Place the Villa GW on a **dedicated VLAN / IoT subnet**, firewalled from
   guest WiFi, smart-TVs, printers and other IoT devices.
2. Allow inbound from your Home Assistant host only.
3. Keep the device firewalled from the internet except for `de.ilifestyle-cloud.com`
   if you still want the vendor app to function. Block all other egress.
4. Change the web admin password from the default.
5. Disable WiFi if you use a wired connection.
6. Avoid using the iLifestyle cloud password anywhere else.

## Contact

If you are a security researcher with questions, please email the integration
maintainer — do not file public GitHub issues with reproduction details before
the disclosure window above has elapsed.
