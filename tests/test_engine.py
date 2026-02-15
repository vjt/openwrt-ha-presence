from datetime import datetime, timezone, timedelta

from openwrt_presence.engine import (
    PresenceEngine,
    PersonState,
    StationReading,
)


def _reading(mac: str, ap: str, rssi: int) -> StationReading:
    return StationReading(mac=mac, ap=ap, rssi=rssi)


def _ts(minutes: float = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)


class TestSnapshotBasicTransitions:
    def test_visible_device_marks_person_home(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is True
        assert changes[0].room == "office"

    def test_unknown_mac_ignored(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_snapshot(_ts(0), [
            _reading("ff:ff:ff:ff:ff:ff", "pingu", -45),
        ])
        assert changes == []

    def test_unknown_ap_uses_none_room(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "unknown-ap", -45),
        ])
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room is None

    def test_disappear_starts_departing(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # Empty snapshot — device disappeared
        changes = engine.process_snapshot(_ts(1), [])
        # Still home (DEPARTING), no state change yet
        assert changes == []

    def test_rssi_included_in_state_change(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -42),
        ])
        assert changes[0].rssi == -42


class TestDeparture:
    def test_timeout_marks_away(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # Device disappears
        engine.process_snapshot(_ts(1), [])
        # Tick past departure_timeout (120s = 2 min)
        changes = engine.process_snapshot(_ts(4), [])
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False
        assert changes[0].room is None

    def test_reappearance_cancels_departure(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # Disappear
        engine.process_snapshot(_ts(1), [])
        # Reappear before timeout
        changes = engine.process_snapshot(_ts(1.5), [
            _reading("aa:bb:cc:dd:ee:01", "albert", -50),
        ])
        # Room changed from office to bedroom
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room == "bedroom"
        # Tick past original deadline — should NOT trigger away
        changes = engine.tick(_ts(10))
        assert changes == []

    def test_multi_device_all_must_disappear(self, sample_config):
        engine = PresenceEngine(sample_config)
        # Both of alice's devices visible
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
            _reading("aa:bb:cc:dd:ee:02", "albert", -50),
        ])
        # One disappears, other stays
        engine.process_snapshot(_ts(1), [
            _reading("aa:bb:cc:dd:ee:02", "albert", -50),
        ])
        # Tick well past timeout — alice still home
        changes = engine.process_snapshot(_ts(10), [
            _reading("aa:bb:cc:dd:ee:02", "albert", -50),
        ])
        assert changes == []
        state = engine.get_person_state("alice")
        assert state.home is True


class TestRoomSelection:
    def test_best_rssi_wins(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -60),
            _reading("aa:bb:cc:dd:ee:01", "albert", -45),  # stronger
        ])
        assert len(changes) == 1
        assert changes[0].room == "bedroom"  # albert's room

    def test_room_changes_with_rssi(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # RSSI now stronger from albert
        changes = engine.process_snapshot(_ts(1), [
            _reading("aa:bb:cc:dd:ee:01", "albert", -40),
        ])
        assert len(changes) == 1
        assert changes[0].room == "bedroom"

    def test_departing_preserves_room(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # Device disappears — DEPARTING, but room preserved
        changes = engine.process_snapshot(_ts(1), [])
        # No state change (still home, room preserved)
        assert changes == []
        state = engine.get_person_state("alice")
        assert state.home is True
        assert state.room == "office"


class TestNoSpuriousChanges:
    def test_stable_snapshot_no_repeat(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        assert len(changes) == 1
        # Same snapshot again — no change
        changes = engine.process_snapshot(_ts(1), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        assert changes == []

    def test_away_then_return(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        # Disappear and timeout
        engine.process_snapshot(_ts(1), [])
        engine.process_snapshot(_ts(5), [])  # past 120s timeout
        # Return
        changes = engine.process_snapshot(_ts(10), [
            _reading("aa:bb:cc:dd:ee:01", "albert", -50),
        ])
        assert len(changes) == 1
        assert changes[0].home is True
        assert changes[0].room == "bedroom"


class TestTick:
    def test_tick_expires_departure(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        engine.process_snapshot(_ts(1), [])  # DEPARTING
        changes = engine.tick(_ts(5))
        assert len(changes) == 1
        assert changes[0].home is False

    def test_tick_does_not_repeat_away(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_ts(0), [
            _reading("aa:bb:cc:dd:ee:01", "pingu", -45),
        ])
        engine.process_snapshot(_ts(1), [])  # DEPARTING
        engine.tick(_ts(5))  # → AWAY
        changes = engine.tick(_ts(10))
        assert changes == []
