# openwrt-presence

WiFi-based presence detection for Home Assistant using OpenWrt APs.

Parses hostapd `AP-STA-CONNECTED` / `AP-STA-DISCONNECTED` events from your OpenWrt access points and publishes per-person home/away state and room location to Home Assistant via MQTT. No cloud, no Bluetooth beacons, no phone polling — just your existing WiFi infrastructure doing what it already knows: which devices are connected and where.

![Home Assistant room tracking history](docs/home-assistant-screenshot.png)

## 🧠 How it works

```
OpenWrt APs  -->  VictoriaLogs / Syslog  -->  openwrt-presence  -->  MQTT  -->  Home Assistant
  (hostapd)         (log source)              (state machine)      (discovery)   (device_tracker + sensor)
```

Each AP is configured as either an **exit** node (e.g. garden) or **interior** node (e.g. office, bedroom). Only exit nodes drive departure detection with a configurable timeout. Interior disconnects — phone doze, AP roaming, the 37 daily reconnections your iPhone does for no reason — are ignored.

### 🏠 HA entities created

For each person in the config:

- `device_tracker.<person>_wifi` — `home` / `not_home` (source type: `router`)
- `sensor.<person>_room` — current room name (e.g. `office`, `bedroom`)

## 🚀 Quick start

1. Copy the example files and edit them:

```bash
cp config.yaml.example config.yaml
cp Dockerfile.example Dockerfile
cp docker-compose.yaml.example docker-compose.yaml
# Edit config.yaml with your APs, people, and MAC addresses
```

2. Run with Docker Compose:

```bash
docker compose up -d
```

Or run directly:

```bash
pip install .
python -m openwrt_presence
```

Set `CONFIG_PATH` to use a custom config location (default: `config.yaml`).

The `.example` files are tracked by git; `config.yaml`, `Dockerfile`, and `docker-compose.yaml` are gitignored so you can customise them without dirtying the repo.

## ⚙️ Configuration

See [`config.yaml.example`](config.yaml.example) for a full example.

### 📡 Source adapters

**VictoriaLogs** (recommended) — tails the VictoriaLogs API for real-time events, with backfill on startup:

```yaml
source:
  type: victorialogs
  url: http://victorialogs:9428
```

**Syslog** — listens for UDP syslog directly from the APs:

```yaml
source:
  type: syslog
  listen: 0.0.0.0:514
```

### 🚪 Node types

- **`exit`** — AP near an exit (garden, front door). Disconnect starts a departure timer. Requires `timeout` in seconds.
- **`interior`** — AP inside the house. Disconnects are straight up ignored (phone doze, roaming between APs). Only connects update room.

### ⏱️ Global away timeout

`away_timeout` (seconds) is a safety net for devices that silently disappear without a proper disconnect — looking at you, every Android phone ever. Default: `64800` (18 hours).

## 🏡 Home Assistant integration

### Prerequisites

The [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) must be configured in HA with your Mosquitto broker. Entities are auto-discovered via MQTT Discovery — no manual HA configuration needed.

### 📱 Interaction with HA Companion App

HA's `person` entity prioritises non-GPS (router) trackers when they say `home`, but falls through to GPS when they say `not_home`. If GPS is stale, the person entity may stay `home` even after WiFi says `not_home`.

For automations that need fast departure detection (e.g. alarm arming), reference `device_tracker.<person>_wifi` directly instead of the `person` entity.

For people without the companion app (e.g. a housekeeper), `device_tracker.<person>_wifi` is the sole presence source.

### 🧩 Example: template sensor combining WiFi + GPS

```yaml
template:
  - binary_sensor:
      - name: "Alice Home"
        state: >
          {{ is_state('device_tracker.alice_wifi', 'home')
             or is_state('person.alice', 'home') }}
```

### 🔐 Example: arm alarm when everyone leaves

```yaml
automation:
  - alias: "Arm alarm when everyone leaves"
    trigger:
      - platform: state
        entity_id:
          - device_tracker.alice_wifi
          - device_tracker.bob_wifi
        to: "not_home"
    condition:
      - condition: state
        entity_id: device_tracker.alice_wifi
        state: "not_home"
      - condition: state
        entity_id: device_tracker.bob_wifi
        state: "not_home"
    action:
      - service: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.home_alarm
```

## 🕐 NTP and timezone on OpenWrt APs

All APs **must** have their timezone set to UTC and NTP enabled. The departure timer on exit nodes uses the event timestamp from syslog — clock skew or timezone mismatch will break timeout calculations. A 3-minute drift can completely eliminate a 2-minute grace period. Ask me how I know.

Since RFC3164 syslog carries no timezone information, VictoriaLogs assumes timestamps are UTC. An AP set to a local timezone (e.g. CET) will appear to have a 1–2 hour clock skew.

Room selection for multi-device users is resilient to clock skew (it uses processing order, not timestamps), but departure deadlines are not.

Verify timezone and NTP on OpenWrt:

```bash
uci show system.@system[0].timezone   # should be UTC0
uci show system.ntp
```

If NTP is not enabled:

```bash
uci set system.ntp.enabled='1'
uci commit system
/etc/init.d/sysntpd restart
```

## 🔒 Custom CA certificates

If VictoriaLogs is behind a reverse proxy with a private CA, uncomment the CA lines in your `Dockerfile`:

```dockerfile
COPY my-ca.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

The `SSL_CERT_FILE` env var is needed because `aiohttp` uses `certifi`'s bundle by default rather than the system store. Because of course it does.

## 🔧 Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

### 📺 Log monitor

A pretty-print CLI is included for watching the log stream in real time with ANSI colors — green for arrivals, red for departures, room names, device details:

```bash
docker container logs <container> -f 2>&1 | python3 src/openwrt_presence/monitor.py
```

Replace `<container>` with your actual container name or ID.

![openwrt-monitor in action](docs/monitor-screenshot.png)

## 📄 License

[MIT](LICENSE)
