from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openwrt_presence.config import Config, NodeConfig
    from openwrt_presence.parser import PresenceEvent


class DeviceState(Enum):
    CONNECTED = "connected"
    DEPARTING = "departing"
    AWAY = "away"


@dataclass
class StateChange:
    person: str
    home: bool
    room: str | None  # None when away
    mac: str
    node: str
    timestamp: datetime


@dataclass
class PersonState:
    home: bool
    room: str | None


@dataclass
class _DeviceTracker:
    """Internal per-device state tracker."""

    state: DeviceState = DeviceState.AWAY
    node: str = ""
    last_connect_time: datetime | None = None
    exit_deadline: datetime | None = None
    last_disconnect_time: datetime | None = None


class PresenceEngine:
    """Pure-logic presence state machine.

    Receives timestamps as arguments — never calls ``datetime.now`` itself.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._devices: dict[str, _DeviceTracker] = {}
        self._last_person_state: dict[str, PersonState] = {}

        # Initialise every known person to away
        for name in config.people:
            self._last_person_state[name] = PersonState(home=False, room=None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_event(self, event: PresenceEvent) -> list[StateChange]:
        """Process a single presence event and return person-level changes."""
        person = self._config.mac_to_person(event.mac)
        if person is None:
            return []

        tracker = self._devices.setdefault(event.mac, _DeviceTracker())
        node_cfg = self._config.nodes.get(event.node)

        if event.event == "connect":
            self._handle_connect(tracker, event, node_cfg)
        elif event.event == "disconnect":
            self._handle_disconnect(tracker, event, node_cfg)

        return self._emit_changes(person, event.mac, event.node, event.timestamp)

    def tick(self, now: datetime) -> list[StateChange]:
        """Check all departure/global timers and return person-level changes."""
        changes: list[StateChange] = []

        for mac, tracker in self._devices.items():
            if tracker.state == DeviceState.AWAY:
                continue

            transitioned = False

            if tracker.state == DeviceState.DEPARTING:
                # Check exit deadline
                if (
                    tracker.exit_deadline is not None
                    and now >= tracker.exit_deadline
                ):
                    tracker.state = DeviceState.AWAY
                    tracker.exit_deadline = None
                    transitioned = True

                # Check global timeout (time since last connect)
                if (
                    not transitioned
                    and tracker.last_connect_time is not None
                    and (now - tracker.last_connect_time).total_seconds()
                    >= self._config.away_timeout
                ):
                    tracker.state = DeviceState.AWAY
                    tracker.exit_deadline = None
                    transitioned = True

            if transitioned:
                person = self._config.mac_to_person(mac)
                if person is not None:
                    new_changes = self._emit_changes(person, mac, tracker.node, now)
                    changes.extend(new_changes)

        return changes

    def get_person_state(self, name: str) -> PersonState:
        """Return the current aggregated state for a person."""
        return self._compute_person_state(name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _handle_connect(
        self,
        tracker: _DeviceTracker,
        event: PresenceEvent,
        node_cfg: NodeConfig | None,
    ) -> None:
        tracker.state = DeviceState.CONNECTED
        tracker.node = event.node
        tracker.last_connect_time = event.timestamp
        tracker.exit_deadline = None  # cancel any departure timer
        tracker.last_disconnect_time = None

    def _handle_disconnect(
        self,
        tracker: _DeviceTracker,
        event: PresenceEvent,
        node_cfg: NodeConfig | None,
    ) -> None:
        tracker.state = DeviceState.DEPARTING
        tracker.last_disconnect_time = event.timestamp

        if node_cfg is not None and node_cfg.type == "exit":
            # Exit node: set departure deadline
            assert node_cfg.timeout is not None
            tracker.exit_deadline = event.timestamp + timedelta(
                seconds=node_cfg.timeout
            )
        else:
            # Interior or unknown node: no exit deadline
            tracker.exit_deadline = None

    def _compute_person_state(self, name: str) -> PersonState:
        """Aggregate device states into a person state."""
        person_cfg = self._config.people.get(name)
        if person_cfg is None:
            return PersonState(home=False, room=None)

        home = False
        best_room: str | None = None
        best_time: datetime | None = None

        for mac in person_cfg.macs:
            tracker = self._devices.get(mac)
            if tracker is None:
                continue

            if tracker.state in (DeviceState.CONNECTED, DeviceState.DEPARTING):
                home = True

            # Room follows the most recently CONNECTED MAC
            if tracker.last_connect_time is not None and (
                best_time is None or tracker.last_connect_time > best_time
            ):
                # Only set room if the device is CONNECTED or DEPARTING
                if tracker.state in (
                    DeviceState.CONNECTED,
                    DeviceState.DEPARTING,
                ):
                    node_cfg = self._config.nodes.get(tracker.node)
                    room = node_cfg.room if node_cfg is not None else None
                    best_room = room
                    best_time = tracker.last_connect_time

        if not home:
            return PersonState(home=False, room=None)

        return PersonState(home=True, room=best_room)

    def _emit_changes(
        self, person: str, mac: str, node: str, timestamp: datetime
    ) -> list[StateChange]:
        """Compare computed person state to last published; emit if changed."""
        new_state = self._compute_person_state(person)
        old_state = self._last_person_state.get(
            person, PersonState(home=False, room=None)
        )

        if new_state == old_state:
            return []

        self._last_person_state[person] = new_state

        return [
            StateChange(
                person=person,
                home=new_state.home,
                room=new_state.room,
                mac=mac,
                node=node,
                timestamp=timestamp,
            )
        ]
