from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

_HOSTAPD_RE = re.compile(
    r"^\S+:\s+AP-STA-(CONNECTED|DISCONNECTED)\s+"
    r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
)

_EVENT_MAP: dict[str, Literal["connect", "disconnect"]] = {
    "CONNECTED": "connect",
    "DISCONNECTED": "disconnect",
}


@dataclass
class PresenceEvent:
    event: Literal["connect", "disconnect"]
    mac: str
    node: str
    timestamp: datetime


def parse_hostapd_message(
    msg: str,
    node: str,
    *,
    timestamp: datetime | None = None,
) -> PresenceEvent | None:
    """Parse a raw hostapd log message and return a :class:`PresenceEvent`.

    Returns ``None`` if *msg* is not a connect/disconnect event.
    """
    match = _HOSTAPD_RE.search(msg)
    if match is None:
        return None

    event_type = _EVENT_MAP[match.group(1)]
    mac = match.group(2).lower()

    return PresenceEvent(
        event=event_type,
        mac=mac,
        node=node,
        timestamp=timestamp if timestamp is not None else datetime.now(timezone.utc),
    )


def parse_victorialogs_line(line: str) -> PresenceEvent | None:
    """Parse a JSONL line from VictoriaLogs and return a :class:`PresenceEvent`.

    Returns ``None`` if the line is malformed, missing required fields,
    or the embedded message is not a connect/disconnect event.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    try:
        msg = data["_msg"]
        hostname = data["tags.hostname"]
        time_str = data["_time"]
    except (KeyError, TypeError):
        return None

    timestamp = datetime.fromisoformat(time_str)

    return parse_hostapd_message(msg, hostname, timestamp=timestamp)
