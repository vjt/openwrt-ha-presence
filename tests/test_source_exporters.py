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
        assert all(r.rssi < 0 for r in readings)

    def test_empty_metrics(self):
        readings = ExporterSource._parse_metrics("", "pingu")
        assert readings == []

    def test_no_wifi_metrics(self):
        text = "node_cpu_seconds_total{cpu=\"0\"} 123.45\n"
        readings = ExporterSource._parse_metrics(text, "pingu")
        assert readings == []

    def test_handles_float_rssi(self):
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
