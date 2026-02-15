"""Tests for PrometheusSource."""

from __future__ import annotations

import pytest

from openwrt_presence.sources.prometheus import PrometheusSource


def _make_source(macs: set[str] | None = None) -> PrometheusSource:
    return PrometheusSource(
        url="http://localhost:9090",
        macs=macs or {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"},
    )


class TestQueryBuilding:
    def test_promql_contains_all_macs(self):
        source = _make_source({"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:03"})
        query = source._build_query()
        assert "AA:BB:CC:DD:EE:01" in query
        assert "AA:BB:CC:DD:EE:03" in query
        assert "wifi_station_signal_dbm" in query

    def test_promql_regex_format(self):
        source = _make_source({"AA:BB:CC:DD:EE:01"})
        query = source._build_query()
        assert query == 'wifi_station_signal_dbm{mac=~"AA:BB:CC:DD:EE:01"}'

    def test_url_trailing_slash_stripped(self):
        source = PrometheusSource(url="http://localhost:9090/", macs=set())
        assert source._url == "http://localhost:9090"


class TestResponseParsing:
    def test_parses_normal_response(self):
        data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "mac": "AA:BB:CC:DD:EE:01",
                            "instance": "ap-office",
                            "ifname": "phy1-ap0",
                        },
                        "value": [1700000000, "-45"],
                    },
                    {
                        "metric": {
                            "mac": "AA:BB:CC:DD:EE:02",
                            "instance": "ap-bedroom",
                            "ifname": "phy1-ap0",
                        },
                        "value": [1700000000, "-62"],
                    },
                ],
            },
        }
        readings = PrometheusSource._parse_response(data)
        assert len(readings) == 2
        assert readings[0].mac == "aa:bb:cc:dd:ee:01"
        assert readings[0].ap == "ap-office"
        assert readings[0].rssi == -45
        assert readings[1].mac == "aa:bb:cc:dd:ee:02"
        assert readings[1].ap == "ap-bedroom"
        assert readings[1].rssi == -62

    def test_parses_empty_result(self):
        data = {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }
        readings = PrometheusSource._parse_response(data)
        assert readings == []

    def test_handles_malformed_response(self):
        readings = PrometheusSource._parse_response({"unexpected": "format"})
        assert readings == []

    def test_skips_malformed_entries(self):
        data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"mac": "AA:BB:CC:DD:EE:01"}, "value": []},  # no value
                    {
                        "metric": {
                            "mac": "AA:BB:CC:DD:EE:02",
                            "instance": "ap1",
                        },
                        "value": [1700000000, "-50"],
                    },
                ],
            },
        }
        readings = PrometheusSource._parse_response(data)
        assert len(readings) == 1
        assert readings[0].mac == "aa:bb:cc:dd:ee:02"

    def test_mac_normalized_to_lowercase(self):
        data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "mac": "AA:BB:CC:DD:EE:FF",
                            "instance": "ap1",
                        },
                        "value": [1700000000, "-40"],
                    },
                ],
            },
        }
        readings = PrometheusSource._parse_response(data)
        assert readings[0].mac == "aa:bb:cc:dd:ee:ff"

    def test_rssi_float_string_truncated(self):
        """VictoriaMetrics sometimes returns floats like '-45.0'."""
        data = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {
                        "metric": {
                            "mac": "AA:BB:CC:DD:EE:01",
                            "instance": "ap1",
                        },
                        "value": [1700000000, "-45.7"],
                    },
                ],
            },
        }
        readings = PrometheusSource._parse_response(data)
        assert readings[0].rssi == -45
