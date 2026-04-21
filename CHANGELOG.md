# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- 🧰 **Pyright strict** + **ruff** + **pre-commit** + **GitHub Actions CI** — tooling floor for the hardening rewrite
- 📜 `scripts/check.sh` — single-command green gate (ruff + pyright + pytest). Inherited from ha-verisure
- 📦 Dev dependency bump: hypothesis, pytest-timeout, pre-commit, aiohttp speedups
- 🧪 **FakeMqttClient + FakeSource + PublishedMsg** in `tests/fakes.py` — test doubles asserting on concrete outcomes, not mock-call string scans
- 🔒 **Wire-format golden fixture** (`tests/wire_format_golden.json` + `tests/test_wire_format.py`) — locks MQTT byte shape against accidental regression through the hardening rewrite
- 🧩 Expanded `sample_config` fixture (3 nodes, 2 people, 3 MACs) via direct `Config(...)` constructor + `ts` timestamp helper — single source for integration tests
- 📐 `Source` protocol in `sources/base.py` — `ExporterSource` and `FakeSource` both satisfy it structurally; `__main__._run(config, client, source: Source)` types the boundary (CRITICAL C5)
- 🧪 Full end-to-end test suite for `__main__._run` via `FakeMqttClient` + `FakeSource` — replaces Session-1 source-inspection guards with real behavioral coverage (connect wiring, LWT ordering, startup gate, graceful shutdown) (CRITICAL C7)

### Changed
- 🧪 `test_mqtt.py` rewritten asserting on `PublishedMsg` outcomes (audit-log + reconnect-no-relog + idempotency coverage preserved)
- 🧪 `test_source_exporters.py` rewritten using `aiohttp` test server + dead-port URLs for health tracking (no private-method monkey-patching)
- 🏷️ Test node names aligned to `ap-*` convention (`ap-garden`/`ap-living`/`ap-bedroom`) — production config untouched
- 🧬 **`StateChange` is now a discriminated union `HomeState | AwayState`** (C1). `HomeState` carries non-optional `room`/`mac`/`node`/`rssi`; `AwayState` carries optional `last_mac`/`last_node` (both `None` for never-seen persons). Consumers pattern-match on the variant — unrepresentable states (away with RSSI, home without room) are no longer expressible. Audit log schema: `state_computed`/`state_delivered` lines for *never-seen* persons now emit `"mac": null, "node": null, "rssi": null` (previously `""`/`""`/`null`). Active-home and once-seen-now-away entries are byte-identical
- 🧊 **`Config` is now a frozen dataclass** (H4). Mutating fields post-construction raises `dataclasses.FrozenInstanceError`. The internal `_mac_lookup` field stays private, `repr=False`, and is now also `compare=False` (public fields uniquely identify a config)
- 🎯 **New `Config.tracked_macs` property** returns `frozenset[Mac]` (M2). `__main__._run` passes `config.tracked_macs` to `ExporterSource` instead of reflattening `config.people` inline — single owner of the lookup set. `ExporterSource` now accepts `AbstractSet[Mac]` and stores `frozenset[Mac]` internally, making the defensive copy immutable
- 🔌 **`_run` accepts injected client + source.** `__main__.main` wires real paho + `ExporterSource`; tests wire `FakeMqttClient` + `FakeSource`. Enables a full end-to-end test suite for the poll loop (CRITICAL C7)
- 🚪 **LWT set in `main()`, not in publisher constructor.** `will_set` must precede `connect_async` (paho sends the will in CONNECT); wiring it at the call site makes the ordering discoverable and testable (HIGH H1)
- 📜 **`audit.py` owns `log_state_computed` / `log_state_delivered`.** `logging.py` is purely the structlog setup boundary — audit calls import from `openwrt_presence.audit` (HIGH H2)
- 🌱 **Seed state after broker connect.** Startup now waits for `on_connect` up to 10s before publishing retained state; `_reseed` runs on the asyncio loop before the wait gate releases. Closes the HA flap where a reconnect could land retained state on a broker that hadn't yet received discovery (HIGH H6)
- 🧠 **Single source of truth for per-person state — engine, not publisher.** `MqttPublisher._last_state` cache deleted; `_on_connect._reseed` builds snapshots from `engine.get_person_snapshot(person, now)`. Eliminates a race-prone duplicate that the paho thread and poll loop could both mutate (HIGH H11)
- ⚙️ **Config defaults centralized** as `Final[int]` module constants (`DEFAULT_AWAY_TIMEOUT_SEC`, `DEFAULT_POLL_INTERVAL_SEC`, `DEFAULT_EXPORTER_PORT`, `DEFAULT_DNS_CACHE_TTL_SEC`). Dataclass field defaults and `from_dict` fallbacks reference the same constants — no more two-places-to-change drift. `departure_timeout` deliberately has no default (alarm-path safety — must be explicit) (HIGH H13)

