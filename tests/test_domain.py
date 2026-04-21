from datetime import UTC, datetime

from openwrt_presence.domain import (
    AwayState,
    HomeState,
    Mac,
    NodeName,
    PersonName,
    Room,
)


def test_newtypes_are_str_at_runtime():
    m = Mac("aa:bb:cc:dd:ee:01")
    assert isinstance(m, str)
    assert m == "aa:bb:cc:dd:ee:01"


def test_newtypes_distinct_at_typecheck():
    # Pyright should flag this (we rely on CI gating); runtime is str.
    p = PersonName("alice")
    n = NodeName("ap-garden")
    assert p != n


def test_home_state_requires_room_and_mac():
    # Pyright enforces. At runtime, constructor takes all fields.
    h = HomeState(
        person=PersonName("alice"),
        room=Room("garden"),
        mac=Mac("aa:bb:cc:dd:ee:01"),
        node=NodeName("ap-garden"),
        timestamp=datetime(2026, 4, 21, tzinfo=UTC),
        rssi=-55,
    )
    assert h.home is True


def test_away_state_allows_no_last_seen():
    a = AwayState(
        person=PersonName("bob"),
        timestamp=datetime(2026, 4, 21, tzinfo=UTC),
    )
    assert a.home is False
    assert a.last_mac is None
