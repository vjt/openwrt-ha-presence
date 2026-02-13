# OpenWrt Presence Detection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use 10x-engineer:executing-plans to implement this plan task-by-task.

**Goal:** Build a Python service that parses hostapd logs from OpenWrt APs, tracks WiFi presence per person, and publishes home/away + room state to Home Assistant via MQTT.

**Architecture:** Three-layer pipeline: pluggable log source adapters (VictoriaLogs tail, syslog) → presence engine (state machine with exit/interior node logic) → MQTT publisher (HA discovery + state). Pure async Python with `asyncio`.

**Tech Stack:** Python 3.11+, paho-mqtt, pyyaml, aiohttp, pytest, pytest-asyncio

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `src/openwrt_presence/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Modify: `.gitignore`

**Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "openwrt-presence"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "paho-mqtt>=2.0",
    "pyyaml>=6.0",
    "aiohttp>=3.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.setuptools.packages.find]
where = ["src"]
```

**Step 2: Create empty `src/openwrt_presence/__init__.py` and `tests/__init__.py`**

**Step 3: Create `tests/conftest.py`**

```python
import pytest

from openwrt_presence.config import Config


@pytest.fixture
def sample_config() -> Config:
    return Config.from_dict({
        "source": {"type": "victorialogs", "url": "http://localhost:9428"},
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "topic_prefix": "openwrt-presence",
        },
        "nodes": {
            "ap-garden": {"room": "garden", "type": "exit", "timeout": 120},
            "ap-office": {"room": "office", "type": "interior"},
            "ap-bedroom": {"room": "bedroom", "type": "interior"},
        },
        "away_timeout": 64800,
        "people": {
            "alice": {"macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]},
            "bob": {"macs": ["aa:bb:cc:dd:ee:03"]},
        },
    })
```

**Step 4: Update `.gitignore`**

Add:
```
__pycache__
*.egg-info
.venv
vmui_logs_export.jsonl
```

**Step 5: Install and verify**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest --co  # should collect 0 tests, no errors
```

**Step 6: Commit**

```
feat: project scaffolding with pyproject.toml and test fixtures
```

---

## Task 2: Configuration loading and validation

**Files:**
- Create: `src/openwrt_presence/config.py`
- Create: `tests/test_config.py`

**Step 1: Write failing tests**

```python
# tests/test_config.py
import pytest

from openwrt_presence.config import Config, ConfigError


class TestConfigLoading:
    def test_loads_valid_config(self, sample_config: Config):
        assert sample_config.away_timeout == 64800
        assert len(sample_config.people) == 2
        assert len(sample_config.nodes) == 3

    def test_node_properties(self, sample_config: Config):
        garden = sample_config.nodes["ap-garden"]
        assert garden.room == "garden"
        assert garden.type == "exit"
        assert garden.timeout == 120

        office = sample_config.nodes["ap-office"]
        assert office.type == "interior"
        assert office.timeout is None

    def test_person_mac_lookup(self, sample_config: Config):
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:01") == "alice"
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:02") == "alice"
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:03") == "bob"
        assert sample_config.mac_to_person("ff:ff:ff:ff:ff:ff") is None

    def test_mac_lookup_is_case_insensitive(self, sample_config: Config):
        assert sample_config.mac_to_person("AA:BB:CC:DD:EE:01") == "alice"

    def test_mac_lookup_normalizes_separators(self, sample_config: Config):
        assert sample_config.mac_to_person("AA-BB-CC-DD-EE-01") == "alice"


class TestConfigValidation:
    def test_rejects_duplicate_mac_across_people(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "interior"}},
                "away_timeout": 3600,
                "people": {
                    "alice": {"macs": ["aa:bb:cc:dd:ee:01"]},
                    "bob": {"macs": ["aa:bb:cc:dd:ee:01"]},
                },
            })

    def test_rejects_exit_node_without_timeout(self):
        with pytest.raises(ConfigError, match="timeout"):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "exit"}},
                "away_timeout": 3600,
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
            })

    def test_rejects_unknown_source_type(self):
        with pytest.raises(ConfigError, match="source"):
            Config.from_dict({
                "source": {"type": "nosql_blockchain"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "interior"}},
                "away_timeout": 3600,
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
            })

    def test_rejects_missing_people(self):
        with pytest.raises(ConfigError):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "interior"}},
                "away_timeout": 3600,
                "people": {},
            })

    def test_rejects_missing_nodes(self):
        with pytest.raises(ConfigError):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {},
                "away_timeout": 3600,
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
            })
