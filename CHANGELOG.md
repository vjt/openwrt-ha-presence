# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
