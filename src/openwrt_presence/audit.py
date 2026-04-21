"""Audit trail for state transitions.

Moved out of ``logging.py`` so that ``logging.py`` is purely the
structlog setup boundary.  Audit lines are a domain concern — they
describe what the engine decided and whether the publish landed.

The wire format is part of the HA-facing contract: ``openwrt-monitor``
parses these lines; external log shippers filter on ``message``.  Do
not change field names or semantics without a Migration note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from openwrt_presence.domain import AwayState, HomeState

if TYPE_CHECKING:
    from openwrt_presence.domain import StateChange


def _state_fields(change: StateChange) -> dict[str, Any]:
    match change:
        case HomeState():
            return {
                "person": change.person,
                "presence": "home",
                "room": change.room,
                "mac": change.mac,
                "node": change.node,
                "rssi": change.rssi,
                "event_ts": change.timestamp.isoformat(),
            }
        case AwayState():
            return {
                "person": change.person,
                "presence": "away",
                "room": None,
                "mac": change.last_mac,
                "node": change.last_node,
                "rssi": None,
                "event_ts": change.timestamp.isoformat(),
            }


def log_state_computed(change: StateChange) -> None:
    """Audit log: the engine decided this state transition.

    Emitted always, regardless of whether the MQTT publish succeeds.
    Paired with :func:`log_state_delivered` — a computed without a
    matching delivered within the same log burst means the publish
    dropped silently.
    """
    structlog.get_logger().info("state_computed", **_state_fields(change))


def log_state_delivered(change: StateChange) -> None:
    """Audit log: all three topic publishes accepted by paho (rc == 0).

    Emitted only when :meth:`MqttPublisher._emit_state` returns True.
    """
    structlog.get_logger().info("state_delivered", **_state_fields(change))