```

**Step 2: Run tests, verify they fail**

```bash
pytest tests/test_config.py
```

Expected: `ModuleNotFoundError: No module named 'openwrt_presence.config'`

**Step 3: Implement `src/openwrt_presence/config.py`**

Dataclasses for `NodeConfig`, `PersonConfig`, `MqttConfig`, `SourceConfig`, `Config`. The `Config.from_dict()` classmethod validates and builds the config. `Config.mac_to_person()` does case-insensitive, separator-normalized lookup from a pre-built dict.

`ConfigError` is a plain `Exception` subclass.

`Config.from_yaml(path)` classmethod loads YAML and delegates to `from_dict()`.

**Step 4: Run tests, verify they pass**

```bash
pytest tests/test_config.py -v
```

**Step 5: Commit**

```
feat: config loading with validation and MAC lookup
```

---

## Task 3: Hostapd log parser

**Files:**
- Create: `src/openwrt_presence/parser.py`
- Create: `tests/test_parser.py`

**Step 1: Write failing tests**

```python
# tests/test_parser.py
from openwrt_presence.parser import parse_hostapd_message, PresenceEvent


class TestParseHostapdMessage:
    def test_parses_sta_connected_open(self):
        msg = "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        event = parse_hostapd_message(msg, "ap-kitchen")
        assert event is not None
        assert event.event == "connect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"
        assert event.node == "ap-kitchen"

    def test_parses_sta_connected_ft(self):
        msg = "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=ft"
        event = parse_hostapd_message(msg, "ap-garden")
        assert event is not None
        assert event.event == "connect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"

    def test_parses_sta_disconnected(self):
        msg = "phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:f0"
        event = parse_hostapd_message(msg, "ap-office")
        assert event is not None
        assert event.event == "disconnect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"

    def test_parses_phy0_interface(self):
        msg = "phy0-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        event = parse_hostapd_message(msg, "ap-kitchen")
        assert event is not None
        assert event.event == "connect"

    def test_ignores_irrelevant_messages(self):
        irrelevant = [
            "phy1-ap0: STA aa:bb:cc:dd:ee:f0 WPA: pairwise key handshake completed (RSN)",
            "phy1-ap0: STA aa:bb:cc:dd:ee:f0 IEEE 802.11: authenticated",
            "nl80211: kernel reports: key addition failed",
            "phy1-ap0: STA aa:bb:cc:dd:ee:f0 IEEE 802.11: associated (aid 3)",
        ]
        for msg in irrelevant:
            assert parse_hostapd_message(msg, "ap-kitchen") is None

    def test_parses_victorialogs_jsonl(self):
        """parse_victorialogs_line extracts hostname and message from JSONL."""
        import json
        line = json.dumps({
            "_time": "2026-02-12T23:23:23Z",
            "_msg": "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=ft",
            "tags.hostname": "ap-garden",
        })
        from openwrt_presence.parser import parse_victorialogs_line
        event = parse_victorialogs_line(line)
        assert event is not None
        assert event.event == "connect"
        assert event.node == "ap-garden"
        assert event.mac == "aa:bb:cc:dd:ee:f0"
```

**Step 2: Run tests, verify they fail**

```bash
pytest tests/test_parser.py
```

**Step 3: Implement `src/openwrt_presence/parser.py`**

`PresenceEvent` dataclass. `parse_hostapd_message(msg, node) -> PresenceEvent | None` uses a compiled regex to extract MAC from `AP-STA-CONNECTED` / `AP-STA-DISCONNECTED`. `parse_victorialogs_line(line) -> PresenceEvent | None` parses JSONL and delegates to `parse_hostapd_message`.

**Step 4: Run tests, verify they pass**

```bash
pytest tests/test_parser.py -v
```

**Step 5: Commit**

```
feat: hostapd log parser for connect/disconnect events
```

---

## Task 4: Presence engine — core state machine

This is the largest task. The engine is pure logic, no I/O, highly testable.

**Files:**
- Create: `src/openwrt_presence/engine.py`
- Create: `tests/test_engine.py`

**Step 1: Write failing tests for basic state transitions**

```python
# tests/test_engine.py
from datetime import datetime, timezone

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine, PersonState
from openwrt_presence.parser import PresenceEvent


