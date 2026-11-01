# AP push events (hybrid with scraping)

**Status: implemented 2026-11-01** (`2a78d1e`, `57fb39c`), live on the five
25.12 APs; the 19.07 AP stays scrape-only (no ucode / `ubus subscribe` /
`uclient-fetch --header`). Measured, UTC, from AP logread + eve log:

| case | hostapd | eve `ap_event` | `state_delivered` | latency |
|---|---|---|---|---|
| reassoc after inactivity (bedroom) | associated 23:10:33.705 | 23:10:34.288 | 23:10:34.289 | 0.58 s |
| FT roam, walking | 23:22:25.664 | — | 23:22:25.756 | 92 ms |

In the FT roam the room changed in HA 2.8 s before the walker's own
physical marker, i.e. before the scrape-only path could have seen it.

**Goal:** a phone that associates to an AP moves its owner to that room *now*,
not at the next poll. The event itself carries the data (MAC, AP, RSSI): no
"kick then scrape".

**Not a return to the 2026-02-13 design.** Events alone were dropped for good
reasons (doze disconnects, no backfill). Scraping stays the source of truth for
RSSI-based room choice and for departures; events only make *arrivals and
roams* immediate.

## Shape

    AP: ucode daemon, ubus *subscriber* on every hostapd.<iface> object
        → uclient-fetch --header "Authorization: Bearer <per-AP token>"
           POST eve:<event_port>/event
           {"node":"<ap>","mac":"..","event":"assoc","rssi":-61}
    eve: HTTP endpoint → engine.process_event() → MQTT (same discovery, same topics)
    eve: poll loop unchanged, safety net + RSSI updates within one AP

## Engine semantics

- `assoc` on node N for a tracked MAC → CONNECTED, node = N, rssi from the
  event (`null` → keep the tracker's last value). Cancels any departure timer.
  Publish at once (only if the person-level state changed, as for scrapes).
- `disassoc` → ignored for state (log only). Departures stay driven by scrape
  disappearance + exit/interior timeouts: a disassoc during roaming or doze
  must not mark anyone away.
- **Hold window** (`event_hold`, default 10 s): for that long after an assoc
  the scrape can neither move the device off N nor start its departure; a
  scrape reading *on N* still refreshes the RSSI. Needed because a scrape
  that was in flight when the event landed carries pre-roam data (the old
  AP still lists the station until its inactivity timer fires), and would
  undo the event within milliseconds. After the hold, the existing
  best-RSSI rule applies again.
- **Tracked and untracked MACs both get 204** (amended: the first draft said
  4xx for unknown MACs here and 202 in Tests). 204, not 202: `uclient-fetch`
  exits 8 on 202 (measured), which the AP would log as a failed post. Rationale: the AP pushes every
  station (guests, IoT) because the tracked list lives in eve only; an
  untracked MAC is the normal case, not a client error, and a different
  status would give anyone holding one AP token an oracle for which MACs
  eve tracks. The untracked case is logged (`ap_event`, `tracked=false`)
  and publishes nothing.
- Malformed body (bad JSON, missing/extra key, bad MAC, unknown event type,
  rssi not an int in [-127, -1] or null) → 400; body > 1 KiB → 413.
- Unknown node, node without a token, missing/wrong token → 403.
- Every rejection is logged (`ap_event_rejected`, reason, peer address).

## Auth

One token per AP in eve config.yaml (`nodes.<name>.token`, ≥ 32 chars,
unique), constant-time compare against the token of the node named in the
body: an AP cannot speak for another. Plain HTTP on the LAN like the scrape;
no MQTT creds on APs. `event_port` in config enables the listener; tokens
without a port, or a port without tokens, is a `ConfigError` (dead config).
Nodes without a token keep working by scrape only, so APs can be migrated
one at a time.

## Which hostapd notification (amended, verified on a 25.12.6 AP)

- `ubus listen` sees **nothing**: hostapd uses `ubus_notify` on its
  `hostapd.<iface>` objects, which reach *subscribers* only. Measured: in
  45 s, `ubus subscribe hostapd.*` got 15 `probe` notifications, `ubus
  listen` got none.
- usteer is also subscribed and sets `notify_response`, so hostapd sends
  `probe`/`auth`/`assoc` as requests and **waits up to 100 ms for every
  subscriber's reply** before answering the client (seen in `ubus
  monitor`: `invoke` + `status` pairs). A subscriber that stalls (e.g. a
  shell loop blocked on an HTTP call with the `ubus subscribe` pipe full)
  therefore stalls hostapd, and a non-zero reply *denies* the request.
  The daemon is ucode (`ucode-mod-ubus` + `uloop`, already on the image)
  instead of `sh` + `ubus subscribe`: the callback returns 0 at once and
  the HTTP POST is an async child (`uloop.process`), so hostapd never
  waits on eve.
- The pushed `assoc` is sent on `sta-authorized` (after the 4-way
  handshake), not on the raw `assoc` request: someone spoofing a
  resident's MAC without the PSK must not be able to move anyone. The RSSI
  comes from the preceding `assoc` request's `signal` (cached per MAC,
  short TTL); missing → `null`.
- Interfaces come and go on wifi reload: the daemon listens for
  `ubus.object.add` and subscribes to new `hostapd.*` objects, so no
  restart is needed after a reload.

## AP side (×6)

- `/usr/bin/eve-push` (ucode), procd init `/etc/init.d/eve-push`
  (respawn), token in `/etc/eve-push.token` (600), eve URL in
  `/etc/eve-push.url`. Source tracked in `contrib/openwrt/`.
- All four in `/etc/sysupgrade.conf`.
- `uclient-fetch` 2026.06 supports `--header` (checked), so the token goes
  in `Authorization` as planned, no extra packages. The token sits in the
  child's argv for ≤ 2 s: acceptable on a root-only AP (hostapd runs
  jailed and cannot see it).
- Fire-and-forget, `-T 2` timeout, at most 8 POSTs in flight (excess
  dropped and logged): eve down must never block hostapd nor pile up
  processes on a 128 MB AP.

## Tests

- engine: assoc moves room + cancels DEPARTING; disassoc changes nothing;
  hold window blocks a stale scrape and the departure; scrape after the
  hold applies best-RSSI; untracked MAC → no change.
- endpoint: token/node mismatch 403, unknown node 403, missing token 403,
  untracked MAC 204 + no publish, tracked MAC 204 + publish, malformed 400,
  oversize 413.
- config: token/port validation.
- end-to-end on one AP before the other five.

## Measured, not assumed

(Results: see the status table at the top.)

Before/after on a phone walking between rooms: assoc line in logread on
the AP → eve `ap_event` → MQTT state change. Today's bound is the poll
(≤10 s) plus the phone's own roaming delay; only the first part goes away.
