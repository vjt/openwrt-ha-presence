# OpenWrt Presence Detection for Home Assistant

## Overview

Use OpenWrt APs as WiFi-based presence detectors for Home Assistant. The system
parses hostapd logs to detect when known devices (identified by MAC address)
associate or disassociate from specific APs, maps them to people and rooms, and
publishes presence state to Home Assistant via MQTT.

## Architecture

```
┌─────────────┐     syslog      ┌──────────┐     ┌────────────────┐
│ OpenWrt APs │ ───────────────→│ telegraf  │────→│ VictoriaLogs   │
│ (6x)        │                 └──────────┘     └───────┬────────┘
└─────────────┘                                          │
                                                    tail API
                                                         │
                                              ┌──────────▼─────────┐
                                              │  openwrt-presence   │
                                              │  (Python, Docker)   │
                                              │                     │
                                              │  ┌───────────────┐  │
                                              │  │ Log Source     │  │
                                              │  │ (adapter)      │  │
                                              │  └───────┬───────┘  │
                                              │          │          │
                                              │  ┌───────▼───────┐  │
                                              │  │ Presence       │  │
                                              │  │ Engine         │  │
                                              │  └───────┬───────┘  │
                                              │          │          │
                                              │  ┌───────▼───────┐  │
                                              │  │ MQTT Publisher │  │
                                              │  └───────┬───────┘  │
                                              └──────────┼─────────┘
                                                         │
                                                    MQTT │
                                                         ▼
                                              ┌────────────────────┐
                                              │  Home Assistant     │
                                              │  - device_tracker   │
                                              │  - sensor (room)    │
                                              └────────────────────┘
```

### Components

**Log Source (adapter interface)**: Pluggable. Ships with two adapters:

- `VictoriaLogsSource`: Tails the VictoriaLogs `/select/logsql/tail` API for
  real-time hostapd events. Only needs the VictoriaLogs URL; the query is built
  internally using shared hostapd event patterns.
- `SyslogSource`: Listens on a UDP/TCP port for syslog messages directly. For
  users who don't run VictoriaLogs.

Both adapters emit the same normalized event:

```python
@dataclass
class PresenceEvent:
    event: Literal["connect", "disconnect"]
    mac: str          # e.g. "aa:bb:cc:dd:ee:f0"
    node: str         # AP hostname, e.g. "ap-kitchen"
    timestamp: datetime
```

The hostapd patterns to match are shared constants, not per-adapter config:

- `AP-STA-CONNECTED <mac>` (with `auth_alg=ft` or `auth_alg=open`)
- `AP-STA-DISCONNECTED <mac>`

Both `phy0-ap0` (2.4GHz) and `phy1-ap0` (5GHz) interfaces are handled — the
parser extracts only the MAC and hostname, ignoring the interface name.

**Presence Engine**: Stateful core. Tracks per-device (MAC) association state,
applies departure logic based on node type (exit vs interior), maps MACs to
people and nodes to rooms. Emits presence state changes.

**MQTT Publisher**: Publishes HA MQTT discovery config on startup to
auto-register entities, then publishes state updates on change.

## Configuration

Single YAML file (`config.yaml`):

```yaml
source:
  type: victorialogs
  url: "http://victorialogs:9428"
  # type: syslog
  # listen: "0.0.0.0:5514"

mqtt:
  host: "homeassistant.local"
  port: 1883
  username: "mqtt_user"
  password: "mqtt_pass"
  topic_prefix: "openwrt-presence"

nodes:
  ap-garden:
    room: garden
    type: exit
    timeout: 120          # seconds before marking away after disconnect
  ap-office:
    room: office
    type: interior
  ap-bedroom:
    room: bedroom
    type: interior
  ap-livingroom:
    room: livingroom
    type: interior
  ap-kitchen:
    room: kitchen
    type: interior
  ap-laundry:
    room: laundry_room
    type: interior

away_timeout: 64800       # 18 hours - global safety net

people:
  alice:
    macs:
      - "a4:c3:f0:85:7b:2e"
      - "d8:f2:ca:91:3d:6a"
  bob:
    macs:
      - "3c:e0:72:4f:aa:19"
  eve:
    macs:
      - "f0:18:98:c7:5e:b3"
```

### Node types

- **`exit`**: Disconnect from this node triggers a departure timer. When the
  timer expires and the device hasn't reconnected to any node, the person is
  marked away. Timeout is configured per-node (e.g. garden AP = 2 minutes).
- **`interior`**: Tracks which room the person is in. Does NOT drive home/away
  transitions. No timeout needed.

