from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openwrt_presence.config import Config
    from openwrt_presence.engine import StateChange


class MqttPublisher:
    """Publishes presence state to Home Assistant via MQTT.

    Handles HA MQTT Discovery, state updates, and availability (LWT).
    """

    def __init__(self, config: Config, client: Any) -> None:
        self._config = config
        self._client = client
        self._topic_prefix = config.mqtt.topic_prefix

        # Set up Last Will and Testament so HA knows if we crash
        self._client.will_set(
            f"{self._topic_prefix}/status",
            payload="offline",
            retain=True,
        )

    @property
    def _availability_topic(self) -> str:
        return f"{self._topic_prefix}/status"

    @staticmethod
    def _device_block() -> dict[str, Any]:
        return {
            "identifiers": ["openwrt_presence"],
            "name": "OpenWrt Presence",
            "manufacturer": "openwrt-presence",
        }

    def publish_discovery(self) -> None:
        """Publish HA MQTT Discovery config for every tracked person."""
        for person in self._config.people:
            self._publish_device_tracker_discovery(person)
            self._publish_room_sensor_discovery(person)

    def _publish_device_tracker_discovery(self, person: str) -> None:
        topic = f"homeassistant/device_tracker/{person}_wifi/config"
        payload = {
            "name": f"{person.title()} WiFi",
            "unique_id": f"openwrt_presence_{person}_wifi",
            "state_topic": f"{self._topic_prefix}/{person}/state",
            "json_attributes_topic": f"{self._topic_prefix}/{person}/attributes",
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "router",
            "availability_topic": self._availability_topic,
            "device": self._device_block(),
        }
        self._client.publish(topic, json.dumps(payload), retain=True)

    def _publish_room_sensor_discovery(self, person: str) -> None:
        topic = f"homeassistant/sensor/{person}_room/config"
        payload = {
            "name": f"{person.title()} Room",
            "unique_id": f"openwrt_presence_{person}_room",
            "state_topic": f"{self._topic_prefix}/{person}/room",
            "availability_topic": self._availability_topic,
            "icon": "mdi:map-marker",
            "device": self._device_block(),
        }
        self._client.publish(topic, json.dumps(payload), retain=True)

    def publish_state(self, change: StateChange) -> None:
        """Publish state, room, and attributes for a person."""
        state_value = "home" if change.home else "not_home"
        room_value = change.room if change.room is not None else ""

        self._client.publish(
            f"{self._topic_prefix}/{change.person}/state",
            state_value,
            retain=True,
        )
        self._client.publish(
            f"{self._topic_prefix}/{change.person}/room",
            room_value,
            retain=True,
        )
        self._client.publish(
            f"{self._topic_prefix}/{change.person}/attributes",
            json.dumps({
                "event_ts": change.timestamp.isoformat(),
                "mac": change.mac,
                "node": change.node,
            }),
            retain=True,
        )

    def publish_online(self) -> None:
        """Publish 'online' to the availability topic."""
        self._client.publish(
            self._availability_topic,
            "online",
            retain=True,
        )
