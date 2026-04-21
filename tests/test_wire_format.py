# tests/test_wire_format.py
"""Golden-fixture test: publishing these transitions today must produce
bytes identical to tests/wire_format_golden.json (captured in Task 1.1).

This is the contract HA consumes. Every refactor in Sessions 2-4 MUST
keep this test green. When C1's discriminated union lands in Session 3,
the fixture will need regenerating — THAT diff is the migration note
for HA users (documented in CHANGELOG + release notes)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from openwrt_presence.domain import (
    AwayState,
    HomeState,
    Mac,
    NodeName,
    PersonName,
    Room,
)
from openwrt_presence.mqtt import MqttPublisher
from tests.fakes import FakeMqttClient

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
        HomeState(
            person=PersonName("alice"),
            room=Room("garden"),
            mac=Mac("aa:bb:cc:dd:ee:01"),
            node=NodeName("ap-garden"),
            timestamp=datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC),
            rssi=-55,
        )
    )
    assert _as_frames(client) == fixture["home"]


def test_away_transition_wire_format(sample_config):
    fixture = json.loads(_GOLDEN.read_text())
    client = FakeMqttClient()
    pub = MqttPublisher(sample_config, client)
    pub.publish_state(
        AwayState(
            person=PersonName("alice"),
            timestamp=datetime(2026, 4, 21, 13, 0, 0, tzinfo=UTC),
            last_mac=Mac("aa:bb:cc:dd:ee:01"),
            last_node=NodeName("ap-garden"),
        )
    )
    assert _as_frames(client) == fixture["away"]


def test_never_seen_transition_wire_format(sample_config):
    """Person tracked in config but has never been seen on any AP —
    startup seed emits AwayState with no last_mac/last_node.  Payload
    drops mac/node/rssi keys (migration note in CHANGELOG)."""
    fixture = json.loads(_GOLDEN.read_text())
    client = FakeMqttClient()
    pub = MqttPublisher(sample_config, client)
    pub.publish_state(
        AwayState(
            person=PersonName("bob"),
            timestamp=datetime(2026, 4, 21, 14, 0, 0, tzinfo=UTC),
        )
    )
    assert _as_frames(client) == fixture["never_seen"]
