"""Shared value vocabulary. Engine + sources + mqtt + audit import
from here; nothing in domain.py imports from any of them.

``StateChange`` is a discriminated union of ``HomeState`` (tracked-device
visible) and ``AwayState`` (all tracked devices timed out, or the person
has no tracked devices yet).  Consumers pattern-match on the variant to
get narrowed access to room/mac/node/rssi — unrepresentable states are
unrepresentable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NewType

Mac = NewType("Mac", str)  # lowercase, colon-separated; normalized at boundary
PersonName = NewType("PersonName", str)
NodeName = NewType("NodeName", str)
Room = NewType("Room", str)


@dataclass
class StationReading:
    """A single RSSI measurement from a Prometheus-compatible TSDB."""

    mac: Mac  # lowercase, colon-separated
    ap: NodeName  # AP hostname (instance label)
    rssi: int  # signal strength in dBm


@dataclass(frozen=True)
class HomeState:
    """Person is home — room/mac/node/rssi all known and non-optional."""

    person: PersonName
    room: Room
    mac: Mac
    node: NodeName
    timestamp: datetime
    rssi: int
    home: Literal[True] = True


@dataclass(frozen=True)
class AwayState:
    """Person is away.

    ``last_mac``/``last_node`` hold the best-representative device's
    last known location at the moment the person departed, or ``None``
    if the person has never been tracked (e.g. freshly-configured
    startup seed).
    """

    person: PersonName
    timestamp: datetime
    last_mac: Mac | None = None
    last_node: NodeName | None = None
    home: Literal[False] = False


StateChange = HomeState | AwayState


@dataclass
class PersonState:
    home: bool
    room: Room | None
