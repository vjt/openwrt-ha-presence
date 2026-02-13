"""Integration test: replay realistic event sequences through parser + engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from openwrt_presence.config import Config
from openwrt_presence.engine import PresenceEngine, PersonState
from openwrt_presence.parser import PresenceEvent, parse_hostapd_message


def _ts(minutes: float = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def _parse(msg: str, node: str, ts: datetime) -> PresenceEvent | None:
    event = parse_hostapd_message(msg, node)
    if event is not None:
        # Override timestamp since parse_hostapd_message uses datetime.now()
        return PresenceEvent(event=event.event, mac=event.mac, node=event.node, timestamp=ts)
    return None


def _make_config() -> Config:
    return Config.from_dict({
        "source": {"type": "victorialogs", "url": "http://localhost:9428"},
        "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
        "nodes": {
            "mowgli": {"room": "garden", "type": "exit", "timeout": 120},
            "pingu": {"room": "office", "type": "interior"},
            "albert": {"room": "bedroom", "type": "interior"},
            "golem": {"room": "livingroom", "type": "interior"},
            "gordon": {"room": "kitchen", "type": "interior"},
        },
        "away_timeout": 64800,
        "people": {
            "alice": {
                "macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]
            },
            "bob": {
                "macs": ["aa:bb:cc:dd:ee:03"]
            },
        },
    })


class TestArrivalAndRoaming:
    """Alice arrives home, roams between rooms."""

    def test_full_arrival_roaming_departure_cycle(self):
        config = _make_config()
        engine = PresenceEngine(config)
        all_changes = []

        # Alice arrives via garden (exit node)
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open", "mowgli", _ts(0))
        assert ev is not None
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is True
        assert changes[0].room == "garden"

        # Alice roams to office (disconnect from garden, connect to office)
        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01", "mowgli", _ts(1))
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        # Disconnect from exit starts timer but person still home
        assert changes == []

        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=ft", "pingu", _ts(1))
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        assert len(changes) == 1
        assert changes[0].room == "office"

        # Alice moves to kitchen via 802.11r fast transition
        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01", "pingu", _ts(30))
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        assert changes == []  # interior disconnect, no change

        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=ft", "gordon", _ts(30))
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        assert len(changes) == 1
        assert changes[0].room == "kitchen"

        # Alice's phone goes to doze — disconnects from kitchen
        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01", "gordon", _ts(60))
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        assert changes == []  # interior, no change

        # 2 hours pass with phone in doze. Tick should NOT mark away (global timeout is 18h)
        changes = engine.tick(_ts(180))
        all_changes.extend(changes)
        assert changes == []

        # Phone wakes up, reconnects to bedroom
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open", "albert", _ts(180))
        changes = engine.process_event(ev)
        all_changes.extend(changes)
        assert len(changes) == 1
        assert changes[0].room == "bedroom"
        assert changes[0].home is True

        # Verify final state
        state = engine.get_person_state("alice")
        assert state.home is True
        assert state.room == "bedroom"

    def test_departure_via_exit_node(self):
        config = _make_config()
        engine = PresenceEngine(config)

        # Alice is home in office
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open", "pingu", _ts(0))
        engine.process_event(ev)

        # Alice roams to garden (exit node) and leaves
        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01", "pingu", _ts(10))
        engine.process_event(ev)
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=ft", "mowgli", _ts(10))
        engine.process_event(ev)

        # Alice walks out the garden gate, disconnects from garden AP
        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01", "mowgli", _ts(15))
        changes = engine.process_event(ev)
        assert changes == []  # timer started, not yet away

        # Tick before timeout — still home
        changes = engine.tick(_ts(16))
        assert changes == []

        # Tick after timeout (120 seconds = 2 minutes)
        changes = engine.tick(_ts(18))
        assert len(changes) == 1
        assert changes[0].person == "alice"
        assert changes[0].home is False
        assert changes[0].room is None

        # Subsequent ticks should NOT repeat away
        changes = engine.tick(_ts(60))
        assert changes == []


class TestMultiDevice:
    """Alice has two devices (phone + work laptop)."""

    def test_one_device_dozes_other_stays_connected(self):
        config = _make_config()
        engine = PresenceEngine(config)

        # Phone connects to office
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open", "pingu", _ts(0))
        engine.process_event(ev)

        # Laptop connects to office
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:02 auth_alg=open", "pingu", _ts(1))
        changes = engine.process_event(ev)
        # No change since already home in office
        assert changes == []

        # Phone dozes
        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01", "pingu", _ts(30))
        changes = engine.process_event(ev)
        assert changes == []  # laptop still connected

        state = engine.get_person_state("alice")
        assert state.home is True
        assert state.room == "office"


class TestUnknownMacs:
    """Unknown MAC addresses should be silently ignored throughout."""

    def test_unknown_macs_ignored_in_full_flow(self):
        config = _make_config()
        engine = PresenceEngine(config)

        # Unknown device connects and disconnects — no changes
        ev = _parse("phy1-ap0: AP-STA-CONNECTED ff:ff:ff:ff:ff:ff auth_alg=open", "pingu", _ts(0))
        assert ev is not None
        changes = engine.process_event(ev)
        assert changes == []

        ev = _parse("phy1-ap0: AP-STA-DISCONNECTED ff:ff:ff:ff:ff:ff", "pingu", _ts(5))
        changes = engine.process_event(ev)
        assert changes == []

        # Known device still works normally
        ev = _parse("phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open", "pingu", _ts(10))
        changes = engine.process_event(ev)
        assert len(changes) == 1
        assert changes[0].person == "alice"


class TestIrrelevantMessages:
    """Non-hostapd messages should be silently dropped by parser."""

    def test_irrelevant_messages_dropped(self):
        config = _make_config()
        engine = PresenceEngine(config)

        irrelevant = [
            "phy1-ap0: STA aa:bb:cc:dd:ee:01 WPA: pairwise key handshake completed (RSN)",
            "phy1-ap0: STA aa:bb:cc:dd:ee:01 IEEE 802.11: authenticated",
            "nl80211: kernel reports: key addition failed",
        ]

        for msg in irrelevant:
            event = parse_hostapd_message(msg, "pingu")
            assert event is None

        # No state changes should have occurred
        state = engine.get_person_state("alice")
        assert state.home is False
