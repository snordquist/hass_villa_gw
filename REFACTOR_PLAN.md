# v0.2.0 — Volle Neustrukturierung (Plan)

Branch: `refactor/v0.2.0-restructure`. Ziel: saubere, **erweiterbare** In-process-Architektur
als Fundament für SIP-Audio-Capture (Schritt-2-Tests: 183-Early-Media, 200-OK-Answer, RTP,
Mux) und folgende Experimente. Kein separates Addon. Jeder Schritt: `pytest` + `ruff` grün,
ein Commit. Deploy am Ende als ein v0.2.0 (HACS + 1 Restart).

## Designentscheidung
Flache, prefixierte Module statt tiefer Subpakete (`sip_*`, `gw_*`) — die Test-Suite lädt
Module per Pfad-`importlib` flach; echte Subpakete würden `from ..x`-Importe + Test-Infra-
Rewrite erzwingen (Risiko ohne Mehrwert). Gleiche Trennung/Erweiterbarkeit.

## Zielmodule
- `sip_messages.py` — pure: md5_hex, parse_headers, parse_digest_challenge, build_digest_auth,
  build_register, _copy_response_headers, build_ok_response, build_terminated_response,
  extract_invite_info, extract_remote_media, USER_AGENT, build_183/SDP-Builder.
- `sip_transport.py` — `SipTransport` Protocol + `TlsSipTransport` (+ `_make_unverified_tls_context`).
- `sip_strategies.py` — ⭐ Erweiterbarkeit: `InviteStrategy`-Basis + `SilentStrategy`,
  `EarlyMedia183Strategy` (heutiger Probe-Code, gekapselt), später `Answer200Strategy`.
  `SipClient` ruft nur `strategy.on_invite(client, msg, ctx)`.
- `sip_media.py` — ⭐ RTP/Audio: UDP-Empfang, G.711-Decode, Capture-Sink; ICE/symmetrisches
  RTP kommt hier rein (echter UA). Strategien nutzen sie.
- `sip_client.py` — schlanke State-Machine (REGISTER, Dialog, Re-REGISTER); re-exportiert die
  sip_messages-Helfer für die bestehenden Tests (`sip.parse_headers` etc.).
- `gw_web.py` (REST/md5/`/api/sip`/rtsp_url), `gw_avlink.py` (AT+B 10086), `gw_bus.py`
  (uart2d 10087), `gw_logtail.py` (Telnet-Tail + Pattern) — aufgeteilt aus `api.py`.
- `coordinator.py` — entschlackt: verdrahtet gw_* + sip_* + State/Events; Poll-/Event-Logik
  ggf. in `coordinator_poll.py` / `coordinator_events.py`.

## Schritte (geordnet, je committbar, Tests grün)
1. ✅ **Lint-Altlasten** (12 ruff-Fehler) — erledigt, Commit.
2. `sip_messages.py` extrahieren; `sip_client.py` importiert+re-exportiert. (Tests referenzieren
   `sip.build_register`/`parse_headers`/… → re-export zwingend.)
3. `sip_transport.py` extrahieren (TlsSipTransport + `_make_unverified_tls_context`).
4. `sip_strategies.py` einführen: `SilentStrategy` + `EarlyMedia183Strategy`; `_dispatch` ruft
   Strategie statt eingebetteter if-Zweige. `_build_183`/`_run_early_media_probe` → Strategie/Media.
5. `sip_media.py`: RTP-Empfang/G.711 aus dem Probe-Code herausziehen, klar gekapselt.
6. `api.py` → `gw_web.py` + `gw_avlink.py` + `gw_bus.py` + `gw_logtail.py`.
7. `coordinator.py` entschlacken (Poll/Events ggf. eigene Module).
8. Test-`importlib`-Shims an neue Modulnamen anpassen; neue Unit-Tests pro Modul.
9. README/CHANGELOG, Version → 0.2.0; HACS-Deploy + 1 Restart.

## Invarianten (dürfen NICHT brechen)
- Silent-Mode bleibt Default (kein 200 OK ungefragt → iPhone-Fork unberührt).
- `cloud_online` self-heal (GW-Poll), SIP-Re-REGISTER vor Expiry, kein Doppel-REGISTER.
- Doorbell-Aufnahme: ein Klingeln = ein Video (kein `live_view_started`-Trigger).
- 63 bestehende Tests grün halten.
