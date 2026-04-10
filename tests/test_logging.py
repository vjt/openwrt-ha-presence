import json
import io
from datetime import datetime, timezone

from openwrt_presence.logging import setup_logging, log_state_change
from openwrt_presence.engine import StateChange

_TS = datetime(2026, 2, 12, 10, 0, 0, tzinfo=timezone.utc)


class TestStructuredLogging:
    def test_logs_home_event_as_json(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        change = StateChange(person="alice", home=True, room="kitchen", mac="aa:bb:cc:dd:ee:01", node="ap-kitchen", timestamp=_TS, rssi=-42)
        log_state_change(change)

        line = stream.getvalue().strip()
        data = json.loads(line)
        assert data["person"] == "alice"
        assert data["presence"] == "home"
        assert data["room"] == "kitchen"
        assert data["rssi"] == -42
        assert "ts" in data
        assert data["event_ts"] == _TS.isoformat()

    def test_logs_away_event(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden", timestamp=_TS)
        log_state_change(change)

        data = json.loads(stream.getvalue().strip())
        assert data["presence"] == "away"
        assert data["room"] is None
        assert data["node"] == "ap-garden"
        assert data["event_ts"] == _TS.isoformat()

    def test_includes_timestamp(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        change = StateChange(person="bob", home=True, room="office", mac="aa:bb:cc:dd:ee:03", node="ap-office", timestamp=_TS)
        log_state_change(change)

        data = json.loads(stream.getvalue().strip())
        assert "ts" in data
        datetime.fromisoformat(data["ts"])

    def test_message_field_is_state_change(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        change = StateChange(person="bob", home=True, room="office", mac="aa:bb:cc:dd:ee:03", node="ap-office", timestamp=_TS)
        log_state_change(change)

        data = json.loads(stream.getvalue().strip())
        assert data["message"] == "state_change"

    def test_level_is_uppercase(self):
        stream = io.StringIO()
        setup_logging(file=stream)
        change = StateChange(person="bob", home=True, room="office", mac="aa:bb:cc:dd:ee:03", node="ap-office", timestamp=_TS)
        log_state_change(change)

        data = json.loads(stream.getvalue().strip())
        assert data["level"] == "INFO"
