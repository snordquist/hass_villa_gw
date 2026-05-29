# Changelog

All notable changes to this integration are documented here. The format
loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.3] — 2026-05-29

### Fixed

- **Event-loop blocking call during SIP connect.**
  `TlsSipTransport.connect()` built its TLS context with
  `ssl.create_default_context()`, which loads the system CA store via blocking
  file I/O (`load_default_certs` / `set_default_verify_paths`) — flagged by
  HA's event-loop blocking-call detector on every (re)connect. Since the Cloud
  cert is self-signed and we use `CERT_NONE`, the CA store is never needed: the
  context is now built directly as a bare `PROTOCOL_TLS_CLIENT` context
  (`_make_unverified_tls_context`), doing no I/O. No behaviour change.

## [0.1.2] — 2026-05-29

### Fixed

- **Cloud SIP listener never stayed registered** (`cloud_sip_connected`
  stuck `off`, log spamming `SIP REGISTER failed (initial)` every ~2 s).
  Two bugs: (1) `_sip_loop` called `register_once()` *and* then `run()`
  re-registered immediately — the Cloud rejected the duplicate REGISTER;
  (2) the backoff was `reset()` right after that first (doomed) register,
  so a rejected listener retried every 2 s instead of escalating, risking
  a Cloud-side block. `run()` is now the single REGISTER path and reports
  success via a new `on_registered` callback; the coordinator flips
  `cloud_sip_connected` and resets the backoff **only** on a genuine
  registration, so failures escalate (2 s → … → 5 min cap).

### Added

- `SipClient.last_register_error` + `on_registered` callback. REGISTER
  rejections now surface the SIP status line (e.g. `403 Forbidden`) in the
  coordinator's WARNING log, so a failing listener is debuggable without
  enabling DEBUG.

## [0.1.1] — 2026-05-29

### Fixed

- **`binary_sensor.cloud_online` no longer falsely reads `off` after a
  restart.** It was driven *only* by the edge-triggered `mqtt connect ok`
  log line, initialised to `False`, and never re-synced — so after every
  HA restart it stayed `off` until the GW happened to (re)connect MQTT and
  emit a fresh log line (which `tail -F` rarely replays). The poll loop now
  reads the authoritative GW→Cloud link status from the web API
  (`GET /api/sip` → `online`) on a throttled cadence
  (`CLOUD_STATUS_INTERVAL_S`, 30 s), so the sensor self-heals to the true
  state on the first poll after start. The log-tail `cloud_connect` event
  remains the instant fast-path between polls.

### Added

- `VillaGwClient.cloud_link_online()` — authoritative GW↔Cloud status read.
- INFO logging on every `cloud_online` transition (poll- and log-sourced),
  for future debugging.

## [0.1.0] — 2026-05-24

First feature-complete release. Adds an opt-in Cloud SIP ring listener
on top of the existing fully-local poll + log-tail paths, with
end-to-end auto-discovery (no manual `sip_id` / `binding_code` lookup).

### Added

- **Cloud SIP listener** (`enable_cloud` toggle, opt-in). HA registers
  as a 2nd App-User at `de.ilifestyle-cloud.com:5061` (TLS, Digest-MD5
  on realm `icloud`) and receives forked SIP-INVITEs in parallel with
  the iPhone app. Silent observer — never sends a SIP response to
  INVITE, so the iPhone-fork is never marginalized.
- Cloud-API client (`cloud_api.py`) — async aiohttp-based wrapper for
  `/api/v2/login`, `/api/device`, and `POST /api/device` (formal bind,
  best-effort). Auto-issues `sip_id` + `sip_password` on first login.
- New config-flow step asks for iLifestyle account email + password +
  optional binding code; the integration generates a stable
  `homeassistant-villa-<hex>` device identifier and persists all cached
  cloud credentials in the entry data.
- Options flow now mirrors the config-flow cloud step, so existing
  Villa GW entries can enable Cloud SIP without delete + re-add.
- Three new diagnostic entities:
  - `binary_sensor.villa_gw_cloud_sip_connected` — SIP REGISTER session
    health (CONNECTIVITY device-class).
  - `sensor.villa_gw_last_ring_source` — which path won the dedup race
    (`sip` / `log` / `poll`). Disabled by default; enable for
    cross-source latency comparison.
- All three ring sources (poll loop, log-tail, Cloud SIP) now tag their
  fires with `source=…`, so the existing `_fire()` 2 s dedup window
  collapses them into a single `villa_gw_doorbell_ringing` HA event.

### Fixed

- `live_view_active` no longer stays False during HA-initiated
  `AT+B UART monitor` sessions. The avlink state machine doesn't
  advance on monitor commands, so the poll path is blind to them; a
  per-button `post_action` callback now flips the flag locally as soon
  as the bus command succeeds, and a successful `AT_UART_MONITOR
  response=ok` from the log-tail path acts as a backup signal.
- SIP listener has a 60 s TTL on tracked `_active_invites` entries —
  iPhone-accepted rings never CANCEL to our endpoint, which would
  otherwise have leaked ~1 kB per ring forever.
- `register_once()` now matches the CSeq header rather than substring,
  so a 200 OK whose `Allow:` header contains "REGISTER" is no longer
  misread as a successful REGISTER response.
- `SipClient.run()` raises on periodic re-REGISTER failure instead of
  silently looping unregistered with `cloud_sip_connected=True`.

### Security / Privacy

- Tokens, SIP passwords, and the binding code are never logged in
  cleartext. Only response `code` + `msg` appear in exception messages
  (the full Cloud response body is omitted because `/api/device` carries
  `sip_password`). Binding code is logged only as `<len=N>`.
- Cloud email/password are persisted in HA's encrypted config entry
  storage. The cached `sip_id`/`sip_password` are reused on every
  reconnect; if the Cloud rotates them, the listener fails to
  REGISTER and the user must currently re-run the options flow to
  refresh. Automatic re-login on REGISTER-401 is planned but not
  yet implemented in 0.1.0.

## [0.0.3] and earlier

Local-only baseline — poll-loop + telnet log-tail + on-demand AT+B
control + RTSP camera. See git history for details.