### Timeouts

There are two distinct timeout mechanisms:

**Exit node timeout** (`timeout` on exit nodes): Short, per-node. When a device
disconnects from an exit node, a timer starts counting down. If the device
reconnects to any node before the timer expires, it is cancelled. If it
expires, the device is marked as away. This is the primary mechanism for fast
departure detection. Only exit nodes have this field. Typical values: 1-5
minutes.

**Global away timeout** (`away_timeout`): Long, applies to all nodes. When a
device is disconnected from all nodes (regardless of type), a global timer
tracks the total time since the last connection on any node. If this timer
exceeds `away_timeout`, the device is marked as away. This is a safety net for
the rare case where someone leaves without passing through an exit node. It
must be long enough to survive overnight phone sleep (7-11 hours observed).
Typical values: 12-24 hours.

Interior nodes have no timeout of their own. A device that disconnects from an
interior node stays in "departing" state indefinitely (maintaining its last
known room) until either it reconnects somewhere, departs via an exit node, or
the global timeout expires.

### Design rationale for node types

Analysis of 7 days of hostapd logs (45K+ events, 4 phones) showed that phones
disconnect WiFi during sleep/doze regularly:

- Overnight: gaps of 7-11 hours (phone sleeping)
- Daytime: gaps of 20-180 minutes (doze mode)

Any timeout short enough to be useful for departure detection (< 30 min) would
cause false "away" triggers multiple times per day. The exit node model solves
this: only the garden AP (which everyone passes through when leaving) drives
departure detection with a short timeout. Interior APs never trigger departure.

The `away_timeout` (18 hours) is a safety net for the unlikely case where
someone leaves without passing an exit node and all their devices go silent.

## Requirements and Limitations

**Exit node required for reliable home/away detection.** This system can only
provide fast, reliable home/away presence detection if at least one AP is
designated as an exit node — an AP whose coverage area everyone must pass
through when leaving the premises. Without an exit node, the system can still
track room-level presence (which room a person is in), but home/away detection
falls back to the global timeout (hours), making it impractical for
time-sensitive automations like alarm arming.

If your network topology does not include an AP on the exit path, you should
use this system only for room tracking and rely on a separate mechanism (e.g.
HA Companion App GPS) for home/away detection.

**Why a simple disconnect timeout doesn't work.** Phones regularly disconnect
from WiFi during sleep and doze cycles — gaps of 20 minutes to 11 hours were
observed while the person was clearly still at home. Any timeout short enough
for useful departure detection (< 30 minutes) would produce multiple false
"away" events per day.

## Presence Engine State Machine

### Per-device (MAC) states

```
                 AP-STA-CONNECTED (any node)
    ┌──────────────────────────────────────────┐
    │                                          ▼
 AWAY ◄──── exit timeout expires ──── CONNECTED(node X)
    ▲        or global timeout                 │
    │                                          │ AP-STA-DISCONNECTED (node X)
    │                                          ▼
    │                                   DEPARTING(node X)
    │                                     if exit node: timer = node timeout
    │                                     if interior: no departure timer
    │                                          │
    │         exit timeout expires             │ AP-STA-CONNECTED (node Y)
    └──────────────────────────────────────────│──→ cancel timer
                                               ▼
                                         CONNECTED(node Y)
```

Three states per device:

- **CONNECTED(node)**: device is associated to a specific node.
- **DEPARTING(node)**: device disconnected. If the node is an exit node, a
  departure timer is running. If interior, no timer — just waiting for the
  next connection event.
- **AWAY**: departure timer expired (exit node) or global timeout expired.

### Transitions

- **Roaming** (DISCONNECT from X, CONNECT to Y within seconds): DEPARTING(X) →
  cancel any timer → CONNECTED(Y). Room changes, person never goes away.
- **Exit departure**: CONNECTED(exit node) → DEPARTING → exit timer expires →
  AWAY.
- **Interior disconnect**: CONNECTED(interior) → DEPARTING (no timer). Room
  stays as last known. Only goes AWAY if global timeout expires.
- **Sleep/doze**: DEPARTING(interior) → phone reconnects minutes/hours later →
  CONNECTED. No state change published.

### Per-person aggregation

```
person.home  = ANY of their MACs is CONNECTED or DEPARTING
person.away  = ALL of their MACs are AWAY
person.room  = room of the most recently CONNECTED MAC
```

A person is "home" even while devices are in DEPARTING state — they only go
"away" when departure is confirmed. Room persists as "last connected room"
indefinitely until the person connects to a different AP or goes away.

