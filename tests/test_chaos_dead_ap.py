"""End-to-end: an AP that goes dark during an active session produces
a departure only after the configured timeout — and only on the
exit-node short timeout if the last representative was on an exit AP."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openwrt_presence.engine import PresenceEngine
from openwrt_presence.domain import AwayState, StationReading


def test_exit_node_ap_death_produces_away_after_departure_timeout(sample_config):
    engine = PresenceEngine(sample_config)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t = start
    engine.process_snapshot(
        t,
        [
            StationReading(mac="aa:bb:cc:dd:ee:01", ap="ap-garden", rssi=-55),
        ],
    )
    for _ in range(30):
        t += timedelta(seconds=10)
        changes = engine.process_snapshot(t, [])
        if any(isinstance(c, AwayState) and c.person == "alice" for c in changes):
            break
    elapsed = (t - start).total_seconds()
    assert 110 <= elapsed <= 150, (
        f"alice departed after {elapsed}s — expected ~120s (departure_timeout)"
    )


def test_interior_node_ap_death_uses_long_timeout(sample_config):
    engine = PresenceEngine(sample_config)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t = start
    engine.process_snapshot(
        t,
        [
            StationReading(mac="aa:bb:cc:dd:ee:03", ap="ap-living", rssi=-60),
        ],
    )
    for _ in range(55):
        t += timedelta(seconds=10)
        changes = engine.process_snapshot(t, [])
        elapsed = (t - start).total_seconds()
        assert not any(
            isinstance(c, AwayState) and c.person == "bob" for c in changes
        ), f"bob departed after {elapsed}s — should use away_timeout (600s)"