### Fixed (security-critical)
- 🛡️ **Circuit breaker: all-APs-unreachable (C3).** When every configured AP fails its scrape in the same cycle, `__main__._run` now logs `all_nodes_unreachable` and skips `engine.process_snapshot` for that cycle. Closes the "core switch down arms the alarm on sleeping occupants" class of failure — engine state from the last healthy snapshot is preserved until evidence returns
- 🧵 **paho thread/asyncio race on reconnect (C2).** `on_connected` used to run on paho's network thread and mutate `publisher._last_state` while the poll loop mutated the same dict — `RuntimeError: dictionary changed size during iteration` waiting to happen. The callback now hops to the asyncio loop via `loop.call_soon_threadsafe`
- 📜 **Honest audit log (C4).** The old `state_change` event unconditionally claimed delivery even when paho silently dropped the publish. Split into two events: `state_computed` (engine decided, emitted always) and `state_delivered` (all three topic publishes returned `rc == 0`). A `state_computed` without a matching `state_delivered` is the silent-data-loss tripwire. `publish_failed` with return code now logs per dropped topic

### Fixed
- `on_connect` callback body now wrapped in try/except; paho's internal logger piped through structlog so swallowed exceptions surface (HIGH H7)
- `asyncio.get_running_loop()` replaces deprecated `asyncio.get_event_loop()` — forward-compatible with Python 3.14 (HIGH H8)
- Initial `source.query()` failure no longer crashes `_run` before the poll loop starts; logs `initial_query_failed` and continues with empty readings (HIGH H9)
- `/metrics` scrape calls `response.raise_for_status()` (503/5xx → per-AP exception → node marked unreachable instead of silently empty) and caps response body at 1 MiB against a pathological exporter (MEDIUM M11+M12)
- `_parse_metrics` now raises `ValueError` on a line starting with `wifi_station_signal_dbm` that fails to parse — silent skipping of garbage hides real exporter bugs; the exception bubbles to the per-AP try/except in `query()` and flags the node unhealthy

### Removed
- `engine.tick()` — `process_snapshot` has always owned expiry via `_expire_devices`; `tick` was dead code in production, kept only for two tests. The tests now cover the expiry path through `process_snapshot` directly (MEDIUM M3)
- `_compute_person_state` defensive "unknown person" branch — the method is now precondition-asserted (`assert name in self._config.people`). Callers iterate `config.people` exclusively; the branch was unreachable (MEDIUM M7)

### Documented
- Shutdown ordering in `_run` finally block: `loop_stop()` before `disconnect()` is deliberate — the broker sees a TCP drop and fires our LWT, which is how HA marks entities unavailable on planned shutdowns (HIGH H1)
- 🔐 **"This is security software" framing** promoted to top of `CLAUDE.md` — fail-secure, crash loud, audit trail non-optional, no smart, state the contract explicitly
- 🧾 **Deliberate non-decisions section** in `CLAUDE.md` — Publisher protocol (H10) and runtime config reload (H14) are *intentionally* not built; monitor.py stringly-typed CLI coupling (A1:A7) is accepted complexity. Prevents future drift toward accidental speculative generality

### Migration notes
**Audit log schema change:** the `state_change` message is GONE, replaced by `state_computed` (engine produced a change) and `state_delivered` (MQTT accepted the three topics). `openwrt-monitor` handles both; any external log shipper filtering on `message=state_change` must update its filter. The structured fields (`person`, `presence`, `room`, `mac`, `node`, `rssi`, `event_ts`) are unchanged.

**MQTT `never_seen` attributes payload** (C1): for a person tracked in `config.yaml` who has never been observed on any AP (e.g. fresh startup seed before their phone has connected), the `attributes` topic previously published `{"event_ts": "...", "mac": "", "node": "", "rssi": null}` and now publishes `{"event_ts": "..."}` — the `mac`/`node`/`rssi` keys are dropped entirely. HA template sensors reading `state_attr('device_tracker.alice_wifi', 'mac')` on a never-seen person get `None` instead of `""`. Both values are falsy in Jinja, so `{% if state_attr(...) %}` templates survive unchanged. Home and once-seen-now-away payloads are byte-identical to 0.5.0.

## [0.5.0] — 2026-04-21

