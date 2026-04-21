from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

import structlog

from openwrt_presence.domain import AwayState, HomeState
from openwrt_presence.logging import log_state_computed, log_state_delivered

if TYPE_CHECKING:
    from openwrt_presence.config import Config
    from openwrt_presence.domain import PersonName, StateChange


_QOS = 1

_log = structlog.get_logger()


class MqttPublisher:
    """Publishes presence state to Home Assistant via MQTT.

    Handles HA MQTT Discovery, state updates, and availability (LWT).
    All publishes use QoS 1 so messages survive broker hiccups: paho queues
    unacked messages locally and retransmits on reconnect.

    Maintains an in-process cache of the last :class:`StateChange` per
    person so ``on_connected`` can re-seed the broker after a (re)connection.

    Does NOT wire the LWT in ``__init__`` — the caller must call
    ``client.will_set(publisher.availability_topic, publisher.OFFLINE_PAYLOAD,
    qos=1, retain=True)`` BEFORE ``connect_async``.  Keeping this out of
    the constructor makes the ordering constraint explicit (H1).
    """

    OFFLINE_PAYLOAD: Final[str] = "offline"
    ONLINE_PAYLOAD: Final[str] = "online"

    def __init__(self, config: Config, client: Any) -> None:
        self._config = config
        self._client = client
        self._topic_prefix = config.mqtt.topic_prefix
        self._last_state: dict[PersonName, StateChange] = {}

    @property
    def availability_topic(self) -> str:
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

    def _publish_device_tracker_discovery(self, person: PersonName) -> None:
        topic = f"homeassistant/device_tracker/{person}_wifi/config"
        payload = {
            "name": f"{person.title()} WiFi",
            "unique_id": f"openwrt_presence_{person}_wifi",
            "state_topic": f"{self._topic_prefix}/{person}/state",
            "json_attributes_topic": f"{self._topic_prefix}/{person}/attributes",
            "payload_home": "home",
            "payload_not_home": "not_home",
            "source_type": "router",
            "availability_topic": self.availability_topic,
            "device": self._device_block(),
        }
        self._client.publish(topic, json.dumps(payload), qos=_QOS, retain=True)

    def _publish_room_sensor_discovery(self, person: PersonName) -> None:
        topic = f"homeassistant/sensor/{person}_room/config"
        payload = {
            "name": f"{person.title()} Room",
            "unique_id": f"openwrt_presence_{person}_room",
            "state_topic": f"{self._topic_prefix}/{person}/room",
            "availability_topic": self.availability_topic,
            "icon": "mdi:map-marker",
            "device": self._device_block(),
        }
        self._client.publish(topic, json.dumps(payload), qos=_QOS, retain=True)

    def publish_state(self, change: StateChange) -> None:
        """Publish state, room, and attributes for a person.

        Audit log ordering:
          1. state_computed — ALWAYS (the engine decided this change)
          2. _emit_state    — returns True iff all 3 publishes had rc == 0
          3. state_delivered — ONLY if (2) succeeded

        A computed without a matching delivered means silent data loss.
        The cached StateChange is retained regardless so on_connected
        can replay it on reconnect.
        """
        self._last_state[change.person] = change
        log_state_computed(change)
        if self._emit_state(change):
            log_state_delivered(change)

    def _emit_state(self, change: StateChange) -> bool:
        """Publish the 3 topics for a person's state change.

        Returns True iff every publish returned rc == 0 (paho accepted
        the message for delivery).  Returns False if ANY topic publish
        failed — caller uses this to gate the state_delivered audit
        line (see Task 2.6).  Each failure is logged loud.
        """
        attrs: dict[str, Any]
        match change:
            case HomeState():
                state_value = "home"
                room_value: str = change.room
                attrs = {
                    "event_ts": change.timestamp.isoformat(),
                    "mac": change.mac,
                    "node": change.node,
                    "rssi": change.rssi,
                }
            case AwayState(last_mac=None):
                state_value = "not_home"
                room_value = ""
                attrs = {"event_ts": change.timestamp.isoformat()}
            case AwayState():
                state_value = "not_home"
                room_value = ""
                attrs = {
                    "event_ts": change.timestamp.isoformat(),
                    "mac": change.last_mac,
                    "node": change.last_node,
                    "rssi": None,
                }

        topics_payloads = (
            (f"{self._topic_prefix}/{change.person}/state", state_value),
            (f"{self._topic_prefix}/{change.person}/room", room_value),
            (
                f"{self._topic_prefix}/{change.person}/attributes",
                json.dumps(attrs),
            ),
        )

        all_ok = True
        for topic, payload in topics_payloads:
            info = self._client.publish(topic, payload, qos=_QOS, retain=True)
            if info.rc != 0:
                all_ok = False
                _log.error(
                    "publish_failed",
                    topic=topic,
                    rc=info.rc,
                    person=change.person,
                )
        return all_ok

    def publish_online(self) -> None:
        """Publish 'online' to the availability topic."""
        self._client.publish(
            self.availability_topic,
            self.ONLINE_PAYLOAD,
            qos=_QOS,
            retain=True,
        )

    def cached_state(self, person: PersonName) -> StateChange | None:
        """Return the last :class:`StateChange` emitted for *person*, if any."""
        return self._last_state.get(person)

    def on_connected(self) -> None:
        """Republish discovery, availability, and cached per-person state.

        Call from the paho ``on_connect`` callback.  This is how we recover
        from broker restarts (including Mosquitto major-version upgrades
        that may drop retained state): the first successful reconnect
        re-seeds the broker with current truth.
        """
        self.publish_discovery()
        self.publish_online()
        for change in self._last_state.values():
            self._emit_state(change)
