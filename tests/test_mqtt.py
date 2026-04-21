from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pytest

from openwrt_presence.domain import (
    AwayState,
    HomeState,
    Mac,
    NodeName,
    PersonName,
    Room,
    StateChange,
)
from openwrt_presence.logging import setup_logging
from openwrt_presence.mqtt import MqttPublisher
from tests.fakes import FakeMqttClient, PublishedMsg


def _home(
    person: str = "alice",
    room: str = "garden",
    mac: str = "aa:bb:cc:dd:ee:01",
    node: str = "ap-garden",
    timestamp: datetime | None = None,
    rssi: int = -55,
) -> HomeState:
    return HomeState(
        person=PersonName(person),
        room=Room(room),
        mac=Mac(mac),
        node=NodeName(node),
        timestamp=timestamp or datetime(2026, 4, 21, tzinfo=UTC),
        rssi=rssi,
    )


def _away(
    person: str = "alice",
    last_mac: str | None = "aa:bb:cc:dd:ee:01",
    last_node: str | None = "ap-garden",
    timestamp: datetime | None = None,
) -> AwayState:
    return AwayState(
        person=PersonName(person),
        timestamp=timestamp or datetime(2026, 4, 21, tzinfo=UTC),
        last_mac=Mac(last_mac) if last_mac is not None else None,
        last_node=NodeName(last_node) if last_node is not None else None,
    )


@pytest.fixture
def publisher(sample_config) -> tuple[MqttPublisher, FakeMqttClient]:
    client = FakeMqttClient()
    return MqttPublisher(sample_config, client), client


class TestLwt:
    def test_publisher_constructor_does_not_set_lwt(self, sample_config):
        """H1: constructor must not mutate the paho client.  LWT wiring
        lives in the caller (_run) to make the 'before connect_async'
        ordering constraint explicit and testable."""
        client = FakeMqttClient()
        MqttPublisher(sample_config, client)
        assert client.lwt is None

    def test_availability_topic_is_public(self, publisher):
        pub, _ = publisher
        assert pub.availability_topic == "openwrt-presence/status"

    def test_lwt_constants_are_strings(self):
        assert MqttPublisher.OFFLINE_PAYLOAD == "offline"
        assert MqttPublisher.ONLINE_PAYLOAD == "online"


class TestDiscovery:
    def test_discovery_published_on_connect(self, publisher):
        pub, client = publisher
        pub.on_connected()
        topics = {m.topic for m in client.published}
        assert "homeassistant/device_tracker/alice_wifi/config" in topics
        assert "homeassistant/device_tracker/bob_wifi/config" in topics
        assert "homeassistant/sensor/alice_room/config" in topics

    def test_discovery_retained_qos1(self, publisher):
        pub, client = publisher
        pub.on_connected()
        for msg in client.published:
            if "config" in msg.topic:
                assert msg.retain is True
                assert msg.qos == 1


class TestStatePublish:
    def test_home_publishes_all_three_topics(self, publisher):
        pub, client = publisher
        pub.publish_state(_home())
        topics = {m.topic for m in client.published}
        assert "openwrt-presence/alice/state" in topics
        assert "openwrt-presence/alice/room" in topics
        assert "openwrt-presence/alice/attributes" in topics

    def test_state_payload_home(self, publisher):
        pub, client = publisher
        pub.publish_state(_home())
        state_msg = next(
            m for m in client.published if m.topic == "openwrt-presence/alice/state"
        )
        assert state_msg.payload == "home"
        assert state_msg.retain is True
        assert state_msg.qos == 1

    def test_state_payload_away(self, publisher):
        pub, client = publisher
        pub.publish_state(_away())
        state_msg = next(
            m for m in client.published if m.topic == "openwrt-presence/alice/state"
        )
        assert state_msg.payload == "not_home"


class TestReconnectReseed:
    def test_on_connected_republishes_cached_state(self, publisher):
        pub, client = publisher
        pub.publish_state(_home())
        client.clear()

        pub.on_connected()
        state_msgs = [
            m for m in client.published if m.topic == "openwrt-presence/alice/state"
        ]
        assert len(state_msgs) == 1
        assert state_msgs[0].payload == "home"
        assert state_msgs[0].retain is True


class TestAvailability:
    def test_publish_online(self, publisher):
        pub, client = publisher
        pub.publish_online()
        assert (
            PublishedMsg(
                topic="openwrt-presence/status",
                payload="online",
                qos=1,
                retain=True,
            )
            in client.published
        )


