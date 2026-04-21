"""Shared value vocabulary. Engine + sources + mqtt + audit import
from here; nothing in domain.py imports from any of them.

The `Mac`/`PersonName`/`NodeName`/`Room` NewTypes land in Task 3.2.
The `StateChange` discriminated union lands in Task 3.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
