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
2. ✅ `sip_messages.py` extrahiert; `sip_client.py` re-exportiert (Tests nutzen `sip.parse_headers` etc.). Commit.
3. ✅ `sip_transport.py` extrahiert (SipTransport + TlsSipTransport + `_make_unverified_tls_context`); re-export für coordinator+tests. Commit. → sip_client.py 700→435 Zeilen.
4. ✅ `sip_strategies.py`: `InviteStrategy`/`SilentStrategy`/`EarlyMedia183Strategy`; `_dispatch`
   feuert Ring-Detection zuerst, delegiert Wire-Response an Strategie; `_build_183`/Probe verschoben.
   + `client.transport`/`.user` Accessor. + Unit-Tests (`test_sip_strategies.py`). 66 Tests. Commit.
   → **SIP-Schicht damit vollständig restrukturiert (messages·transport·strategies·client), sip_client 700→341.**
5. ⏸️ `sip_media.py` (RTP/G.711) — **bewusst zurückgestellt**: RTP-Logik lebt aktuell sauber in
   EarlyMedia183Strategy; die richtige Media-Abstraktion ergibt sich erst beim echten ICE/RTP-Audio-UA.
   Dann extrahieren (verhindert spekulative Abstraktion).
6. (Hygiene) `api.py` → `gw_web/avlink/bus/logtail` — nicht audio-blockierend.
7. (Hygiene) `coordinator.py` (794 Z.) entschlacken — nicht audio-blockierend.
8. ✅ teilweise: Strategie-Tests da; weitere pro Modul bei 6/7.
9. README/CHANGELOG, Version → 0.2.0; HACS-Deploy + 1 Restart.

**Audio-relevanter Umbau = fertig.** Schritte 6/7 sind allgemeine Hygiene; Schritt 5 wartet aufs Audio-Feature.

## Invarianten (dürfen NICHT brechen)
- Silent-Mode bleibt Default (kein 200 OK ungefragt → iPhone-Fork unberührt).
- `cloud_online` self-heal (GW-Poll), SIP-Re-REGISTER vor Expiry, kein Doppel-REGISTER.
- Doorbell-Aufnahme: ein Klingeln = ein Video (kein `live_view_started`-Trigger).
- 63 bestehende Tests grün halten.