def _event(event_type: str, mac: str, node: str, ts: datetime | None = None) -> PresenceEvent:
    return PresenceEvent(
        event=event_type,
        mac=mac,
        node=node,
        timestamp=ts or datetime.now(timezone.utc),
    )


def _ts(minutes: int = 0) -> datetime:
    """Create a timestamp offset by minutes from epoch for deterministic tests."""
    from datetime import timedelta
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)


class TestBasicTransitions:
    def test_connect_marks_person_home(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is True
        assert changes[0].room == "office"

    def test_unknown_mac_ignored(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_event("connect", "ff:ff:ff:ff:ff:ff", "ap-office", _ts(0)))
        assert changes == []

    def test_unknown_node_treated_as_interior(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "unknown-ap", _ts(0)))
        assert len(changes) == 1
        assert changes[0].home is True

    def test_disconnect_from_interior_keeps_home(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        changes = engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        # No state change — person is still home (departing from interior)
        assert changes == []

    def test_room_change_on_roaming(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-bedroom", _ts(1)))
        assert len(changes) == 1
        assert changes[0].room == "bedroom"
        assert changes[0].home is True


class TestExitNodeDeparture:
    def test_disconnect_from_exit_starts_timer(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        changes = engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        # No change yet — departure timer started, person still home
        assert changes == []

    def test_exit_timeout_marks_away(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        # Tick past the timeout (120 seconds = 2 minutes)
        changes = engine.tick(_ts(4))  # 4 minutes later
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False
        assert changes[0].room is None

    def test_reconnect_before_exit_timeout_cancels_departure(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        # Reconnect to different AP before timeout
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(2)))
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room == "office"
        # Tick past original timeout — should NOT trigger away
        changes = engine.tick(_ts(10))
        assert changes == []


class TestGlobalTimeout:
    def test_global_timeout_marks_away_from_interior(self, sample_config: Config):
        # Use a config with short global timeout for testing
        cfg = Config.from_dict({
            "source": {"type": "victorialogs", "url": "http://localhost:9428"},
            "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
            "nodes": {"ap-office": {"room": "office", "type": "interior"}},
            "away_timeout": 600,  # 10 minutes
            "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
        })
        engine = PresenceEngine(cfg)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        # Interior disconnect — no exit timer. But global timeout kicks in.
        changes = engine.tick(_ts(15))  # 15 minutes > 10 min timeout
        assert len(changes) == 1
        assert changes[0].home is False


class TestMultiDevicePerson:
    def test_person_home_if_any_device_connected(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-bedroom", _ts(1)))
        # Disconnect one device
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(2)))
        # Person still home via second device — no state change
        state = engine.get_person_state("alice")
        assert state.home is True

    def test_person_away_only_when_all_devices_away(self, sample_config: Config):
        cfg = Config.from_dict({
            "source": {"type": "victorialogs", "url": "http://localhost:9428"},
            "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
            "nodes": {"ap-garden": {"room": "garden", "type": "exit", "timeout": 60}},
            "away_timeout": 64800,
            "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]}},
        })
        engine = PresenceEngine(cfg)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:02", "ap-garden", _ts(1)))
        changes = engine.tick(_ts(5))  # past timeout
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False

    def test_room_follows_most_recent_device(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-bedroom", _ts(5)))
        state = engine.get_person_state("alice")
        assert state.room == "bedroom"  # most recent


class TestNoSpuriousChanges:
    def test_reconnect_to_same_node_no_change(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        # Already home in office — no change to publish
        assert changes == []

    def test_away_person_coming_home(self, sample_config: Config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        engine.tick(_ts(5))  # now away
        # Come back
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(10)))
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room == "garden"
```

**Step 2: Run tests, verify they fail**

```bash
pytest tests/test_engine.py
```

**Step 3: Implement `src/openwrt_presence/engine.py`**

Key types:
- `DeviceState` enum: `CONNECTED`, `DEPARTING`, `AWAY`
- `DeviceTracker` dataclass: holds per-MAC state, current node, last connect time, departure deadline (if any)
- `PersonState` dataclass: `home: bool`, `room: str | None`
- `StateChange` dataclass: `person: str`, `home: bool`, `room: str | None`, `mac: str`, `node: str`
- `PresenceEngine` class:
  - `process_event(event) -> list[StateChange]`: processes a single event, returns any person-level changes
  - `tick(now) -> list[StateChange]`: checks expired timers, returns changes
  - `get_person_state(name) -> PersonState`: returns current aggregated state

The engine is synchronous, time-independent (receives timestamps, doesn't call `datetime.now()`), and has no I/O.

**Step 4: Run tests, verify they pass**

```bash
pytest tests/test_engine.py -v
```

**Step 5: Commit**

```
feat: presence engine with exit/interior node departure logic
```

---

## Task 5: MQTT publisher

**Files:**
- Create: `src/openwrt_presence/mqtt.py`
- Create: `tests/test_mqtt.py`

**Step 1: Write failing tests**

Test the discovery payload generation and state publishing logic using a mock MQTT client.

```python
# tests/test_mqtt.py
import json
from unittest.mock import MagicMock

from openwrt_presence.config import Config
from openwrt_presence.engine import StateChange
from openwrt_presence.mqtt import MqttPublisher


class TestMqttDiscovery:
    def test_publishes_device_tracker_discovery(self, sample_config: Config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()

        # Find the device_tracker discovery call for alice
        calls = mock_client.publish.call_args_list
        tracker_calls = [c for c in calls if "device_tracker/alice_wifi/config" in str(c)]
        assert len(tracker_calls) == 1
        payload = json.loads(tracker_calls[0].args[1])
        assert payload["source_type"] == "router"
        assert payload["state_topic"] == "openwrt-presence/alice/state"

    def test_publishes_room_sensor_discovery(self, sample_config: Config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()

        calls = mock_client.publish.call_args_list
        sensor_calls = [c for c in calls if "sensor/alice_room/config" in str(c)]
        assert len(sensor_calls) == 1
        payload = json.loads(sensor_calls[0].args[1])
        assert payload["state_topic"] == "openwrt-presence/alice/room"


class TestMqttStatePublish:
    def test_publishes_home_state(self, sample_config: Config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office")
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        state_call = [c for c in calls if "alice/state" in str(c)]
        assert len(state_call) == 1
        assert state_call[0].args[1] == "home"

        room_call = [c for c in calls if "alice/room" in str(c)]
        assert len(room_call) == 1
        assert room_call[0].args[1] == "office"

    def test_publishes_away_state(self, sample_config: Config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden")
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        state_call = [c for c in calls if "alice/state" in str(c)]
        assert state_call[0].args[1] == "not_home"


class TestMqttLwt:
    def test_sets_lwt_on_construction(self, sample_config: Config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        mock_client.will_set.assert_called_once()
```

**Step 2: Run tests, verify they fail**

```bash
pytest tests/test_mqtt.py
```

**Step 3: Implement `src/openwrt_presence/mqtt.py`**

`MqttPublisher` class wrapping paho-mqtt. Methods:
- `publish_discovery()`: sends HA MQTT discovery config for all people
- `publish_state(change: StateChange)`: publishes state + room
- Constructor sets up LWT (Last Will and Testament) for availability

**Step 4: Run tests, verify they pass**

```bash
pytest tests/test_mqtt.py -v
```

**Step 5: Commit**

```
feat: MQTT publisher with HA discovery and LWT
```

---

## Task 6: VictoriaLogs source adapter

**Files:**
- Create: `src/openwrt_presence/sources/__init__.py`
- Create: `src/openwrt_presence/sources/victorialogs.py`
- Create: `tests/test_source_victorialogs.py`

**Step 1: Write failing tests**

Test the URL construction, query building, and response line parsing. Use `aiohttp` test utilities or mock the HTTP responses.

Key tests:
- Builds correct tail URL with LogsQL query
- Builds correct backfill query URL with time range
- Parses streamed JSONL lines into `PresenceEvent`s
- Skips malformed lines without crashing
- Reconnects on connection errors

**Step 2: Run tests, verify they fail**

**Step 3: Implement the adapter**

`VictoriaLogsSource` class:
- `async backfill(hours: int) -> AsyncIterator[PresenceEvent]`: queries `/select/logsql/query` for recent history
- `async tail() -> AsyncIterator[PresenceEvent]`: streams `/select/logsql/tail` with auto-reconnect
- Query is hardcoded: `_msg:~"AP-STA-CONNECTED|AP-STA-DISCONNECTED" AND tags.appname:"hostapd"`

**Step 4: Run tests, verify they pass**

**Step 5: Commit**

```
feat: VictoriaLogs source adapter with backfill and tail
```

---

## Task 7: Syslog source adapter

**Files:**
- Create: `src/openwrt_presence/sources/syslog.py`
- Create: `tests/test_source_syslog.py`

**Step 1: Write failing tests**

Key tests:
- Parses RFC3164 syslog messages extracting hostname and message
- Parses RFC5424 syslog messages
- Ignores non-hostapd messages
- Handles UDP datagrams

**Step 2: Run tests, verify they fail**

**Step 3: Implement the adapter**

`SyslogSource` class using `asyncio.DatagramProtocol`:
- Listens on configured UDP port
- Parses syslog header to extract hostname
- Delegates message body to `parse_hostapd_message()`
- Yields `PresenceEvent`s via an `asyncio.Queue`

**Step 4: Run tests, verify they pass**

**Step 5: Commit**

```
feat: syslog source adapter for direct AP log ingestion
```

---

## Task 8: Structured logging

**Files:**
- Create: `src/openwrt_presence/logging.py`
- Create: `tests/test_logging.py`

**Step 1: Write failing tests**

```python
# tests/test_logging.py
import json
import io

from openwrt_presence.logging import PresenceLogger
from openwrt_presence.engine import StateChange


class TestPresenceLogger:
    def test_logs_home_event_as_json(self):
        stream = io.StringIO()
        logger = PresenceLogger(stream)
        change = StateChange(person="alice", home=True, room="kitchen", mac="aa:bb:cc:dd:ee:01", node="ap-kitchen")
        logger.log_change(change)

        line = stream.getvalue().strip()
        data = json.loads(line)
        assert data["person"] == "alice"
        assert data["event"] == "home"
        assert data["room"] == "kitchen"
        assert "ts" in data

    def test_logs_away_event(self):
        stream = io.StringIO()
        logger = PresenceLogger(stream)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden")
        logger.log_change(change)

        data = json.loads(stream.getvalue().strip())
        assert data["event"] == "away"
        assert data.get("room") is None or data.get("last_node") == "ap-garden"
```

**Step 2: Run tests, verify they fail**

**Step 3: Implement structured JSON logger to stderr**

**Step 4: Run tests, verify they pass**

**Step 5: Commit**

```
feat: structured JSON logging for state changes
```

---

## Task 9: Main entrypoint and async orchestration

**Files:**
- Create: `src/openwrt_presence/__main__.py`
- Create: `config.example.yaml`
- Create: `Dockerfile`

**Step 1: Implement `__main__.py`**

The main loop:
1. Load config from `CONFIG_PATH` env var or `config.yaml`
2. Connect MQTT, publish discovery, set up LWT
3. Start source adapter (VictoriaLogs or syslog based on config)
4. If VictoriaLogs: run backfill first to reconstruct state
5. Process events from source → engine → publish changes + log
6. Run a periodic `tick()` every 30 seconds to check timer expiry
7. Handle SIGTERM/SIGINT for graceful shutdown

**Step 2: Create `config.example.yaml`**

Copy the example from the design doc (with alice/bob/eve).

**Step 3: Create `Dockerfile`**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .
CMD ["python", "-m", "openwrt_presence"]
```

**Step 4: Verify it starts**

```bash
python -m openwrt_presence --help  # or just verify it loads config and fails gracefully without MQTT
```

**Step 5: Commit**

```
feat: main entrypoint, Dockerfile, and example config
```

---

## Task 10: Integration test with log replay

**Files:**
- Create: `tests/test_integration.py`

**Step 1: Write integration test**

Replay a sequence of real-world-like events through the full pipeline (parser → engine) and verify the final person states and the sequence of state changes emitted.

Key scenarios to replay:
- Person arrives (connect to exit → connect to interior rooms)
- Person roams between rooms rapidly (802.11r)
- Person's phone goes to doze (disconnect from interior, no reconnect for an hour)
- Person leaves (disconnect from exit node, timeout expires)
- Multi-device person: one device dozes while other stays connected
- Unknown MAC addresses are ignored throughout

**Step 2: Run tests, verify they pass**

**Step 3: Commit**

```
test: integration test with realistic event replay
```

---

## Verification

After all tasks are complete:

```bash
# Run full test suite
pytest -v

# Verify Docker build
docker build -t openwrt-presence .

# Verify it starts with example config (will fail to connect to MQTT, that's OK)
docker run --rm -v $(pwd)/config.example.yaml:/app/config.yaml openwrt-presence
```
