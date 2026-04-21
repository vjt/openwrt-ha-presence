from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openwrt_presence.config import (
    Config,
    MqttConfig,
    NodeConfig,
    PersonConfig,
)
from openwrt_presence.domain import Mac, NodeName, PersonName, Room


@pytest.fixture
def sample_config() -> Config:
    """Canonical config for integration tests.

    Nodes:
      - ap-garden (exit, room=garden)
      - ap-living (interior, room=office)
      - ap-bedroom (interior, room=bedroom)
    People:
      - alice (2 MACs)
      - bob (1 MAC)
    """
    nodes = {
        NodeName("ap-bedroom"): NodeConfig(room=Room("bedroom")),
        NodeName("ap-living"): NodeConfig(room=Room("office")),
        NodeName("ap-garden"): NodeConfig(room=Room("garden"), exit=True),
    }
    people = {
        PersonName("alice"): PersonConfig(
            macs=[Mac("aa:bb:cc:dd:ee:01"), Mac("aa:bb:cc:dd:ee:02")]
        ),
        PersonName("bob"): PersonConfig(macs=[Mac("aa:bb:cc:dd:ee:03")]),
    }
    mac_lookup = {
        Mac("aa:bb:cc:dd:ee:01"): PersonName("alice"),
        Mac("aa:bb:cc:dd:ee:02"): PersonName("alice"),
        Mac("aa:bb:cc:dd:ee:03"): PersonName("bob"),
    }
    return Config(
        mqtt=MqttConfig(
            host="localhost",
            port=1883,
            topic_prefix="openwrt-presence",
        ),
        nodes=nodes,
        people=people,
        departure_timeout=120,
        away_timeout=600,
        poll_interval=5,
        exporter_port=9100,
        dns_cache_ttl=300,
        _mac_lookup=mac_lookup,
    )


def _ts(minutes: float = 0) -> datetime:
    """Deterministic timestamp helper."""
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC) + timedelta(minutes=minutes)


@pytest.fixture
def ts():
    return _ts


pytest_plugins = ["aiohttp.pytest_plugin"]
