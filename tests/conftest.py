from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openwrt_presence.config import (
    Config,
    MqttConfig,
    NodeConfig,
    PersonConfig,
)


@pytest.fixture
def sample_config() -> Config:
    """Canonical config for integration tests.

    Nodes:
      - mowgli (exit, room=garden)
      - pingu (interior, room=office)
      - albert (interior, room=bedroom)
    People:
      - alice (2 MACs)
      - bob (1 MAC)
    """
    nodes = {
        "albert": NodeConfig(room="bedroom"),
        "pingu": NodeConfig(room="office"),
        "mowgli": NodeConfig(room="garden", exit=True),
    }
    people = {
        "alice": PersonConfig(macs=["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]),
        "bob": PersonConfig(macs=["aa:bb:cc:dd:ee:03"]),
    }
    mac_lookup = {
        "aa:bb:cc:dd:ee:01": "alice",
        "aa:bb:cc:dd:ee:02": "alice",
        "aa:bb:cc:dd:ee:03": "bob",
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
    return datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minutes
    )


@pytest.fixture
def ts():
    return _ts


pytest_plugins = ["aiohttp.pytest_plugin"]
