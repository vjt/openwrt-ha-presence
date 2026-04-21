"""Tests for ExporterSource."""

from __future__ import annotations

import io
import json

from aiohttp import web

from openwrt_presence.domain import Mac, NodeName
from openwrt_presence.logging import setup_logging
from openwrt_presence.sources.exporters import ExporterSource

# ── shared fixtures/constants ─────────────────────────────────────────
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


def _make_source(
    node_urls: dict[str, str] | None = None,
    tracked_macs: set[str] | None = None,
) -> ExporterSource:
    raw_urls = node_urls or {
        "ap-living": "http://ap-living:9100/metrics",
        "ap-bedroom": "http://ap-bedroom:9100/metrics",
    }
    raw_macs = tracked_macs or {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"}
    return ExporterSource(
        node_urls={NodeName(k): v for k, v in raw_urls.items()},
        tracked_macs={Mac(m) for m in raw_macs},
    )


def _log_lines(stream: io.StringIO) -> list[dict]:
    text = stream.getvalue().strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


# ── parser tests (pure logic, no network) ─────────────────────────────
class TestMetricsParsing:
    def test_parses_wifi_station_lines(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, NodeName("ap-living"))
        assert len(readings) == 3

    def test_extracts_mac_lowercase(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, NodeName("ap-living"))
        macs = {r.mac for r in readings}
        assert "aa:bb:cc:11:22:01" in macs
        assert "aa:bb:cc:11:22:04" in macs

    def test_extracts_rssi_as_int(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, NodeName("ap-living"))
        by_mac = {r.mac: r for r in readings}
        assert by_mac[Mac("aa:bb:cc:11:22:01")].rssi == -55
        assert by_mac[Mac("aa:bb:cc:11:22:04")].rssi == -42

    def test_ap_name_set_from_argument(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, NodeName("ap-bedroom"))
        assert all(r.ap == "ap-bedroom" for r in readings)

    def test_ignores_non_wifi_metrics(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, NodeName("ap-living"))
        assert all(r.rssi < 0 for r in readings)

    def test_empty_metrics(self):
        readings = ExporterSource._parse_metrics("", NodeName("ap-living"))
        assert readings == []

    def test_no_wifi_metrics(self):
        text = 'node_cpu_seconds_total{cpu="0"} 123.45\n'
        readings = ExporterSource._parse_metrics(text, NodeName("ap-living"))
        assert readings == []

    def test_handles_float_rssi(self):
        text = (
            'wifi_station_signal_dbm{ifname="phy1-ap0",mac="AA:BB:CC:DD:EE:01"} -55.7\n'
        )
        readings = ExporterSource._parse_metrics(text, NodeName("ap-living"))
        assert readings[0].rssi == -55

    def test_filters_to_tracked_macs(self):
        source = _make_source(tracked_macs={"aa:bb:cc:11:22:01"})
        readings = source._filter_tracked(
            ExporterSource._parse_metrics(SAMPLE_METRICS, NodeName("ap-living"))
        )
        assert len(readings) == 1
        assert readings[0].mac == "aa:bb:cc:11:22:01"


# ── HTTP integration tests (aiohttp test server) ──────────────────────
_METRICS_SAMPLE_SHORT = """# HELP wifi_station_signal_dbm
# TYPE wifi_station_signal_dbm gauge
wifi_station_signal_dbm{mac="aa:bb:cc:dd:ee:01"} -55
wifi_station_signal_dbm{mac="aa:bb:cc:dd:ee:02"} -70
"""


async def _metrics_handler(request: web.Request) -> web.Response:
    return web.Response(text=_METRICS_SAMPLE_SHORT, content_type="text/plain")


async def _error_handler(request: web.Request) -> web.Response:
    return web.Response(status=503, text="busy")


class TestExporterSource:
    async def test_scrapes_tracked_macs(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={NodeName("ap-garden"): url},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        macs = [r.mac for r in readings]
        assert macs == ["aa:bb:cc:dd:ee:01"]
        assert readings[0].rssi == -55
        assert readings[0].ap == "ap-garden"

    async def test_ignores_untracked_macs(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={NodeName("ap-garden"): url},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        assert all(r.mac == "aa:bb:cc:dd:ee:01" for r in readings)

    async def test_503_treated_as_unreachable(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _error_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={NodeName("ap-garden"): url},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        assert readings == []
        # Node must be flagged as unhealthy (503 is not a success)
        assert source._node_healthy.get(NodeName("ap-garden")) is False


# ── health tracking (dead-port for legitimate connect errors) ─────────
_DEAD_URL = "http://127.0.0.1:1/metrics"


class TestNodeHealthTracking:
    """Verify that only health transitions produce log output."""

    async def test_first_failure_logs_warning(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={NodeName("ap-living"): _DEAD_URL},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
        )
        try:
            await source.query()
        finally:
            await source.close()

        logs = _log_lines(stream)
        unreachable = [
            entry for entry in logs if entry.get("message") == "node_unreachable"
        ]
        assert len(unreachable) == 1
        assert unreachable[0]["node"] == "ap-living"

    async def test_repeated_failure_is_silent(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={NodeName("ap-living"): _DEAD_URL},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
        )
        try:
            await source.query()  # first — logs
            stream.truncate(0)
            stream.seek(0)
            await source.query()  # repeat — silent
        finally:
            await source.close()

        logs = _log_lines(stream)
        assert not any(entry.get("message") == "node_unreachable" for entry in logs)

    async def test_healthy_node_no_log(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={NodeName("ap-living"): url},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
        )
        try:
            await source.query()
        finally:
            await source.close()

        logs = _log_lines(stream)
        assert not any(
            entry.get("message") in ("node_unreachable", "node_recovered")
            for entry in logs
        )

    # test_recovery_logs_info deferred to Session 2 (requires mid-test server
    # state change; re-add with a mutable handler once Task 2.4 lands).

    async def test_first_scrape_logs_initial_state_healthy(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={NodeName("ap-living"): url},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
        )
        try:
            await source.query()
            stream.truncate(0)
            stream.seek(0)
            await source.query()  # second call must not re-emit
        finally:
            await source.close()

        # Rerun from a fresh stream to capture only the first-scrape log.
        stream2 = io.StringIO()
        setup_logging(file=stream2)
        source2 = ExporterSource(
            node_urls={NodeName("ap-living"): url},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
        )
        try:
            await source2.query()
        finally:
            await source2.close()

        logs = _log_lines(stream2)
        initial = [
            entry for entry in logs if entry.get("message") == "initial_node_state"
        ]
        assert len(initial) == 1
        assert initial[0]["node"] == "ap-living"
        assert initial[0]["healthy"] is True

        # Second call on the original source emitted nothing of this shape.
        logs_after = _log_lines(stream)
        assert not any(
            entry.get("message") == "initial_node_state" for entry in logs_after
        )

    async def test_first_scrape_logs_initial_state_unhealthy(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={NodeName("ap-living"): _DEAD_URL},
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
        )
        try:
            await source.query()
        finally:
            await source.close()

        logs = _log_lines(stream)
        initial = [
            entry for entry in logs if entry.get("message") == "initial_node_state"
        ]
        assert len(initial) == 1
        assert initial[0]["node"] == "ap-living"
        assert initial[0]["healthy"] is False


class TestMalformedMetrics:
    """A malformed metric line from an AP is a real bug, not noise — it must
    fail the scrape and flag the node unhealthy. See M11/M12 + ha-verisure rule."""

    async def test_malformed_line_marks_node_unhealthy(self, aiohttp_server):
        async def _handler(request: web.Request) -> web.Response:
            return web.Response(
                text='wifi_station_signal_dbm{mac="not-a-mac"} not-a-number\n',
                content_type="text/plain",
            )

        app = web.Application()
        app.router.add_get("/metrics", _handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={NodeName("ap-broken"): url},
            tracked_macs=set(),
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        assert readings == []
        logs = _log_lines(stream)
        assert any(
            entry.get("message") == "node_unreachable"
            and entry.get("node") == "ap-broken"
            for entry in logs
        )


class TestAllNodesUnhealthy:
    """Breaker predicate: only trips when every configured node is
    currently failing. A single healthy node keeps the engine running."""

    async def test_false_before_any_query(self):
        source = ExporterSource(
            node_urls={NodeName("down"): _DEAD_URL},
            tracked_macs=set(),
        )
        try:
            assert source.all_nodes_unhealthy is False
        finally:
            await source.close()

    async def test_false_when_mixed(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={
                NodeName("up"): url,
                NodeName("down"): _DEAD_URL,
            },
            tracked_macs={Mac("aa:bb:cc:dd:ee:01")},
            dns_cache_ttl=60,
        )
        try:
            await source.query()
            assert source.all_nodes_unhealthy is False
        finally:
            await source.close()

    async def test_true_when_all_down(self):
        source = ExporterSource(
            node_urls={NodeName("down1"): _DEAD_URL, NodeName("down2"): _DEAD_URL},
            tracked_macs=set(),
            dns_cache_ttl=60,
        )
        try:
            await source.query()
            assert source.all_nodes_unhealthy is True
        finally:
            await source.close()
