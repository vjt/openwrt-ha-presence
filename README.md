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

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

## License

[MIT](LICENSE)
