from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from openwrt_presence.domain import (
    ApEvent,
    ApEventKind,
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


def _recency(reading: StationReading) -> tuple[int, int]:
    """Rank key: most recently heard first, strongest RSSI on a tie."""
    return (-reading.inactive_ms, reading.rssi)


@dataclass
class _DeviceTracker:
    """Internal per-device state tracker."""

    state: DeviceState = DeviceState.AWAY
    node: NodeName = field(default_factory=lambda: NodeName(""))
    rssi: int = -100
    departure_deadline: datetime | None = None
    # Set by an AP assoc event: until then, scrapes cannot move the device
    # off ``node`` nor start its departure (see process_event).
    hold_until: datetime | None = None


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

    def process_snapshot(  # noqa: PLR0912 — three sequential per-MAC passes (visible→CONNECTED, disappeared→DEPARTING, expired→AWAY) cohere as one logical operation; splitting would add call plumbing without clarifying control flow
        self, now: datetime, readings: list[StationReading]
    ) -> list[StateChange]:
        """Process a complete station snapshot and return person-level changes.

        For each tracked MAC:
        - If visible in the snapshot → CONNECTED, update node/rssi from the
          reading with the smallest ``inactive_ms`` (ties: strongest RSSI).
        - If not visible and was CONNECTED → DEPARTING, set departure deadline.
        - If DEPARTING and deadline passed → AWAY.

        Returns the person-level changes; ``[]`` when nothing changed.
        Raises nothing.
        """
        # A station is associated to one AP at a time. The same MAC on
        # several APs is a stale entry on the AP it left, kept until that
        # hostapd's inactivity timeout (minutes) with the RSSI of its last
        # frame — as strong as the live one or stronger. Its inactive time
        # only grows; the AP the station is on heard it last.
        tracked = [r for r in readings if self._config.mac_to_person(r.mac) is not None]
        visible: dict[Mac, StationReading] = {}
        for r in tracked:
            mac = r.mac
            if mac not in visible or _recency(r) > _recency(visible[mac]):
                visible[mac] = r
        self._apply_holds(now, tracked, visible)

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
        for _mac, tracker in self._devices.items():
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

    def process_event(self, now: datetime, event: ApEvent) -> list[StateChange]:
        """Apply an AP push event and return person-level changes.

        - ``ASSOC`` for a tracked MAC → CONNECTED on ``event.node``, RSSI
          from the event (``None`` keeps the last one), departure timer
          cancelled, and a hold of ``config.event_hold`` seconds during
          which scrapes cannot move the device off that node nor start
          its departure: a scrape in flight during the roam still carries
          the old AP's stale entry and would undo the event at once.
        - ``DISASSOC`` → no state change. Departures stay scrape-driven;
          a disassoc during roaming or doze must not mark anyone away.
        - Untracked MAC → no state change.

        Returns ``[]`` or one :class:`HomeState`. Raises nothing.
        """
        if event.kind is ApEventKind.DISASSOC:
            return []
        person = self._config.mac_to_person(event.mac)
        if person is None:
            return []
        tracker = self._devices.setdefault(event.mac, _DeviceTracker())
        tracker.state = DeviceState.CONNECTED
        tracker.node = event.node
        if event.rssi is not None:
            tracker.rssi = event.rssi
        tracker.departure_deadline = None
        tracker.hold_until = now + timedelta(seconds=self._config.event_hold)
        return self._emit_changes(person, now)

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

    def _apply_holds(
        self,
        now: datetime,
        tracked: list[StationReading],
        visible: dict[Mac, StationReading],
    ) -> None:
        """Pin held devices to their event node in *visible* (mutated).

        While held, only a reading on the held node may refresh the RSSI;
        absence from the scrape counts as visible. Expired holds are cleared.
        """
        for mac, tracker in self._devices.items():
            if tracker.hold_until is None:
                continue
            if now >= tracker.hold_until:
                tracker.hold_until = None
                continue
            on_node = [r.rssi for r in tracked if r.mac == mac and r.ap == tracker.node]
            rssi = max(on_node) if on_node else tracker.rssi
            # Synthetic: the hold, not the scrape, puts the device here, and
            # the readings are already ranked. 0 = the assoc event is the
            # freshest evidence there is.
            visible[mac] = StationReading(
                mac=mac, ap=tracker.node, rssi=rssi, inactive_ms=0
            )

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
