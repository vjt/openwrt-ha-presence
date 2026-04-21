"""Structured JSON logging for the presence detection service."""

from __future__ import annotations

import sys
from typing import IO, TYPE_CHECKING, Any

import structlog

from openwrt_presence.domain import AwayState, HomeState

if TYPE_CHECKING:
    from openwrt_presence.domain import StateChange


def _uppercase_level(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    if "level" in event_dict:
        event_dict["level"] = event_dict["level"].upper()
    return event_dict


def setup_logging(*, file: IO[str] | None = None) -> None:
    """Configure structlog for JSON output.

    Parameters
    ----------
    file:
        Output stream.  Defaults to *stderr*.
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            _uppercase_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.format_exc_info,
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.WriteLoggerFactory(
            file=file if file is not None else sys.stderr,
        ),
    )


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