def _change(person: str, home: bool, room: str | None) -> StateChange:
    if home:
        assert room is not None
        return HomeState(
            person=PersonName(person),
            room=Room(room),
            mac=Mac("aa:bb:cc:dd:ee:01"),
            node=NodeName("ap-office"),
            timestamp=datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC),
            rssi=-50,
        )
    return AwayState(
        person=PersonName(person),
        timestamp=datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC),
        last_mac=Mac("aa:bb:cc:dd:ee:01"),
        last_node=NodeName("ap-garden"),
    )


class TestAudit:
    """publish_state emits state_computed (engine decided) and
    state_delivered (MQTT accepted) around _emit_state.  on_connected
    re-publishes cached state without emitting either audit line — the
    transitions already happened."""

    def test_publish_state_writes_audit_log_lines(self, sample_config):
        stream = io.StringIO()
        setup_logging(file=stream)
        client = FakeMqttClient()
        pub = MqttPublisher(sample_config, client)
        pub.publish_state(_change("alice", True, "office"))

        lines = [line for line in stream.getvalue().splitlines() if line]
        computed = [
            json.loads(line)
            for line in lines
            if json.loads(line).get("message") == "state_computed"
        ]
        delivered = [
            json.loads(line)
            for line in lines
            if json.loads(line).get("message") == "state_delivered"
        ]
        assert len(computed) == 1
        assert len(delivered) == 1
        assert computed[0]["person"] == "alice"
        assert computed[0]["presence"] == "home"
        assert delivered[0]["person"] == "alice"

    def test_on_connected_does_not_relog_cached_state(self, sample_config):
        client = FakeMqttClient()
        pub = MqttPublisher(sample_config, client)
        pub.publish_state(_change("alice", True, "office"))

        stream = io.StringIO()
        setup_logging(file=stream)
        pub.on_connected()

        for line in stream.getvalue().splitlines():
            if not line:
                continue
            data = json.loads(line)
            assert data.get("message") not in (
                "state_computed",
                "state_delivered",
            ), f"on_connected must not emit audit lines: {data}"

    def test_on_connected_idempotent(self, sample_config):
        client = FakeMqttClient()
        pub = MqttPublisher(sample_config, client)
        pub.publish_state(_change("alice", True, "office"))
        client.clear()

        pub.on_connected()
        first_count = len(client.published)
        client.clear()

        pub.on_connected()
        second_count = len(client.published)

        assert first_count == second_count
        assert first_count > 0


class TestPublishFailure:
    """publish() returning non-zero rc means paho couldn't even enqueue
    the message. That is a silent data loss class of bug on the alarm
    path — must surface loud via publish_failed."""

    def test_nonzero_rc_emits_publish_failed(self, sample_config):
        stream = io.StringIO()
        setup_logging(file=stream)
        client = FakeMqttClient()
        client.publish_rc = 2  # MQTT_ERR_QUEUE_SIZE
        pub = MqttPublisher(sample_config, client)

        pub.publish_state(_home())

        failed = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line and json.loads(line).get("message") == "publish_failed"
        ]
        # One publish_failed per failed topic (3 topics: state/room/attributes)
        assert len(failed) == 3
        assert {f["topic"] for f in failed} == {
            "openwrt-presence/alice/state",
            "openwrt-presence/alice/room",
            "openwrt-presence/alice/attributes",
        }
        assert all(f["rc"] == 2 for f in failed)
        assert all(f["person"] == "alice" for f in failed)

    def test_rc_zero_no_publish_failed(self, sample_config):
        stream = io.StringIO()
        setup_logging(file=stream)
        client = FakeMqttClient()
        pub = MqttPublisher(sample_config, client)

        pub.publish_state(_home())

        for line in stream.getvalue().splitlines():
            if not line:
                continue
            assert json.loads(line).get("message") != "publish_failed"


class TestComputedVsDelivered:
    """Audit log invariant: state_computed always fires (engine decided),
    state_delivered only if all 3 publishes returned rc == 0.  A computed
    without a matching delivered is the silent-data-loss tripwire."""

    def test_rc_zero_emits_both(self, sample_config):
        stream = io.StringIO()
        setup_logging(file=stream)
        client = FakeMqttClient()
        pub = MqttPublisher(sample_config, client)
        pub.publish_state(_home())
        msgs = [
            json.loads(line).get("message")
            for line in stream.getvalue().splitlines()
            if line
        ]
        assert "state_computed" in msgs
        assert "state_delivered" in msgs

    def test_rc_nonzero_emits_computed_not_delivered(self, sample_config):
        stream = io.StringIO()
        setup_logging(file=stream)
        client = FakeMqttClient()
        client.publish_rc = 2
        pub = MqttPublisher(sample_config, client)
        pub.publish_state(_home())
        msgs = [
            json.loads(line).get("message")
            for line in stream.getvalue().splitlines()
            if line
        ]
        assert "state_computed" in msgs
        assert "state_delivered" not in msgs
        assert "publish_failed" in msgs