### What triggers MQTT publishes

- **home/away change**: only on actual transitions, not on every roaming event
- **room change**: only when the resolved room actually differs from the
  previous one
- Rapid roaming events (common with 802.11r) are naturally debounced by this
  model: connect/disconnect/connect sequences update internal state but only
  publish if the resulting person-level state changed.

## Home Assistant Integration

### Entities per tracked person

Each person gets two entities, auto-registered via MQTT Discovery:

1. **`device_tracker.<person>_wifi`** — `source_type: router`, reports
   `home` / `not_home`
2. **`sensor.<person>_room`** — reports the room name (e.g. `bedroom`,
   `kitchen`) or empty when away

### Why device_tracker (not binary_sensor)

`device_tracker` is HA's native entity type for presence. It integrates with
the `person` entity, dashboards, and existing automations.

### Interaction with HA Companion App GPS tracker

HA's `person` entity uses this priority logic (from HA source code):

```
if any non-GPS tracker says "home" → person is "home"
elif any GPS tracker has a state   → use that
else                               → use latest "not_home"
```

When the WiFi tracker (non-GPS/router) says `not_home`, HA falls through to
GPS. If the companion app GPS is stale (no cell coverage around the house), the
`person` entity stays `home` until GPS catches up.

**Recommendation**: For automations that need fast departure detection (e.g.
alarm arming), reference `device_tracker.<person>_wifi` directly instead of the
`person` entity. Alternatively, create a template binary sensor combining both:

```yaml
template:
  - binary_sensor:
      - name: "Alice Home"
        state: >
          {{ is_state('device_tracker.alice_wifi', 'home')
             or is_state('person.alice', 'home') }}
```

This gives fast departure (WiFi) while keeping GPS as a fallback for arrival
when WiFi is slow.

For people without the companion app (e.g. a housekeeper),
`device_tracker.eve_wifi` is the sole presence source — no template needed.

### MQTT Topics

Using MQTT Discovery, the service publishes config and state to:

```
homeassistant/device_tracker/<person>_wifi/config   # discovery payload
openwrt-presence/<person>/state                     # "home" or "not_home"

homeassistant/sensor/<person>_room/config           # discovery payload
openwrt-presence/<person>/room                      # room name or ""
```

### Example automation: arm alarm when everyone leaves

```yaml
automation:
  - alias: "Arm alarm when everyone leaves"
    description: >
      Arms the alarm when all tracked people are away according to WiFi
      presence. Uses device_tracker entities directly for fast departure
      detection instead of the person entity (which waits for GPS).
    trigger:
      - platform: state
        entity_id:
          - device_tracker.alice_wifi
          - device_tracker.bob_wifi
          - device_tracker.eve_wifi
        to: "not_home"
    condition:
      - condition: state
        entity_id: device_tracker.alice_wifi
        state: "not_home"
      - condition: state
        entity_id: device_tracker.bob_wifi
        state: "not_home"
      - condition: state
        entity_id: device_tracker.eve_wifi
        state: "not_home"
    action:
      - service: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.home_alarm
```

The trigger fires when any person goes `not_home`, and the condition block
ensures all people are `not_home` before arming. This way the alarm is only
armed when the last person leaves.

## Startup Behavior

On startup, the service has no prior state. To avoid falsely reporting everyone
as "away" until the next connect event:

- **VictoriaLogs adapter**: queries the last 4 hours of logs to reconstruct
  current state before switching to live tail.
- **Syslog adapter**: no backfill possible; starts with unknown state and
  resolves on first events.

## Logging

State changes are logged as structured JSON to stderr for consumption by
Grafana, VictoriaLogs, or `docker logs`:

```json
{"ts": "2026-02-13T10:00:00Z", "person": "alice", "event": "home", "room": "kitchen", "mac": "a4:c3:f0:85:7b:2e", "node": "ap-kitchen"}
{"ts": "2026-02-13T10:05:00Z", "person": "alice", "event": "room_change", "room": "office", "mac": "a4:c3:f0:85:7b:2e", "node": "ap-office"}
{"ts": "2026-02-13T18:30:00Z", "person": "alice", "event": "away", "last_room": "garden", "mac": "a4:c3:f0:85:7b:2e", "node": "ap-garden"}
```

## Deployment

Docker container on the rpi5 alongside telegraf, VictoriaLogs, etc.
Config file mounted as a volume.

## Technology

- **Language**: Python
- **Dependencies**: paho-mqtt, pyyaml, aiohttp (for VictoriaLogs API)
