import io
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

from openwrt_presence.engine import StateChange
from openwrt_presence.logging import setup_logging
from openwrt_presence.mqtt import MqttPublisher

_TS = datetime(2026, 2, 12, 10, 0, 0, tzinfo=UTC)


class TestMqttDiscovery:
    def test_publishes_device_tracker_discovery(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()

        calls = mock_client.publish.call_args_list
        tracker_calls = [c for c in calls if "device_tracker/alice_wifi/config" in str(c)]
        assert len(tracker_calls) == 1
        payload = json.loads(tracker_calls[0][0][1])  # positional arg 1
        assert payload["source_type"] == "router"
        assert payload["state_topic"] == "openwrt-presence/alice/state"
        assert payload["json_attributes_topic"] == "openwrt-presence/alice/attributes"
        assert payload["availability_topic"] == "openwrt-presence/status"

    def test_publishes_room_sensor_discovery(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()

        calls = mock_client.publish.call_args_list
        sensor_calls = [c for c in calls if "sensor/alice_room/config" in str(c)]
        assert len(sensor_calls) == 1
        payload = json.loads(sensor_calls[0][0][1])
        assert payload["state_topic"] == "openwrt-presence/alice/room"

    def test_publishes_discovery_for_all_people(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()

        calls = mock_client.publish.call_args_list
        # 2 entities per person (device_tracker + sensor), 2 people = 4 discovery messages
        discovery_calls = [c for c in calls if "homeassistant/" in str(c)]
        assert len(discovery_calls) == 4

    def test_discovery_payloads_are_retained(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()

        for c in mock_client.publish.call_args_list:
            if "homeassistant/" in str(c):
                assert c[1].get("retain", False) or (len(c[0]) > 2 and c[0][2]) or c[1].get("retain") is True, \
                    f"Discovery message not retained: {c}"


class TestMqttStatePublish:
    def test_publishes_home_state(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office", timestamp=_TS)
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        state_calls = [c for c in calls if "alice/state" in str(c)]
        assert len(state_calls) == 1
        assert state_calls[0][0][1] == "home"

    def test_publishes_room(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office", timestamp=_TS)
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        room_calls = [c for c in calls if "alice/room" in str(c)]
        assert len(room_calls) == 1
        assert room_calls[0][0][1] == "office"

    def test_publishes_not_home_state(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden", timestamp=_TS)
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        state_calls = [c for c in calls if "alice/state" in str(c)]
        assert state_calls[0][0][1] == "not_home"

    def test_publishes_empty_room_when_away(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden", timestamp=_TS)
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        room_calls = [c for c in calls if "alice/room" in str(c)]
        assert room_calls[0][0][1] == ""

    def test_publishes_attributes_with_timestamp(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office", timestamp=_TS, rssi=-45)
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        attr_calls = [c for c in calls if "alice/attributes" in str(c)]
        assert len(attr_calls) == 1
        attrs = json.loads(attr_calls[0][0][1])
        assert attrs["event_ts"] == _TS.isoformat()
        assert attrs["mac"] == "aa:bb:cc:dd:ee:01"
        assert attrs["node"] == "ap-office"
        assert attrs["rssi"] == -45

    def test_state_messages_are_retained(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office", timestamp=_TS)
        publisher.publish_state(change)

        for c in mock_client.publish.call_args_list:
            assert c[1].get("retain", False) or (len(c[0]) > 2 and c[0][2]) or c[1].get("retain") is True


class TestMqttLwt:
    def test_sets_lwt(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        mock_client.will_set.assert_called_once()
        args = mock_client.will_set.call_args
        assert "status" in str(args)
        assert "offline" in str(args)

    def test_lwt_uses_qos_1(self, sample_config):
        mock_client = MagicMock()
        MqttPublisher(sample_config, mock_client)
        kwargs = mock_client.will_set.call_args.kwargs
        assert kwargs.get("qos") == 1

    def test_publish_online(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_online()
        calls = mock_client.publish.call_args_list
        online_calls = [c for c in calls if "status" in str(c)]
        assert len(online_calls) == 1
        assert "online" in str(online_calls[0])


def _all_publish_qos_values(mock_client) -> list[int]:
    """Extract the qos value from every publish() call, default 0."""
    values: list[int] = []
    for c in mock_client.publish.call_args_list:
        if "qos" in c.kwargs:
            values.append(c.kwargs["qos"])
        elif len(c.args) >= 4:
            values.append(c.args[3])
        else:
            values.append(0)
    return values


class TestMqttQos:
    """Every outbound publish must use QoS 1 so broker hiccups don't lose messages."""

    def test_state_publishes_use_qos_1(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(
            person="alice", home=True, room="office",
            mac="aa:bb:cc:dd:ee:01", node="ap-office", timestamp=_TS,
        )
        publisher.publish_state(change)
        qos_values = _all_publish_qos_values(mock_client)
        assert qos_values, "expected at least one publish"
        assert all(q == 1 for q in qos_values), f"non-QoS-1 publish: {qos_values}"

    def test_discovery_publishes_use_qos_1(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_discovery()
        qos_values = _all_publish_qos_values(mock_client)
        assert qos_values
        assert all(q == 1 for q in qos_values)

    def test_online_publish_uses_qos_1(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_online()
        qos_values = _all_publish_qos_values(mock_client)
        assert qos_values
        assert all(q == 1 for q in qos_values)


class TestMqttStateCacheAndReconnect:
    """publish_state caches last emitted change; on_connected replays cache."""

    def _change(self, person: str, home: bool, room: str | None) -> StateChange:
        return StateChange(
            person=person,
            home=home,
            room=room,
            mac="aa:bb:cc:dd:ee:01",
            node="ap-office" if home else "ap-garden",
            timestamp=_TS,
            rssi=-50 if home else None,
        )

    def test_publish_state_updates_cache(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        c1 = self._change("alice", True, "office")
        publisher.publish_state(c1)
        assert publisher.cached_state("alice") == c1

    def test_publish_state_overwrites_previous_cache_entry(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_state(self._change("alice", True, "office"))
        c2 = self._change("alice", False, None)
        publisher.publish_state(c2)
        assert publisher.cached_state("alice") == c2

    def test_on_connected_with_empty_cache_publishes_discovery_and_online(
        self, sample_config,
    ):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.on_connected()
        topics = [c.args[0] for c in mock_client.publish.call_args_list]
        # discovery: 2 people * 2 entities = 4
        assert sum(1 for t in topics if t.startswith("homeassistant/")) == 4
        assert any(t.endswith("/status") for t in topics)
        # no state topics yet (cache empty)
        assert not any("/state" in t or "/room" in t or "/attributes" in t
                       for t in topics)

    def test_on_connected_replays_cached_state(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_state(self._change("alice", True, "office"))
        publisher.publish_state(self._change("bob", False, None))
        mock_client.reset_mock()

        publisher.on_connected()

        topics = [c.args[0] for c in mock_client.publish.call_args_list]
        # discovery (4) + online (1) + 2 persons * 3 topics (state/room/attributes) = 11
        assert "openwrt-presence/alice/state" in topics
        assert "openwrt-presence/bob/state" in topics
        assert "openwrt-presence/alice/room" in topics
        assert "openwrt-presence/alice/attributes" in topics

    def test_publish_state_writes_audit_log_line(self, sample_config):
        """publish_state is the single gate that emits + logs.  Callers
        must not have to remember to log separately."""
        stream = io.StringIO()
        setup_logging(file=stream)
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_state(self._change("alice", True, "office"))

        lines = [line for line in stream.getvalue().splitlines() if line]
        state_change_lines = [
            json.loads(line) for line in lines
            if json.loads(line).get("message") == "state_change"
        ]
        assert len(state_change_lines) == 1
        assert state_change_lines[0]["person"] == "alice"
        assert state_change_lines[0]["presence"] == "home"

    def test_on_connected_does_not_relog_cached_state(self, sample_config):
        """Reconnect republishing is protocol-level; the state_change
        already happened and was already logged when publish_state ran."""
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_state(self._change("alice", True, "office"))

        stream = io.StringIO()
        setup_logging(file=stream)
        publisher.on_connected()

        for line in stream.getvalue().splitlines():
            if not line:
                continue
            data = json.loads(line)
            assert data.get("message") != "state_change", \
                f"on_connected must not emit state_change: {data}"

    def test_on_connected_idempotent(self, sample_config):
        """Two calls to on_connected must issue the same publishes each time."""
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_state(self._change("alice", True, "office"))
        mock_client.reset_mock()

        publisher.on_connected()
        first_count = len(mock_client.publish.call_args_list)
        mock_client.reset_mock()

        publisher.on_connected()
        second_count = len(mock_client.publish.call_args_list)

        assert first_count == second_count
        assert first_count > 0
