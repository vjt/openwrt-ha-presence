"""Tests for ExporterSource."""

from __future__ import annotations

import io
import json

import pytest
from aiohttp import web

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
    return ExporterSource(
        node_urls=node_urls
        or {
            "ap-living": "http://ap-living:9100/metrics",
            "ap-bedroom": "http://ap-bedroom:9100/metrics",
        },
        tracked_macs=tracked_macs or {"aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"},
    )


def _log_lines(stream: io.StringIO) -> list[dict]:
    text = stream.getvalue().strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines()]


# ── parser tests (pure logic, no network) ─────────────────────────────
class TestMetricsParsing:
    def test_parses_wifi_station_lines(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "ap-living")
        assert len(readings) == 3

    def test_extracts_mac_lowercase(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "ap-living")
        macs = {r.mac for r in readings}
        assert "aa:bb:cc:11:22:01" in macs
        assert "aa:bb:cc:11:22:04" in macs

    def test_extracts_rssi_as_int(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "ap-living")
        by_mac = {r.mac: r for r in readings}
        assert by_mac["aa:bb:cc:11:22:01"].rssi == -55
        assert by_mac["aa:bb:cc:11:22:04"].rssi == -42

    def test_ap_name_set_from_argument(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "ap-bedroom")
        assert all(r.ap == "ap-bedroom" for r in readings)

    def test_ignores_non_wifi_metrics(self):
        readings = ExporterSource._parse_metrics(SAMPLE_METRICS, "ap-living")
        assert all(r.rssi < 0 for r in readings)

    def test_empty_metrics(self):
        readings = ExporterSource._parse_metrics("", "ap-living")
        assert readings == []

    def test_no_wifi_metrics(self):
        text = 'node_cpu_seconds_total{cpu="0"} 123.45\n'
        readings = ExporterSource._parse_metrics(text, "ap-living")
        assert readings == []

    def test_handles_float_rssi(self):
        text = (
            'wifi_station_signal_dbm{ifname="phy1-ap0",mac="AA:BB:CC:DD:EE:01"} -55.7\n'
        )
        readings = ExporterSource._parse_metrics(text, "ap-living")
        assert readings[0].rssi == -55

    def test_filters_to_tracked_macs(self):
        source = _make_source(tracked_macs={"aa:bb:cc:11:22:01"})
        readings = source._filter_tracked(
            ExporterSource._parse_metrics(SAMPLE_METRICS, "ap-living")
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
            node_urls={"ap-garden": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
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
            node_urls={"ap-garden": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        assert all(r.mac == "aa:bb:cc:dd:ee:01" for r in readings)

    @pytest.mark.xfail(
        reason="fixed in Task 2.4 — M12 response status check",
        strict=True,
    )
    async def test_503_treated_as_unreachable(self, aiohttp_server, caplog):
        app = web.Application()
        app.router.add_get("/metrics", _error_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        source = ExporterSource(
            node_urls={"ap-garden": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
            dns_cache_ttl=60,
        )
        try:
            readings = await source.query()
        finally:
            await source.close()

        # After Task 2.4 (M12 fix), 503 becomes an exception → empty
        # readings AND node_unreachable log. Asserting both.
        assert readings == []
        assert any(
            r.message == "node_unreachable"
            for r in caplog.records  # type: ignore[attr-defined]
        )


# ── health tracking (dead-port for legitimate connect errors) ─────────
_DEAD_URL = "http://127.0.0.1:1/metrics"


class TestNodeHealthTracking:
    """Verify that only health transitions produce log output."""

    async def test_first_failure_logs_warning(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={"ap-living": _DEAD_URL},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
        )
        try:
            await source.query()
        finally:
            await source.close()

        logs = _log_lines(stream)
        unreachable = [l for l in logs if l.get("message") == "node_unreachable"]
        assert len(unreachable) == 1
        assert unreachable[0]["node"] == "ap-living"

    async def test_repeated_failure_is_silent(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={"ap-living": _DEAD_URL},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
        )
        try:
            await source.query()  # first — logs
            stream.truncate(0)
            stream.seek(0)
            await source.query()  # repeat — silent
        finally:
            await source.close()

        logs = _log_lines(stream)
        assert not any(l.get("message") == "node_unreachable" for l in logs)

    async def test_healthy_node_no_log(self, aiohttp_server):
        app = web.Application()
        app.router.add_get("/metrics", _metrics_handler)
        server = await aiohttp_server(app)
        url = f"http://{server.host}:{server.port}/metrics"

        stream = io.StringIO()
        setup_logging(file=stream)
        source = ExporterSource(
            node_urls={"ap-living": url},
            tracked_macs={"aa:bb:cc:dd:ee:01"},
        )
        try:
            await source.query()
        finally:
            await source.close()

        logs = _log_lines(stream)
        assert not any(
            l.get("message") in ("node_unreachable", "node_recovered") for l in logs
        )

    # test_recovery_logs_info deferred to Session 2 (requires mid-test server
    # state change; re-add with a mutable handler once Task 2.4 lands).
