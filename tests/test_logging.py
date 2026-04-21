"""Tests for the audit-log functions."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

from openwrt_presence.domain import (
    AwayState,
    HomeState,
    Mac,
    NodeName,
    PersonName,
    Room,
    StateChange,
)
from openwrt_presence.logging import (
    log_state_computed,
    log_state_delivered,
    setup_logging,
)


def _change(person: str = "alice", home: bool = True) -> StateChange:
    if home:
        return HomeState(
            person=PersonName(person),
            room=Room("garden"),
            mac=Mac("aa:bb:cc:dd:ee:01"),
            node=NodeName("ap-garden"),
            timestamp=datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC),
            rssi=-55,
        )
    return AwayState(
        person=PersonName(person),
        timestamp=datetime(2026, 4, 21, 12, 0, 0, tzinfo=UTC),
        last_mac=Mac("aa:bb:cc:dd:ee:01"),
        last_node=NodeName("ap-garden"),
    )


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


class TestLogStateComputed:
    def test_message_field(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        log_state_computed(_change())
        assert _lines(stream)[0]["message"] == "state_computed"

    def test_fields_present(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        log_state_computed(_change())
        entry = _lines(stream)[0]
        assert entry["person"] == "alice"
        assert entry["presence"] == "home"
        assert entry["room"] == "garden"
        assert entry["mac"] == "aa:bb:cc:dd:ee:01"
        assert entry["node"] == "ap-garden"
        assert entry["rssi"] == -55
        assert entry["event_ts"] == "2026-04-21T12:00:00+00:00"

    def test_away_presence(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        log_state_computed(_change(home=False))
        entry = _lines(stream)[0]
        assert entry["presence"] == "away"
        assert entry["room"] is None
        assert entry["rssi"] is None


class TestLogStateDelivered:
    def test_message_field(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        log_state_delivered(_change())
        assert _lines(stream)[0]["message"] == "state_delivered"

    def test_shares_schema_with_computed(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        log_state_computed(_change())
        log_state_delivered(_change())
        lines = _lines(stream)
        assert lines[0]["person"] == lines[1]["person"]
        assert lines[0]["presence"] == lines[1]["presence"]
        assert lines[0]["event_ts"] == lines[1]["event_ts"]
