"""Structured JSON logging setup — structlog → JSON to stderr.

Audit-log helpers live in :mod:`openwrt_presence.audit`.
"""

from __future__ import annotations

import sys
from typing import IO, Any

import structlog


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
