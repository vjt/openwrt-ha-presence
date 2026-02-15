"""Structured JSON logging for the presence detection service."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openwrt_presence.engine import StateChange


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }

        # Merge any extra fields that were passed via the `extra` kwarg.
        # Standard LogRecord attributes are excluded so only user-supplied
        # extras appear in the JSON output.
        _standard = set(logging.LogRecord("", 0, "", 0, None, None, None).__dict__)
        for key, value in record.__dict__.items():
            if key not in _standard and key not in obj:
                obj[key] = value

        return json.dumps(obj, default=str)


def setup_logging(
    *,
    handler: logging.Handler | None = None,
    level: int = logging.INFO,
) -> None:
    """Configure the root logger with a JSON formatter.

    Parameters
    ----------
    handler:
        A logging handler to attach. When *None* (the default) a
        ``StreamHandler`` writing to ``stderr`` is used.
    level:
        The log level to set on the root logger.
    """
    logger = logging.getLogger()
    logger.setLevel(level)

    if handler is None:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(_JSONFormatter())
    logger.addHandler(handler)


def log_state_change(change: StateChange) -> None:
    """Log a presence state change as a structured JSON line."""
    logging.info(
        "state_change",
        extra={
            "person": change.person,
            "event": "home" if change.home else "away",
            "room": change.room,
            "mac": change.mac,
            "node": change.node,
            "rssi": change.rssi,
            "event_ts": change.timestamp.isoformat(),
        },
    )
