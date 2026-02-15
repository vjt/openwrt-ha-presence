from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openwrt_presence.config import Config, NodeConfig


class DeviceState(Enum):
    CONNECTED = "connected"
    DEPARTING = "departing"
    AWAY = "away"


@dataclass
class StationReading:
    """A single RSSI measurement from a Prometheus-compatible TSDB."""

    mac: str  # lowercase, colon-separated
    ap: str  # AP hostname (instance label)
    rssi: int  # signal strength in dBm


@dataclass
class StateChange:
    person: str
    home: bool
    room: str | None  # None when away
    mac: str
    node: str
    timestamp: datetime
    rssi: int | None = None


@dataclass
class PersonState:
    home: bool
    room: str | None


@dataclass
class _DeviceTracker:
    """Internal per-device state tracker."""

    state: DeviceState = DeviceState.AWAY
    node: str = ""
    rssi: int = -100
    departure_deadline: datetime | None = None


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

    def process_snapshot(
        self, now: datetime, readings: list[StationReading]
    ) -> list[StateChange]:
        """Process a complete station snapshot and return person-level changes.

        For each tracked MAC:
        - If visible in the snapshot → CONNECTED, update node/rssi.
        - If not visible and was CONNECTED → DEPARTING, set departure deadline.
        - If DEPARTING and deadline passed → AWAY.
        """
        # Build best-RSSI-per-MAC lookup from readings (tracked MACs only)
        visible: dict[str, StationReading] = {}
        for r in readings:
            mac = r.mac.lower()
            if self._config.mac_to_person(mac) is None:
                continue
            if mac not in visible or r.rssi > visible[mac].rssi:
                visible[mac] = StationReading(mac=mac, ap=r.ap, rssi=r.rssi)

        # Update visible MACs → CONNECTED
        for mac, reading in visible.items():
            tracker = self._devices.setdefault(mac, _DeviceTracker())
            tracker.state = DeviceState.CONNECTED
            tracker.node = reading.ap
            tracker.rssi = reading.rssi
            tracker.departure_deadline = None

        # Update disappeared MACs → DEPARTING
        for mac, tracker in self._devices.items():
            if mac in visible:
                continue
            if tracker.state == DeviceState.CONNECTED:
                tracker.state = DeviceState.DEPARTING
                tracker.departure_deadline = now + timedelta(
                    seconds=self._config.departure_timeout
                )

        # Expire DEPARTING → AWAY
        for mac, tracker in self._devices.items():
            if (
                tracker.state == DeviceState.DEPARTING
                and tracker.departure_deadline is not None
                and now >= tracker.departure_deadline
            ):
                tracker.state = DeviceState.AWAY
                tracker.departure_deadline = None

        # Emit changes for all affected persons
        affected: set[str] = set()
        for mac in visible:
            person = self._config.mac_to_person(mac)
            if person:
                affected.add(person)
        for mac, tracker in self._devices.items():
            if tracker.state in (DeviceState.DEPARTING, DeviceState.AWAY):
                person = self._config.mac_to_person(mac)
                if person:
                    affected.add(person)

        changes: list[StateChange] = []
        for person in affected:
            changes.extend(self._emit_changes(person, now))

        return changes

    def tick(self, now: datetime) -> list[StateChange]:
        """Check all departure timers and return person-level changes."""
        changes: list[StateChange] = []

        for mac, tracker in self._devices.items():
            if tracker.state != DeviceState.DEPARTING:
                continue
            if (
                tracker.departure_deadline is not None
                and now >= tracker.departure_deadline
            ):
                tracker.state = DeviceState.AWAY
                tracker.departure_deadline = None
                person = self._config.mac_to_person(mac)
                if person is not None:
                    changes.extend(self._emit_changes(person, now))

        return changes

    def get_person_state(self, name: str) -> PersonState:
        """Return the current aggregated state for a person."""
        return self._compute_person_state(name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_person_state(self, name: str) -> PersonState:
        """Aggregate device states into a person state.

        Room is determined by the CONNECTED device with the strongest RSSI.
        If all devices are DEPARTING, the last known room is preserved.
        """
        person_cfg = self._config.people.get(name)
        if person_cfg is None:
            return PersonState(home=False, room=None)

        home = False
        best_room: str | None = None
        best_rssi: int = -200  # impossibly low

        for mac in person_cfg.macs:
            tracker = self._devices.get(mac)
            if tracker is None:
                continue

            if tracker.state in (DeviceState.CONNECTED, DeviceState.DEPARTING):
                home = True

            # Room follows the CONNECTED device with strongest RSSI
            if tracker.state == DeviceState.CONNECTED and tracker.rssi > best_rssi:
                node_cfg = self._config.nodes.get(tracker.node)
                room = node_cfg.room if node_cfg is not None else None
                best_room = room
                best_rssi = tracker.rssi

        if not home:
            return PersonState(home=False, room=None)

        # If all devices are DEPARTING (none CONNECTED), fall back to
        # any DEPARTING device's last known room.
        if best_room is None:
            for mac in person_cfg.macs:
                tracker = self._devices.get(mac)
                if tracker and tracker.state == DeviceState.DEPARTING:
                    node_cfg = self._config.nodes.get(tracker.node)
                    best_room = node_cfg.room if node_cfg is not None else None
                    break

        return PersonState(home=True, room=best_room)

    def _emit_changes(
        self, person: str, timestamp: datetime
    ) -> list[StateChange]:
        """Compare computed person state to last published; emit if changed."""
        new_state = self._compute_person_state(person)
        old_state = self._last_person_state.get(
            person, PersonState(home=False, room=None)
        )

        if new_state == old_state:
            return []

        self._last_person_state[person] = new_state

        # Find the best representative device for the state change
        person_cfg = self._config.people[person]
        best_mac = ""
        best_node = ""
        best_rssi: int | None = None
        best_rssi_val = -200

        for mac in person_cfg.macs:
            tracker = self._devices.get(mac)
            if tracker is None:
                continue
            if tracker.rssi > best_rssi_val:
                best_mac = mac
                best_node = tracker.node
                best_rssi = tracker.rssi
                best_rssi_val = tracker.rssi

        return [
            StateChange(
                person=person,
                home=new_state.home,
                room=new_state.room,
                mac=best_mac,
                node=best_node,
                timestamp=timestamp,
                rssi=best_rssi,
            )
        ]
