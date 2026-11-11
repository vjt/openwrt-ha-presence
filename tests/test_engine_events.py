"""Engine semantics for AP push events (docs/plans/2026-11-01-ap-push-events.md)."""

from datetime import UTC, datetime, timedelta

import pytest

from openwrt_presence.domain import (
    ApEvent,
    ApEventKind,
    AwayState,
    HomeState,
    Mac,
    NodeName,
    PersonName,
    StationReading,
)
from openwrt_presence.engine import PresenceEngine

ALICE = "aa:bb:cc:dd:ee:01"
BOB = "aa:bb:cc:dd:ee:03"
ALICE_P = PersonName("alice")


def _t(seconds: float = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)


def _reading(mac: str, ap: str, rssi: int, inactive_ms: int = 0) -> StationReading:
    return StationReading(
        mac=Mac(mac), ap=NodeName(ap), rssi=rssi, inactive_ms=inactive_ms
    )


def _assoc(mac: str, ap: str, rssi: int | None = -60) -> ApEvent:
    return ApEvent(node=NodeName(ap), mac=Mac(mac), kind=ApEventKind.ASSOC, rssi=rssi)


def _disassoc(mac: str, ap: str) -> ApEvent:
    return ApEvent(
        node=NodeName(ap), mac=Mac(mac), kind=ApEventKind.DISASSOC, rssi=None
    )


