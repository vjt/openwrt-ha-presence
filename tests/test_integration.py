"""Integration test: replay realistic snapshot sequences through engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine, PersonState, StationReading


def _ts(minutes: float = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _reading(mac: str, ap: str, rssi: int) -> StationReading:
    return StationReading(mac=mac, ap=ap, rssi=rssi)


def _make_config() -> Config:
    return Config.from_dict({
        "source": {"type": "prometheus", "url": "http://localhost:9090"},
        "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
        "nodes": {
            "mowgli": {"room": "garden"},
            "pingu": {"room": "office"},
            "albert": {"room": "bedroom"},
            "golem": {"room": "livingroom"},
            "gordon": {"room": "kitchen"},
        },
        "departure_timeout": 120,
        "people": {
            "alice": {
                "macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"],
            },
            "bob": {
                "macs": ["aa:bb:cc:dd:ee:03"],
            },
        },
    })


class TestArrivalAndRoaming:
    """Alice arrives, roams between rooms based on RSSI, then departs."""

    def test_full_arrival_roaming_departure_cycle(self):
        config = _make_config()
        engine = PresenceEngine(config)

        # Alice arrives — phone visible on garden AP
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "mowgli", -55),
        ])
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is True
        assert changes[0].room == "garden"

        # Alice walks inside — phone visible on office and garden, office stronger
        changes = engine.process_snapshot(_ts(0.5), [
            _reading("aa:bb:cc:dd:ee:01", "mowgli", -70),
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        assert len(changes) == 1
        assert changes[0].room == "office"

        # Alice moves to kitchen — only kitchen AP sees her
        changes = engine.process_snapshot(_ts(1), [
            _reading("aa:bb:cc:dd:ee:01", "gordon", -42),
        ])
        assert len(changes) == 1
        assert changes[0].room == "kitchen"

        # Stable in kitchen for several polls — no changes
        changes = engine.process_snapshot(_ts(1.5), [
            _reading("aa:bb:cc:dd:ee:01", "gordon", -44),
        ])
        assert changes == []

        # Alice goes to garden gate, walks away — last reading from garden
        changes = engine.process_snapshot(_ts(10), [
            _reading("aa:bb:cc:dd:ee:01", "mowgli", -65),
        ])
        assert len(changes) == 1
        assert changes[0].room == "garden"

        # Phone goes out of range — empty snapshot
        changes = engine.process_snapshot(_ts(10.5), [])
        # DEPARTING — still home, room preserved
        assert changes == []
        state = engine.get_person_state("alice")
        assert state.home is True
        assert state.room == "garden"

        # Before timeout (120s) — still departing
        changes = engine.process_snapshot(_ts(11), [])
        assert changes == []

        # Past timeout (10.5 + 2 = 12.5 min)
        changes = engine.process_snapshot(_ts(13), [])
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False
        assert changes[0].room is None

        # Subsequent empty snapshots — no repeat
        changes = engine.process_snapshot(_ts(20), [])
        assert changes == []

    def test_rssi_based_room_tracking(self):
        """Room follows the AP with strongest RSSI, not the first seen."""
        config = _make_config()
        engine = PresenceEngine(config)

        # Phone visible on three APs simultaneously
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -60),
            _reading("aa:bb:cc:dd:ee:01", "albert", -50),
            _reading("aa:bb:cc:dd:ee:01", "gordon", -70),
        ])
        assert len(changes) == 1
        assert changes[0].room == "bedroom"  # albert has strongest RSSI

        # Next poll: office RSSI improves
        changes = engine.process_snapshot(_ts(0.5), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -40),
            _reading("aa:bb:cc:dd:ee:01", "albert", -55),
        ])
        assert len(changes) == 1
        assert changes[0].room == "office"


class TestMultiDevice:
    """Alice has two devices (phone + work laptop)."""

    def test_one_device_dozes_other_stays_visible(self):
        config = _make_config()
        engine = PresenceEngine(config)

        # Both devices visible in office
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
            _reading("aa:bb:cc:dd:ee:02", "pingu", -50),
        ])
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is True

        # Phone dozes (disappears), laptop still visible
        changes = engine.process_snapshot(_ts(1), [
            _reading("aa:bb:cc:dd:ee:02", "pingu", -50),
        ])
        assert changes == []
        state = engine.get_person_state("alice")
        assert state.home is True
        assert state.room == "office"

        # Even past timeout — laptop keeps alice home
        changes = engine.process_snapshot(_ts(10), [
            _reading("aa:bb:cc:dd:ee:02", "pingu", -48),
        ])
        assert changes == []
        state = engine.get_person_state("alice")
        assert state.home is True

    def test_devices_in_different_rooms(self):
        """Room follows strongest RSSI across all devices."""
        config = _make_config()
        engine = PresenceEngine(config)

        # Phone in bedroom, laptop in office
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "albert", -60),
            _reading("aa:bb:cc:dd:ee:02", "pingu", -45),  # stronger
        ])
        assert len(changes) == 1
        assert changes[0].room == "office"  # laptop's room, stronger RSSI

    def test_both_devices_disappear(self):
        """Alice only marked away when ALL devices timeout."""
        config = _make_config()
        engine = PresenceEngine(config)

        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
            _reading("aa:bb:cc:dd:ee:02", "pingu", -50),
        ])
        # Both disappear
        engine.process_snapshot(_ts(1), [])
        # Past timeout
        changes = engine.process_snapshot(_ts(5), [])
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False


class TestMultiplePeople:
    """Alice and Bob tracked independently."""

    def test_simultaneous_presence(self):
        config = _make_config()
        engine = PresenceEngine(config)

        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
            _reading("aa:bb:cc:dd:ee:03", "albert", -50),
        ])
        assert len(changes) == 2
        persons = {c.person for c in changes}
        assert persons == {"alice", "bob"}

        # Bob leaves, Alice stays
        changes = engine.process_snapshot(_ts(1), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # No immediate change (bob is DEPARTING)
        assert changes == []

        # Past timeout — only bob goes away
        changes = engine.process_snapshot(_ts(5), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        assert len(changes) == 1
        assert changes[0].person == "bob"
        assert changes[0].home is False

        state = engine.get_person_state("alice")
        assert state.home is True
