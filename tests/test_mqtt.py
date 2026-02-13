import json
from unittest.mock import MagicMock, call

from openwrt_presence.config import Config
from openwrt_presence.engine import StateChange
from openwrt_presence.mqtt import MqttPublisher


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
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office")
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        state_calls = [c for c in calls if "alice/state" in str(c)]
        assert len(state_calls) == 1
        assert state_calls[0][0][1] == "home"

    def test_publishes_room(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office")
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        room_calls = [c for c in calls if "alice/room" in str(c)]
        assert len(room_calls) == 1
        assert room_calls[0][0][1] == "office"

    def test_publishes_not_home_state(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden")
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        state_calls = [c for c in calls if "alice/state" in str(c)]
        assert state_calls[0][0][1] == "not_home"

    def test_publishes_empty_room_when_away(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=False, room=None, mac="aa:bb:cc:dd:ee:01", node="ap-garden")
        publisher.publish_state(change)

        calls = mock_client.publish.call_args_list
        room_calls = [c for c in calls if "alice/room" in str(c)]
        assert room_calls[0][0][1] == ""

    def test_state_messages_are_retained(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        change = StateChange(person="alice", home=True, room="office", mac="aa:bb:cc:dd:ee:01", node="ap-office")
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

    def test_publish_online(self, sample_config):
        mock_client = MagicMock()
        publisher = MqttPublisher(sample_config, mock_client)
        publisher.publish_online()
        calls = mock_client.publish.call_args_list
        online_calls = [c for c in calls if "status" in str(c)]
        assert len(online_calls) == 1
        assert "online" in str(online_calls[0])
