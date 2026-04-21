from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from openwrt_presence.domain import (
    AwayState,
    HomeState,
    Mac,
    NodeName,
    PersonName,
    PersonState,
    Room,
    StateChange,
    StationReading,
)

if TYPE_CHECKING:
    from openwrt_presence.config import Config


class DeviceState(Enum):
    CONNECTED = "connected"
    DEPARTING = "departing"
    AWAY = "away"


@dataclass
class _DeviceTracker:
    """Internal per-device state tracker."""

    state: DeviceState = DeviceState.AWAY
    node: NodeName = field(default_factory=lambda: NodeName(""))
    rssi: int = -100
    departure_deadline: datetime | None = None


class PresenceEngine:
    """Pure-logic presence state machine.

    Receives timestamps as arguments — never calls ``datetime.now`` itself.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._devices: dict[Mac, _DeviceTracker] = {}
        self._last_person_state: dict[PersonName, PersonState] = {}

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
        visible: dict[Mac, StationReading] = {}
        for r in readings:
            mac = r.mac
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
                timeout = self._config.timeout_for_node(tracker.node)
                tracker.departure_deadline = now + timedelta(seconds=timeout)

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
        affected: set[PersonName] = set()
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

    def get_person_state(self, name: PersonName) -> PersonState:
        """Return the current aggregated state for a person."""
        return self._compute_person_state(name)

    def get_person_snapshot(self, name: PersonName, now: datetime) -> StateChange:
        """Return the current aggregated state as a :class:`StateChange`.

        Unlike :meth:`process_snapshot` this always returns a value regardless
        of whether the state has transitioned — it is intended for startup
        seeding and post-reconnect reconciliation.  For a person that has
        never been seen, returns an :class:`AwayState` with
        ``last_mac``/``last_node`` set to ``None``.
        """
        state = self._compute_person_state(name)
        rep = self._best_representative(name)
        if state.home and rep is not None:
            mac, node, rssi = rep
            return HomeState(
                person=name,
                room=state.room if state.room is not None else Room(""),
                mac=mac,
                node=node,
                timestamp=now,
                rssi=rssi,
            )
        if rep is not None:
            mac, node, _ = rep
            return AwayState(
                person=name,
                timestamp=now,
                last_mac=mac,
                last_node=node,
            )
        return AwayState(person=name, timestamp=now)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _best_representative(
        self, person: PersonName
    ) -> tuple[Mac, NodeName, int] | None:
        """Pick the strongest-RSSI device for *person* to represent them.

        Returns ``None`` if the person is unknown or has no tracked
        devices seen yet.  When non-None, ``rssi`` is the device's last
        recorded RSSI (integer, possibly the default -100 sentinel from
        the tracker).
        """
        person_cfg = self._config.people.get(person)
        if person_cfg is None:
            return None

        best: tuple[Mac, NodeName, int] | None = None
        best_rssi_val = -200

        for mac in person_cfg.macs:
            tracker = self._devices.get(mac)
            if tracker is None:
                continue
            if tracker.rssi > best_rssi_val:
                best = (mac, tracker.node, tracker.rssi)
                best_rssi_val = tracker.rssi

        return best

    def _compute_person_state(self, name: PersonName) -> PersonState:
        """Aggregate device states into a person state.

        Room is determined by the CONNECTED device with the strongest RSSI.
        If all devices are DEPARTING, the last known room is preserved.

        Precondition: *name* must be in ``config.people``.  Callers iterate
        the config; an unknown person is a programmer error, not a runtime
        case.
        """
        assert name in self._config.people, (
            f"unknown person {name!r} — callers must iterate config.people"
        )
        person_cfg = self._config.people[name]

        home = False
        best_room: Room | None = None
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
        self, person: PersonName, timestamp: datetime
    ) -> list[StateChange]:
        """Compare computed person state to last published; emit if changed."""
        new_state = self._compute_person_state(person)
        old_state = self._last_person_state.get(
            person, PersonState(home=False, room=None)
        )

        if new_state == old_state:
            return []

        self._last_person_state[person] = new_state

        rep = self._best_representative(person)
        if new_state.home and rep is not None:
            mac, node, rssi = rep
            return [
                HomeState(
                    person=person,
                    room=new_state.room if new_state.room is not None else Room(""),
                    mac=mac,
                    node=node,
                    timestamp=timestamp,
                    rssi=rssi,
                )
            ]
        if rep is not None:
            mac, node, _ = rep
            return [
                AwayState(
                    person=person,
                    timestamp=timestamp,
                    last_mac=mac,
                    last_node=node,
                )
            ]
        return [AwayState(person=person, timestamp=timestamp)]
