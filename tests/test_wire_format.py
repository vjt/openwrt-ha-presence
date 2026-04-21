# tests/test_wire_format.py
"""Golden-fixture test: publishing these transitions today must produce
bytes identical to tests/wire_format_golden.json (captured in Task 1.1).

This is the contract HA consumes. Every refactor in Sessions 2-4 MUST
keep this test green. When C1's discriminated union lands in Session 3,
the fixture will need regenerating — THAT diff is the migration note
for HA users (documented in CHANGELOG + release notes)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from openwrt_presence.engine import StateChange
from openwrt_presence.mqtt import MqttPublisher
from tests.conftest import sample_config  # noqa: F401 — fixture usage
from tests.fakes import FakeMqttClient, PublishedMsg


_GOLDEN = Path(__file__).parent / "wire_format_golden.json"


def _as_frames(client: FakeMqttClient) -> list[dict]:
    return [
        {"topic": p.topic, "payload": p.payload, "qos": p.qos, "retain": p.retain}
        for p in client.published
    ]


def test_home_transition_wire_format(sample_config):
    fixture = json.loads(_GOLDEN.read_text())
    client = FakeMqttClient()
    pub = MqttPublisher(sample_config, client)
    pub.publish_state(
        StateChange(
            person="alice",
            home=True,
            room="garden",
            mac="aa:bb:cc:dd:ee:01",
            node="ap-garden",
            timestamp=datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc),
            rssi=-55,
        )
    )
    assert _as_frames(client) == fixture["home"]


def test_away_transition_wire_format(sample_config):
    fixture = json.loads(_GOLDEN.read_text())
    client = FakeMqttClient()
    pub = MqttPublisher(sample_config, client)
    pub.publish_state(
        StateChange(
            person="alice",
            home=False,
            room=None,
            mac="aa:bb:cc:dd:ee:01",
            node="ap-garden",
            timestamp=datetime(2026, 4, 21, 13, 0, 0, tzinfo=timezone.utc),
            rssi=None,
        )
    )
    assert _as_frames(client) == fixture["away"]
