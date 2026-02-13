# openwrt-presence

WiFi-based presence detection for Home Assistant using OpenWrt APs.

Parses hostapd `AP-STA-CONNECTED` / `AP-STA-DISCONNECTED` events from your OpenWrt access points and publishes per-person home/away state and room location to Home Assistant via MQTT.

## How it works

```
OpenWrt APs  -->  VictoriaLogs / Syslog  -->  openwrt-presence  -->  MQTT  -->  Home Assistant
  (hostapd)         (log source)              (state machine)      (discovery)   (device_tracker + sensor)
```

Each AP is configured as either an **exit** node (e.g. garden) or **interior** node (e.g. office, bedroom). Only exit nodes drive departure detection with a short timeout. Interior disconnects (phone doze, roaming) are ignored, avoiding false "away" triggers.

### HA entities created

For each person in the config:

- `device_tracker.<person>_wifi` -- `home` / `not_home` (source type: `router`)
- `sensor.<person>_room` -- current room name (e.g. `office`, `bedroom`)

## Quick start

1. Copy and edit the example config:

```bash
cp config.example.yaml config.yaml
# Edit config.yaml with your APs, people, and MAC addresses
```

2. Run with Docker:

```bash
docker build -t openwrt-presence .
docker run -d \
  -v $(pwd)/config.yaml:/app/config.yaml \
  --name openwrt-presence \
  openwrt-presence
```

Or run directly:

```bash
pip install .
python -m openwrt_presence
```

Set `CONFIG_PATH` to use a custom config location (default: `config.yaml`).

## Configuration

See [`config.example.yaml`](config.example.yaml) for a full example.

### Source adapters

**VictoriaLogs** (recommended) -- tails the VictoriaLogs API for real-time events, with backfill on startup:

```yaml
source:
  type: victorialogs
  url: http://victorialogs:9428
```

**Syslog** -- listens for UDP syslog directly from the APs:

```yaml
source:
  type: syslog
  listen: 0.0.0.0:514
```

### Node types

- **`exit`** -- AP near an exit (garden, front door). Disconnect starts a departure timer. Requires `timeout` in seconds.
- **`interior`** -- AP inside the house. Disconnects are ignored (phone doze, roaming between APs).

### Global away timeout

`away_timeout` (seconds) is a safety net. If a device hasn't reconnected to any AP within this duration after its last connection, it's marked away. Default: `64800` (18 hours).

## Home Assistant integration

### Prerequisites

The [MQTT integration](https://www.home-assistant.io/integrations/mqtt/) must be configured in HA with your Mosquitto broker. Entities are auto-discovered via MQTT Discovery -- no manual HA configuration needed.

### Interaction with HA Companion App

HA's `person` entity prioritises non-GPS (router) trackers when they say `home`, but falls through to GPS when they say `not_home`. If GPS is stale, the person entity may stay `home` even after WiFi says `not_home`.

For automations that need fast departure detection (e.g. alarm arming), reference `device_tracker.<person>_wifi` directly instead of the `person` entity.

For people without the companion app (e.g. a housekeeper), `device_tracker.<person>_wifi` is the sole presence source.

### Example: template sensor combining WiFi + GPS

```yaml
template:
  - binary_sensor:
      - name: "Alice Home"
        state: >
          {{ is_state('device_tracker.alice_wifi', 'home')
             or is_state('person.alice', 'home') }}
```

### Example: arm alarm when everyone leaves

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

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

## License

[MIT](LICENSE)
