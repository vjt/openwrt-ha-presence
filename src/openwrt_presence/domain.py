"""Shared value vocabulary. Engine + sources + mqtt + audit import
from here; nothing in domain.py imports from any of them.

The `StateChange` discriminated union lands in Task 3.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NewType

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


@dataclass
class StateChange:
    person: PersonName
    home: bool
    room: Room | None  # None when away
    mac: Mac
    node: NodeName
    timestamp: datetime
    rssi: int | None = None


@dataclass
class PersonState:
    home: bool
    room: Room | None
