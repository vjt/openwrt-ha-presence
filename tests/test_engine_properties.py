"""Property-based fuzz of the presence state machine.

Invariants:
- Never default to HOME. An unknown MAC in a snapshot never
  produces a HomeState for anyone.
- HomeState.room is always a configured room.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from openwrt_presence.domain import HomeState, StationReading
from openwrt_presence.engine import PresenceEngine

TRACKED_MACS = [
    "aa:bb:cc:dd:ee:01",
    "aa:bb:cc:dd:ee:02",
    "aa:bb:cc:dd:ee:03",
]


_mac_strategy = st.sampled_from(
    [
        *TRACKED_MACS,
        "ff:ff:ff:ff:ff:01",  # untracked
        "ff:ff:ff:ff:ff:02",
    ]
)
_ap_strategy = st.sampled_from(["ap-garden", "ap-living", "ap-bedroom"])
_rssi_strategy = st.integers(min_value=-95, max_value=-30)
_reading_strategy = st.builds(
    StationReading,
    mac=_mac_strategy,
    ap=_ap_strategy,
    rssi=_rssi_strategy,
)
_snapshot_strategy = st.lists(_reading_strategy, min_size=0, max_size=8)


def test_never_defaults_to_home_for_untracked_mac(sample_config):
    @given(snapshots=st.lists(_snapshot_strategy, min_size=1, max_size=20))
    def inner(snapshots):
        engine = PresenceEngine(sample_config)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for snap in snapshots:
            changes = engine.process_snapshot(now, snap)
            now += timedelta(seconds=30)
            for c in changes:
                if isinstance(c, HomeState):
                    assert sample_config.mac_to_person(c.mac) is not None

    inner()


def test_state_machine_never_reports_impossible_room(sample_config):
    configured_rooms = {n.room for n in sample_config.nodes.values()}

    @given(snapshots=st.lists(_snapshot_strategy, min_size=1, max_size=20))
    def inner(snapshots):
        engine = PresenceEngine(sample_config)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        for snap in snapshots:
            changes = engine.process_snapshot(now, snap)
            now += timedelta(seconds=30)
            for c in changes:
                if isinstance(c, HomeState):
                    assert c.room in configured_rooms

    inner()