### Added
- 🛡️ **QoS 1 on every publish** (state, discovery, availability, LWT). Paho locally buffers unacked messages and retransmits on reconnect — no more silent losses when the broker hiccups
- 🔁 **Reconnect-aware MQTT.** `on_connect` / `on_disconnect` callbacks log transitions, and every (re)connect re-publishes discovery, availability, and the full per-person state cache. Survives broker restarts and retained-state loss (e.g. Mosquitto major-version upgrades)
- 🌱 **Startup state seed.** Every configured person gets a `state_change` published and logged at boot, even if nobody transitioned — closes the gap where an already-away person could leave HA showing stale `home`
- 🧠 `PresenceEngine.get_person_snapshot(name, now)` — current aggregated state as a `StateChange`, used by startup seed and reconnect republish
- 📦 Structured JSON logging via `structlog` — ISO `ts`, uppercase levels, one line per event
- 🩺 **Node health tracking.** Scrape failures only log on the healthy↔unreachable transition, no more log spam when an AP flaps
- 🛜 `dns_cache_ttl` config — reuse DNS lookups across poll cycles (default 300s)
- 📖 `CLAUDE.md` committing the project's engineering standards
- 📜 This changelog 🎉

### Changed
- 🚪 **One gate for state emission.** `publisher.publish_state(change)` now both publishes and writes the audit log. Callers can no longer forget one half
- 🔌 `connect_async()` + `loop_start()` at boot — eve tolerates the broker being down at startup; paho queues and flushes on first successful connect
- ⚙️ Explicit paho tuning: `reconnect_delay_set(1, 60)`, `max_queued_messages_set(1000)`

### Fixed
- 📝 Startup path now always emits one `state_change` log line per person — previously silent when everyone was already away at boot

### Migration notes
No config changes required. If you scrape eve's logs, note that every startup now emits one `state_change` per configured person (previously only on transition).

## [0.4.0] — 2026-02-16

### Added
- 🚪 **Exit nodes.** Mark APs with `exit: true` (e.g. a garden AP) to use `departure_timeout` — short, "person walked out". Interior nodes use `away_timeout` — long, safety net for phone doze
- ⏱️ `away_timeout` config (default `64800` / 18h) — covers phone Wi-Fi doze cycles without triggering false departures
- 🧪 Integration tests covering exit/interior timeout interactions

### Changed
- 🧠 `Config.timeout_for_node()` is the single source of truth for the exit-vs-interior policy
- 📖 README updated with exit node configuration and automations

### Migration notes
Optional. With no `exit: true` markers, every node keeps using `departure_timeout` (backwards compatible). If you do add exit nodes, you'll want to drop `departure_timeout` back down to ~120s since the doze safety-net now lives in `away_timeout`.

## [0.3.0] — 2026-02-16

### Added
- 📡 **`ExporterSource`** — scrapes each AP's `/metrics` endpoint directly in parallel, parses `wifi_station_signal_dbm`
- ⚙️ `exporter_port` config (default `9100`) + per-node `url` override for APs without local DNS or on non-standard ports

### Removed
- 🗑️ `PrometheusSource` and `VictoriaLogsSource` — no more external TSDB dependency. eve talks to APs directly

### Changed
- 📖 Architecture docs rewritten for the direct-scrape flow

### Migration notes
**Breaking** for existing Prometheus / VictoriaLogs deployments:
- Remove `source:` and `lookback:` from `config.yaml`
- Add `exporter_port: 9100` (or per-node `url:` overrides)
- Install `prometheus-node-exporter-lua-wifi_stations` on each AP (see README). Prometheus / VictoriaLogs are no longer needed.

## [0.2.0] — 2026-02-15

### Added
- 📊 **RSSI-based room detection** — strongest-signal AP wins
- 🔄 `PrometheusSource` for scraping RSSI from VictoriaMetrics / Prometheus
- 📜 `VictoriaLogsSource` for ingesting AP logs
- ⏱️ Configurable `poll_interval` and `lookback`
- 🛠️ `openwrt-monitor` CLI — pretty-prints the JSON log stream with ANSI colors

### Changed
- 🔄 Switched from hostapd log parsing (event-driven) to RSSI metrics polling (snapshot-driven). More robust against dropped or out-of-order events
- 📖 README overhauled with HA screenshots and setup instructions

## [0.1.0] — 2026-02-13

### Added
- 🧠 **Presence engine** — per-device state machine (`CONNECTED → DEPARTING → AWAY`), aggregated into per-person home/away
- 📡 Hostapd log parser (initial event source)
- 🏡 **MQTT publisher** with Home Assistant Discovery — `device_tracker.<person>_wifi` + `sensor.<person>_room` per person, retained state, Last Will & Testament
- 🛠️ `config.yaml` loader with validation (nodes, people, MAC lookup, duplicate-MAC detection)
- 📜 Structured JSON logs for state changes
- 🐳 Dockerfile + docker-compose for deployment
- 🧪 Unit tests and integration replay tests
- 📖 README + MIT license

[Unreleased]: https://github.com/vjt/openwrt-ha-presence/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/vjt/openwrt-ha-presence/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/vjt/openwrt-ha-presence/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vjt/openwrt-ha-presence/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vjt/openwrt-ha-presence/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/vjt/openwrt-ha-presence/releases/tag/v0.1.0