class TestAssoc:
    def test_assoc_marks_away_person_home_at_once(self, sample_config):
        engine = PresenceEngine(sample_config)
        changes = engine.process_event(_t(0), _assoc(ALICE, "ap-bedroom", -61))
        assert len(changes) == 1
        change = changes[0]
        assert isinstance(change, HomeState)
        assert change.person == "alice"
        assert change.room == "bedroom"
        assert change.node == "ap-bedroom"
        assert change.rssi == -61

    def test_assoc_moves_room(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-living", -50)])
        changes = engine.process_event(_t(1), _assoc(ALICE, "ap-bedroom"))
        assert len(changes) == 1
        assert isinstance(changes[0], HomeState)
        assert changes[0].room == "bedroom"

    def test_assoc_cancels_departure(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-garden", -50)])
        engine.process_snapshot(_t(10), [])  # DEPARTING, deadline t=130
        engine.process_event(_t(20), _assoc(ALICE, "ap-garden"))
        # Past the old deadline and absent again: a NEW departure starts
        # (deadline t=251), the old one must not fire.
        assert engine.process_snapshot(_t(131), []) == []
        assert engine.get_person_state(ALICE_P).home is True
        changes = engine.process_snapshot(_t(252), [])
        assert len(changes) == 1
        assert isinstance(changes[0], AwayState)

    def test_assoc_same_room_emits_nothing(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-bedroom", -50)])
        assert engine.process_event(_t(1), _assoc(ALICE, "ap-bedroom")) == []

    def test_assoc_without_rssi_keeps_last(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-living", -47)])
        changes = engine.process_event(_t(1), _assoc(ALICE, "ap-bedroom", None))
        assert isinstance(changes[0], HomeState)
        assert changes[0].rssi == -47

    def test_untracked_mac_changes_nothing(self, sample_config):
        engine = PresenceEngine(sample_config)
        assert (
            engine.process_event(_t(0), _assoc("ff:ff:ff:ff:ff:ff", "ap-bedroom")) == []
        )
        assert engine.process_snapshot(_t(1), []) == []


class TestDisassoc:
    def test_disassoc_changes_nothing(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-garden", -50)])
        assert engine.process_event(_t(1), _disassoc(ALICE, "ap-garden")) == []
        # Still home and still in garden while the scrape sees it
        changes = engine.process_snapshot(_t(2), [_reading(ALICE, "ap-garden", -50)])
        assert changes == []

    def test_disassoc_does_not_start_departure(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-garden", -50)])
        engine.process_event(_t(1), _disassoc(ALICE, "ap-garden"))
        # Still visible to the scrape at t=300 (> departure_timeout): no AWAY
        assert (
            engine.process_snapshot(_t(300), [_reading(ALICE, "ap-garden", -50)]) == []
        )


class TestHoldWindow:
    def test_stale_scrape_cannot_undo_event(self, sample_config):
        """A scrape in flight during the roam still shows the old AP stronger."""
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-living", -40)])
        engine.process_event(_t(5), _assoc(ALICE, "ap-bedroom", -65))
        changes = engine.process_snapshot(
            _t(6),
            [_reading(ALICE, "ap-living", -40), _reading(ALICE, "ap-bedroom", -66)],
        )
        assert changes == []
        assert engine.get_person_state(ALICE_P).room == "bedroom"

    def test_scrape_on_held_node_refreshes_rssi(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_t(0), _assoc(ALICE, "ap-bedroom", -65))
        engine.process_snapshot(_t(1), [_reading(ALICE, "ap-bedroom", -58)])
        snap = engine.get_person_snapshot(ALICE_P, _t(2))
        assert isinstance(snap, HomeState)
        assert snap.rssi == -58

    def test_absent_from_scrape_during_hold_does_not_depart(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_t(0), _assoc(ALICE, "ap-garden", -65))
        engine.process_snapshot(_t(1), [])  # inside hold: not DEPARTING
        # Absent from t=11 (hold over) → departs; deadline t=11+120
        engine.process_snapshot(_t(11), [])
        assert engine.process_snapshot(_t(125), []) == []
        changes = engine.process_snapshot(_t(132), [])
        assert len(changes) == 1
        assert isinstance(changes[0], AwayState)

    def test_scrape_rule_resumes_after_hold(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_t(0), _assoc(ALICE, "ap-bedroom", -65))
        changes = engine.process_snapshot(
            _t(sample_config.event_hold),
            [
                _reading(ALICE, "ap-living", -40, inactive_ms=100),
                _reading(ALICE, "ap-bedroom", -66, inactive_ms=9_000),
            ],
        )
        assert len(changes) == 1
        assert isinstance(changes[0], HomeState)
        assert changes[0].room == "office"

    def test_hold_only_pins_the_event_device(self, sample_config):
        engine = PresenceEngine(sample_config)
        engine.process_event(_t(0), _assoc(ALICE, "ap-bedroom", -65))
        changes = engine.process_snapshot(_t(1), [_reading(BOB, "ap-living", -50)])
        assert len(changes) == 1
        assert changes[0].person == "bob"


class TestStaleEntryAfterRoam:
    """Measured 2026-11-11: a tracked phone roamed to the office AP (push
    event), the AP it left kept its station entry for ~10 min (hostapd
    inactivity timeout) with the RSSI frozen at the last frame, equal to
    or stronger than the live one. Strongest-RSSI flipped the room every
    10-30 s once the event hold expired."""

    @pytest.mark.parametrize("ghost_rssi", [-58, -55])
    def test_room_stays_where_the_phone_is(self, sample_config, ghost_rssi):
        engine = PresenceEngine(sample_config)
        engine.process_snapshot(_t(0), [_reading(ALICE, "ap-bedroom", -58, 400)])
        changes = engine.process_event(_t(1), _assoc(ALICE, "ap-living", -57))
        assert [c.room for c in changes if isinstance(c, HomeState)] == ["office"]

        live_inactive = [40, 900, 2_300, 150]
        for i, t in enumerate(range(5, 600, 10)):
            snapshot = [
                # Stale entry first: equal RSSI must not win on list order.
                _reading(ALICE, "ap-bedroom", ghost_rssi, t * 1_000 + 400),
                _reading(ALICE, "ap-living", -56 - 2 * (i % 2), live_inactive[i % 4]),
            ]
            assert engine.process_snapshot(_t(t), snapshot) == [], f"flip at t={t}s"
        assert engine.get_person_state(ALICE_P).room == "office"

        # hostapd finally drops the stale entry: nothing changes either.
        assert (
            engine.process_snapshot(_t(605), [_reading(ALICE, "ap-living", -57)]) == []
        )
        assert engine.get_person_state(ALICE_P).room == "office"
