import json
import logging
import io
from datetime import datetime, timezone

from openwrt_presence.logging import setup_logging, log_state_change
from openwrt_presence.engine import StateChange

_TS = datetime(2026, 2, 12, 10, 0, 0, tzinfo=timezone.utc)


class TestStructuredLogging:
    def test_logs_home_event_as_json(self):
        handler = logging.StreamHandler(stream := io.StringIO())
        setup_logging(handler=handler)
        change = StateChange(person="alice", home=True, room="kitchen", mac="aa:bb:cc:dd:ee:01", node="ap-kitchen", timestamp=_TS)
        log_state_change(change)

        line = stream.getvalue().strip()
        data = json.loads(line)
        assert data["person"] == "alice"
        assert data["event"] == "home"
        assert data["room"] == "kitchen"
        assert "ts" in data
        assert data["event_ts"] == _TS.isoformat()

    def test_logs_away_event(self):
        handler = logging.StreamHandler(stream := io.StringIO())
        setup_logging(handler=handler)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden", timestamp=_TS)
        log_state_change(change)

        data = json.loads(stream.getvalue().strip())
        assert data["event"] == "away"
        assert data["room"] is None
        assert data["node"] == "ap-garden"
        assert data["event_ts"] == _TS.isoformat()

    def test_includes_timestamp(self):
        handler = logging.StreamHandler(stream := io.StringIO())
        setup_logging(handler=handler)
        change = StateChange(person="bob", home=True, room="office", mac="aa:bb:cc:dd:ee:03", node="ap-office", timestamp=_TS)
        log_state_change(change)

        data = json.loads(stream.getvalue().strip())
        assert "ts" in data
        # Should be ISO format
        from datetime import datetime
        datetime.fromisoformat(data["ts"])  # will raise if invalid
