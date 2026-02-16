# Direct AP Scraping Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use 10x-engineer:executing-plans to implement this plan task-by-task.

**Goal:** Replace the VictoriaMetrics/Prometheus TSDB polling with direct HTTP scraping of the `prometheus-node-exporter-lua` endpoints on each AP, eliminating the telegraf pipeline latency.

**Architecture:** The source adapter HTTP GETs each AP's `/metrics` endpoint in parallel each poll cycle, parses the Prometheus text exposition format for `wifi_station_signal_dbm` lines, and returns `StationReading`s. Each node can optionally specify a custom `url` for its exporter (to handle custom IPs, ports, or paths); otherwise the URL is derived from the node name and a global `exporter_port`. No TSDB, no PromQL, no intermediary.

**Tech Stack:** Python 3.12, aiohttp, pytest, Prometheus text exposition format

---

## What changes

| Before | After |
|--------|-------|
| `PrometheusSource` queries VictoriaMetrics JSON API | `ExporterSource` scrapes AP `/metrics` endpoints directly |
| `source:` config section (type, url) | `exporter_port:` global default + per-node `url:` override |
| `lookback:` config field | Removed (no TSDB staleness) |
| `tracked_macs` property (uppercase MACs for PromQL) | Removed (filtering happens in Python) |
| PromQL regex query building | Prometheus text format line parsing |
| Single HTTP request per cycle | Parallel HTTP requests (one per AP) |
| Pipeline: AP → telegraf → VictoriaMetrics → us | Pipeline: AP → us |

## What stays the same

- `StationReading(mac, ap, rssi)` dataclass
- `engine.py` — unchanged, receives `list[StationReading]` as before
- `mqtt.py`, `logging.py`, `monitor.py` — unchanged
- Poll loop structure in `__main__.py`
- All engine tests and integration tests (only config fixture changes)

---

### Task 1: Config — remove source/lookback, add exporter_port and per-node url

**Files:**
- Modify: `src/openwrt_presence/config.py`
- Test: `tests/test_config.py`
- Modify: `tests/conftest.py`

**Step 1: Update test_config.py — adjust validation tests**

Remove the `test_rejects_unknown_source_type` test. Remove `test_tracked_macs_returns_uppercase`. Add `test_exporter_port_default`, `test_exporter_port_custom`, `test_node_url_default`, and `test_node_url_override`. Update `_base_config()` to remove `source` key.

```python
# In _base_config(), replace the full function:
def _base_config(**overrides):
    """Return a valid config dict, with optional overrides."""
    cfg = {
        "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
        "nodes": {"ap1": {"room": "room1"}},
        "departure_timeout": 120,
        "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
    }
    cfg.update(overrides)
    return cfg

# Remove test_rejects_unknown_source_type entirely.

# Remove test_tracked_macs_returns_uppercase entirely.

# Add to TestConfigLoading:
    def test_exporter_port_default(self, sample_config: Config):
        assert sample_config.exporter_port == 9100

    def test_exporter_port_custom(self):
        cfg = Config.from_dict(_base_config(exporter_port=9200))
        assert cfg.exporter_port == 9200

    def test_node_url_default(self, sample_config: Config):
        assert sample_config.nodes["pingu"].url is None

    def test_node_url_override(self):
        cfg = Config.from_dict(_base_config(nodes={
            "ap1": {"room": "room1", "url": "http://192.168.1.10:9100/metrics"},
        }))
        assert cfg.nodes["ap1"].url == "http://192.168.1.10:9100/metrics"

    def test_node_urls_property(self):
        cfg = Config.from_dict(_base_config(
            nodes={
                "ap1": {"room": "room1"},
                "ap2": {"room": "room2", "url": "http://10.0.0.5:9200/metrics"},
            },
            exporter_port=9100,
        ))
        urls = cfg.node_urls
        assert urls["ap1"] == "http://ap1:9100/metrics"
        assert urls["ap2"] == "http://10.0.0.5:9200/metrics"
```

**Step 2: Update conftest.py — remove source from sample_config**

