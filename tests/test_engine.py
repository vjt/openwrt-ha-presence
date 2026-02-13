from datetime import datetime, timezone, timedelta
import pytest
from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine, PersonState, StateChange
from openwrt_presence.parser import PresenceEvent


def _event(event_type, mac, node, ts=None):
    return PresenceEvent(
        event=event_type, mac=mac, node=node,
        timestamp=ts or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

def _ts(minutes=0):
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)


class TestBasicTransitions:
    def test_connect_marks_person_home(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is True
        assert changes[0].room == "office"

    def test_unknown_mac_ignored(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_event("connect", "ff:ff:ff:ff:ff:ff", "ap-office", _ts(0)))
        assert changes == []

    def test_unknown_node_treated_as_interior(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "unknown-ap", _ts(0)))
        assert len(changes) == 1
        assert changes[0].home is True

    def test_disconnect_from_interior_keeps_home(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        changes = engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        assert changes == []

    def test_room_change_on_roaming(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-bedroom", _ts(1)))
        assert len(changes) == 1
        assert changes[0].room == "bedroom"
        assert changes[0].home is True


class TestExitNodeDeparture:
    def test_disconnect_from_exit_no_immediate_change(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        changes = engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        assert changes == []

    def test_exit_timeout_marks_away(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        changes = engine.tick(_ts(4))  # 4 min > 2 min timeout
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False
        assert changes[0].room is None

    def test_reconnect_before_exit_timeout_cancels(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(2)))
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room == "office"
        # Tick past original timeout — should NOT trigger away
        changes = engine.tick(_ts(10))
        assert changes == []


class TestGlobalTimeout:
    def test_global_timeout_marks_away_from_interior(self):
        cfg = Config.from_dict({
            "source": {"type": "victorialogs", "url": "http://localhost:9428"},
            "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
            "nodes": {"ap-office": {"room": "office", "type": "interior"}},
            "away_timeout": 600,
            "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
        })
        engine = PresenceEngine(cfg)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        changes = engine.tick(_ts(15))  # 15 min > 10 min timeout
        assert len(changes) == 1
        assert changes[0].home is False


class TestMultiDevicePerson:
    def test_person_home_if_any_device_connected(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-bedroom", _ts(1)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(2)))
        state = engine.get_person_state("alice")
        assert state.home is True

    def test_person_away_only_when_all_devices_away(self):
        cfg = Config.from_dict({
            "source": {"type": "victorialogs", "url": "http://localhost:9428"},
            "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
            "nodes": {"ap-garden": {"room": "garden", "type": "exit", "timeout": 60}},
            "away_timeout": 64800,
            "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]}},
        })
        engine = PresenceEngine(cfg)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:02", "ap-garden", _ts(1)))
        changes = engine.tick(_ts(5))
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False

    def test_room_follows_most_recent_device(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-bedroom", _ts(5)))
        state = engine.get_person_state("alice")
        assert state.room == "bedroom"

    def test_room_follows_processing_order_not_timestamp(self, sample_config):
        """Clock skew: second connect has an earlier timestamp but should still win."""
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(10)))
        # Second device connects later but has a skewed timestamp in the past
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:02", "ap-bedroom", _ts(0)))
        state = engine.get_person_state("alice")
        assert state.room == "bedroom"


class TestNoSpuriousChanges:
    def test_reconnect_to_same_node_no_change(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(0)))
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-office", _ts(1)))
        assert changes == []

    def test_away_person_coming_home(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        engine.tick(_ts(5))  # now away
        changes = engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(10)))
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room == "garden"

    def test_tick_does_not_repeat_away(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_event("connect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(0)))
        engine.process_event(_event("disconnect", "aa:bb:cc:dd:ee:01", "ap-garden", _ts(1)))
        changes1 = engine.tick(_ts(5))
        assert len(changes1) == 1
        # Tick again — should NOT emit another away
        changes2 = engine.tick(_ts(10))
        assert changes2 == []
