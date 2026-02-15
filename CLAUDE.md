# openwrt-presence

WiFi presence detection for Home Assistant using OpenWrt AP RSSI metrics.

## Architecture

```
OpenWrt APs → telegraf → VictoriaMetrics → openwrt-presence → MQTT → Home Assistant
 (node-exporter-lua)     (Prometheus API)   (state machine)   (discovery)  (device_tracker + sensor)
```

### Data flow

1. **Source** (`sources/prometheus.py`) polls a Prometheus-compatible TSDB for `wifi_station_signal_dbm` metrics, returns `StationReading(mac, ap, rssi)`
2. **Engine** (`engine.py`) processes snapshots through a per-device state machine (`CONNECTED`/`DEPARTING`/`AWAY`), uses RSSI for room selection, aggregates into per-person state, emits `StateChange` when person-level state changes
3. **MQTT publisher** (`mqtt.py`) publishes HA discovery configs, state (`home`/`not_home`), room, and JSON attributes (event_ts, mac, node, rssi)
4. **Main** (`__main__.py`) wires it all together: initial query → poll loop, with graceful shutdown

### Presence detection model

Every poll cycle (~30s), the engine queries which stations are currently associated with which APs and at what signal strength. All nodes are equal — no exit/interior distinction.

- **Visible MAC** → CONNECTED, room determined by strongest RSSI
- **Disappeared MAC** (was CONNECTED, not in snapshot) → DEPARTING, departure timer starts
- **Departure timer expires** (`departure_timeout`, default 120s) → AWAY
- **Reappears before timeout** → back to CONNECTED, timer cancelled

Room selection: strongest RSSI among CONNECTED devices for a person. DEPARTING devices preserve last known room as fallback.

## Key files

- `src/openwrt_presence/engine.py` — `PresenceEngine` state machine, `StationReading`, `StateChange` dataclasses
- `src/openwrt_presence/sources/prometheus.py` — `PrometheusSource`, PromQL query building, response parsing
- `src/openwrt_presence/mqtt.py` — `MqttPublisher` with HA MQTT Discovery
- `src/openwrt_presence/config.py` — YAML config loading, `Config` dataclass
- `src/openwrt_presence/logging.py` — structured JSON logging, `log_state_change()`
- `src/openwrt_presence/__main__.py` — async entrypoint, poll loop, signal handling
- `src/openwrt_presence/monitor.py` — pretty-print CLI for JSON log stream (ANSI colors, RSSI display, stdin filter)

## Testing

```bash
.venv/bin/pytest -v
```

55 tests. Engine tests are pure-logic (no I/O). Source tests validate query building and response parsing. MQTT tests mock paho client.

## Deployment

Docker Compose with custom CA trust (`bad-ass.crt`). The `.example` files are tracked; actual `Dockerfile`, `docker-compose.yaml`, and `config.yaml` are gitignored.

`SSL_CERT_FILE` env var is required because aiohttp uses certifi's bundle, not the system CA store.

`POLL_INTERVAL` env var (default 30s) controls how often the TSDB is queried.

## My setup

- **People**: marcello (AA:BB:CC:11:22:01 personal, AA:BB:CC:11:22:02 work), sara (AA:BB:CC:11:22:03), elene (AA:BB:CC:11:22:04)
- **Nodes**: mowgli=garden, pingu=office, albert=bedroom, golem=livingroom, gordon=kitchen, parrot=laundry_room
- **HA entities**: `device_tracker.openwrt_presence_{marcello,sara,elene}_wifi`, `sensor.openwrt_presence_{name}_room`
- **Person entities**: `person.marcello_barnaba`, `person.sara_lo_russo`
- **VictoriaMetrics**: https://metrics.example.com/ (custom CA)
- **Docker service name**: eve
- **Deploy path**: /srv/eve on the server
- **Blog**: https://sindro.me/ — README style should match this voice

## Design decisions

- Engine never calls `datetime.now()` — timestamps come as arguments for testability
- RSSI-based room selection: strongest signal among CONNECTED devices determines room, immune to AP clock skew
- Departure timeout (~120s) is the only timeout — replaces both exit-node timeout and global away_timeout
- `StateChange` carries the snapshot timestamp, logged as `event_ts` (distinct from log record `ts`)
- Poll loop uses `asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL)` — sleeps for poll interval AND wakes immediately on SIGTERM/SIGINT
- `PrometheusSource.query()` catches `aiohttp.ClientError`/`TimeoutError` and returns empty list — caller retries next cycle
- MAC format: VictoriaMetrics stores uppercase, engine uses lowercase — normalization happens at the source adapter boundary
- Monitor CLI (`monitor.py`) is pure stdlib, no dependencies — can be run directly with `python3` without pip install