```python
@pytest.fixture
def sample_config() -> Config:
    return Config.from_dict({
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "topic_prefix": "openwrt-presence",
        },
        "nodes": {
            "albert": {"room": "bedroom"},
            "pingu": {"room": "office"},
            "mowgli": {"room": "garden"},
        },
        "departure_timeout": 120,
        "people": {
            "alice": {"macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]},
            "bob": {"macs": ["aa:bb:cc:dd:ee:03"]},
        },
    })
```

**Step 3: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_config.py -v
```

Expected: FAIL — config.py still requires `source` section and has no `exporter_port` or `node_urls`.

**Step 4: Update config.py — remove SourceConfig, add exporter_port and per-node url**

Remove: `_VALID_SOURCE_TYPES`, `SourceConfig` dataclass, `tracked_macs` property, `source` field from `Config`, all source-related parsing in `from_dict()`, `lookback` field and its parsing.

Add `url: str | None = None` to `NodeConfig`.

Add `exporter_port: int = 9100` to `Config`.

Add `node_urls` property to `Config`:

```python
    @property
    def node_urls(self) -> dict[str, str]:
        """Return resolved metrics URLs for all nodes.

        Uses the node's ``url`` if set, otherwise constructs
        ``http://{name}:{exporter_port}/metrics``.
        """
        return {
            name: node.url or f"http://{name}:{self.exporter_port}/metrics"
            for name, node in self.nodes.items()
        }
```

The resulting `NodeConfig`:

```python
@dataclass(frozen=True)
class NodeConfig:
    room: str
    url: str | None = None
```

The resulting `Config` dataclass:

```python
@dataclass
class Config:
    mqtt: MqttConfig
    nodes: dict[str, NodeConfig]
    people: dict[str, PersonConfig]
    departure_timeout: int
    poll_interval: int = 30
    exporter_port: int = 9100
    _mac_lookup: dict[str, str] = field(default_factory=dict, repr=False)
```

In `from_dict()`, remove the `# --- source ---` block and `# --- lookback ---` block. Update node parsing to include `url`:

```python
        for name, ndata in nodes_raw.items():
            nodes[name] = NodeConfig(room=ndata["room"], url=ndata.get("url"))
```

Add:

```python
        # --- exporter_port ---
        exporter_port: int = data.get("exporter_port", 9100)
```

Update the `return cls(...)` call: remove `source=source` and `lookback=lookback`, add `exporter_port=exporter_port`.

Remove the `Literal` import (no longer used). Remove `SourceConfig` entirely.

**Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_config.py -v
```

Expected: PASS

**Step 6: Update test_integration.py — remove source from _make_config**

In `tests/test_integration.py`, update `_make_config()` to remove the `"source"` key:

```python
def _make_config() -> Config:
    return Config.from_dict({
        "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
        "nodes": {
            "mowgli": {"room": "garden"},
            "pingu": {"room": "office"},
            "albert": {"room": "bedroom"},
            "golem": {"room": "livingroom"},
            "gordon": {"room": "kitchen"},
        },
        "departure_timeout": 120,
        "people": {
            "alice": {
                "macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            },
            "bob": {
                "macs": ["aa:bb:cc:dd:ee:03"],
            },
        },
    })
```

**Step 7: Run full test suite**

```bash
.venv/bin/pytest -v
```

Expected: All tests pass except the prometheus source tests (which will be replaced in Task 2).

**Step 8: Commit**

```bash
git add src/openwrt_presence/config.py tests/test_config.py tests/conftest.py tests/test_integration.py && git commit -m "refactor: remove source/lookback config, add exporter_port and per-node url"
```

---

### Task 2: ExporterSource — scrape AP /metrics endpoints

**Files:**
- Create: `src/openwrt_presence/sources/exporters.py`
- Create: `tests/test_source_exporters.py`
- Delete: `src/openwrt_presence/sources/prometheus.py`
- Delete: `tests/test_source_prometheus.py`

**Step 1: Write test_source_exporters.py — text parsing tests**

These tests cover the Prometheus text exposition format parser. No HTTP mocking needed for the parser tests — they're pure functions. The `ExporterSource` constructor takes `node_urls: dict[str, str]` (resolved URLs) and `tracked_macs: set[str]`.

```python
"""Tests for ExporterSource."""

from __future__ import annotations

from openwrt_presence.sources.exporters import ExporterSource


def _make_source(
    node_urls: dict[str, str] | None = None,
    tracked_macs: set[str] | None = None,
) -> ExporterSource:
    return ExporterSource(
        node_urls=node_urls or {
            "pingu": "http://pingu:9100/metrics",
            "albert": "http://albert:9100/metrics",
        },
        tracked_macs=tracked_macs or {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"},
    )


SAMPLE_METRICS = """\
# HELP wifi_station_signal_dbm Signal strength of associated stations
# TYPE wifi_station_signal_dbm gauge
wifi_station_signal_dbm{ifname="phy1-ap0",mac="AA:BB:CC:11:22:01"} -55
wifi_station_signal_dbm{ifname="phy1-ap0",mac="AA:BB:CC:11:22:04"} -42
wifi_station_signal_dbm{ifname="phy0-ap0",mac="AA:BB:CC:11:22:05"} -63
# HELP node_cpu_seconds_total CPU time
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 123456.78
"""


class TestMetricsParsing:
    def test_parses_wifi_station_lines(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "pingu")
        assert len(readings) == 3

    def test_extracts_mac_lowercase(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "pingu")
        macs = {r.mac for r in readings}
        assert "aa:bb:cc:11:22:01" in macs
        assert "aa:bb:cc:11:22:04" in macs

    def test_extracts_rssi_as_int(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "pingu")
        by_mac = {r.mac: r for r in readings}
        assert by_mac["aa:bb:cc:11:22:01"].rssi == -55
        assert by_mac["aa:bb:cc:11:22:04"].rssi == -42

    def test_ap_name_set_from_argument(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "albert")
        assert all(r.ap == "albert" for r in readings)

    def test_ignores_non_wifi_metrics(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "pingu")
        # Only wifi_station_signal_dbm lines, not node_cpu_seconds_total
        assert all(r.rssi < 0 for r in readings)  # dBm values are negative

    def test_empty_metrics(self):
        readings = ExporterSource._parse_metrics("", "pingu")
        assert readings == []

    def test_no_wifi_metrics(self):
        text = "node_cpu_seconds_total{cpu=\"0\"} 123.45\n"
        readings = ExporterSource._parse_metrics(text, "pingu")
        assert readings == []

    def test_handles_float_rssi(self):
        """Some exporters emit floats like -55.0."""
        text = 'wifi_station_signal_dbm{ifname="phy1-ap0",mac="AA:BB:CC:DD:EE:01"} -55.7\n'
        readings = ExporterSource._parse_metrics(text, "pingu")
        assert readings[0].rssi == -55

    def test_filters_to_tracked_macs(self):
        source = _make_source(tracked_macs={"aa:bb:cc:11:22:01"})
        readings = source._filter_tracked(
            ExporterSource._parse_metrics(SAMPLE_METRICS, "pingu")
        )
        assert len(readings) == 1
        assert readings[0].mac == "aa:bb:cc:11:22:01"
```

**Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_source_exporters.py -v
```

Expected: FAIL — `openwrt_presence.sources.exporters` does not exist yet.

**Step 3: Write exporters.py — the source adapter**

```python
"""Source adapter that scrapes prometheus-node-exporter-lua on each AP."""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from openwrt_presence.engine import StationReading

logger = logging.getLogger(__name__)

_METRIC_RE = re.compile(
    r'^wifi_station_signal_dbm\{[^}]*mac="([^"]+)"[^}]*\}\s+(-?\d+(?:\.\d+)?)\s*$',
    re.MULTILINE,
)


class ExporterSource:
    """Scrapes /metrics on each AP for wifi_station_signal_dbm."""

    def __init__(self, node_urls: dict[str, str], tracked_macs: set[str]) -> None:
        self._node_urls = node_urls
        self._tracked_macs = {m.lower() for m in tracked_macs}

    async def query(self) -> list[StationReading]:
        """Scrape all APs in parallel and return station readings.

        APs that fail to respond are silently skipped (logged as warning).
        """
        readings: list[StationReading] = []
        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = {
                node: asyncio.create_task(self._scrape_ap(session, node, url))
                for node, url in self._node_urls.items()
            }
            for node, task in tasks.items():
                try:
                    ap_readings = await task
                    readings.extend(ap_readings)
                except Exception as exc:
                    logger.warning("Failed to scrape %s: %s", node, exc)

        return self._filter_tracked(readings)

    async def _scrape_ap(
        self, session: aiohttp.ClientSession, node: str, url: str
    ) -> list[StationReading]:
        """Scrape a single AP and parse its metrics."""
        async with session.get(url) as response:
            text = await response.text()
        return self._parse_metrics(text, node)

    @staticmethod
    def _parse_metrics(text: str, ap: str) -> list[StationReading]:
        """Parse Prometheus text exposition format for wifi RSSI metrics."""
        readings: list[StationReading] = []
        for match in _METRIC_RE.finditer(text):
            mac = match.group(1).lower().replace("-", ":")
            rssi = int(float(match.group(2)))
            readings.append(StationReading(mac=mac, ap=ap, rssi=rssi))
        return readings

    def _filter_tracked(self, readings: list[StationReading]) -> list[StationReading]:
        """Keep only readings for tracked MACs."""
        return [r for r in readings if r.mac in self._tracked_macs]
```

**Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_source_exporters.py -v
```

Expected: PASS

**Step 5: Delete old prometheus source and tests**

```bash
rm src/openwrt_presence/sources/prometheus.py tests/test_source_prometheus.py
```

**Step 6: Run full test suite**

```bash
.venv/bin/pytest -v
```

Expected: FAIL in `__main__.py` import (still imports `PrometheusSource`). That's fixed in Task 3.

**Step 7: Commit**

```bash
git add src/openwrt_presence/sources/exporters.py tests/test_source_exporters.py && git add -u && git commit -m "feat: add ExporterSource, delete PrometheusSource"
```

---

### Task 3: Main entrypoint — wire up ExporterSource

**Files:**
- Modify: `src/openwrt_presence/__main__.py`

**Step 1: Update __main__.py**

Replace the `PrometheusSource` import and instantiation:

```python
# Change import from:
from openwrt_presence.sources.prometheus import PrometheusSource
# To:
from openwrt_presence.sources.exporters import ExporterSource
```

Replace the source creation block (the assert + PrometheusSource constructor) with:

```python
    # Create source adapter — scrapes each AP's /metrics endpoint directly
    source = ExporterSource(
        node_urls=config.node_urls,
        tracked_macs={
            mac
            for person_cfg in config.people.values()
            for mac in person_cfg.macs
        },
    )
```

Remove the `assert config.source.url is not None` line.

**Step 2: Run full test suite**

```bash
.venv/bin/pytest -v
```

Expected: All tests pass.

**Step 3: Commit**

```bash
git add src/openwrt_presence/__main__.py && git commit -m "feat: wire ExporterSource into poll loop"
```

---

### Task 4: Update config.yaml.example and docs

**Files:**
- Modify: `config.yaml.example`
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Step 1: Rewrite config.yaml.example**

```yaml
mqtt:
  host: mosquitto
  port: 1883
  topic_prefix: openwrt-presence
  # username: optional
  # password: optional

nodes:
  ap-garden:
    room: garden
  ap-office:
    room: office
  ap-bedroom:
    room: bedroom
    # url: http://192.168.1.50:9100/metrics   # override if no DNS or custom port
  ap-livingroom:
    room: livingroom
  ap-kitchen:
    room: kitchen
  ap-laundry:
    room: laundry_room

departure_timeout: 120  # seconds without signal before marking away
poll_interval: 5         # seconds between AP scrapes
exporter_port: 9100      # default prometheus-node-exporter-lua port

people:
  alice:
    macs:
      - "AA:BB:CC:DD:EE:01"
      - "AA:BB:CC:DD:EE:02"
  bob:
    macs:
      - "AA:BB:CC:DD:EE:03"
```

**Step 2: Update CLAUDE.md**

Update the architecture diagram:

```
OpenWrt APs → openwrt-presence → MQTT → Home Assistant
 (node-exporter-lua)  (state machine)   (discovery)  (device_tracker + sensor)
```

Update the data flow:

> 1. **Source** (`sources/exporters.py`) scrapes `/metrics` on each AP via HTTP, parses Prometheus text exposition format for `wifi_station_signal_dbm`, returns `StationReading(mac, ap, rssi)`

Update key files: replace `sources/prometheus.py` reference with `sources/exporters.py` — `ExporterSource`, parallel AP scraping, Prometheus text format parsing.

Remove mentions of: VictoriaMetrics, PromQL, `lookback`, `POLL_INTERVAL` env var, telegraf, TSDB, PrometheusSource.

Add to design decisions:
- Direct AP scraping eliminates telegraf→TSDB pipeline latency — presence detection is independent of metrics collection
- All APs are scraped concurrently using `asyncio.create_task()` — a failing AP doesn't block the others
- `_filter_tracked()` filters to configured MACs in Python — no PromQL needed
- Per-node `url` override allows custom IPs/ports without local DNS

**Step 3: Update README.md**

Update the architecture diagram:

```
OpenWrt APs  -->  openwrt-presence  -->  MQTT  -->  Home Assistant
 (node-exporter-lua)   (state machine)    (discovery)   (device_tracker + sensor)
```

Update the description paragraph — replace TSDB references with direct scraping. Remove the telegraf reference.

Update the "How it works" section:

> Every ~5 seconds, `openwrt-presence` scrapes the `/metrics` endpoint on each AP for current RSSI readings of tracked MAC addresses.

Remove the `source:` config section from the Configuration docs. Replace with exporter_port and per-node url docs:

```markdown
### Exporter port

The `prometheus-node-exporter-lua` HTTP port on APs (default: `9100`). Individual nodes can override the full URL.

```yaml
exporter_port: 9100
```

### Nodes

Each node maps an AP hostname to a room name. The hostname is used to construct the scrape URL (`http://{hostname}:{exporter_port}/metrics`). Use `url` to override for APs without DNS or on non-standard ports:

```yaml
nodes:
  ap-office:
    room: office
  ap-bedroom:
    room: bedroom
    url: http://192.168.1.50:9100/metrics  # custom IP
```
```

Remove the `POLL_INTERVAL` row from the environment variables table (it's in config.yaml now). Remove the `SSL_CERT_FILE` row and the custom CA certificates section (no TSDB connection to secure).

Update the OpenWrt prerequisites section — remove "A metrics scraper (telegraf, prometheus, etc.) should collect from each AP and write to your TSDB." Replace with:

> `openwrt-presence` scrapes each AP directly — no metrics collector or TSDB is needed for presence detection. You can still run telegraf/VictoriaMetrics alongside for dashboards and historical data.

Remove the NTP section (NTP is no longer critical since we don't depend on AP-sourced timestamps).

**Step 4: Run tests one final time**

```bash
.venv/bin/pytest -v
```

Expected: All tests pass.

**Step 5: Verify no stale references**

```bash
grep -r "victoriametrics\|VictoriaMetrics\|PromQL\|promql\|PrometheusSource\|prometheus" src/ --include="*.py"
grep -r "lookback\|tracked_macs\|source\.url\|source\.type" src/ --include="*.py"
```

Expected: No matches.

**Step 6: Commit**

```bash
git add config.yaml.example CLAUDE.md README.md && git commit -m "docs: update for direct AP scraping architecture"
```

---

## Files summary

| Action | File |
|--------|------|
| Modify | `config.py` — remove SourceConfig/lookback/tracked_macs, add exporter_port, per-node url, node_urls property |
| Modify | `__main__.py` — import ExporterSource, build from config.node_urls |
| Create | `sources/exporters.py` — ExporterSource, /metrics parser, parallel scraping |
| Create | `test_source_exporters.py` — parsing + filtering tests |
| Modify | `test_config.py` — remove source tests, add exporter_port + node url tests |
| Modify | `conftest.py` — remove source from fixture |
| Modify | `test_integration.py` — remove source from _make_config |
| Delete | `sources/prometheus.py` |
| Delete | `test_source_prometheus.py` |
| Modify | `config.yaml.example` — remove source/lookback, add exporter_port, show url override |
| Modify | `CLAUDE.md` — new architecture |
| Modify | `README.md` — new architecture |

## Verification

```bash
.venv/bin/pytest -v                              # all tests pass
grep -r "PrometheusSource\|prometheus" src/ --include="*.py"  # no stale refs
grep -r "lookback\|tracked_macs" src/ --include="*.py"        # no stale refs
```
