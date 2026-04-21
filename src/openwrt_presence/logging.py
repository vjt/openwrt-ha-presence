"""Structured JSON logging setup — structlog → JSON to stderr.

Audit-log helpers live in :mod:`openwrt_presence.audit`.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, TextIO

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, WrappedLogger


def _uppercase_level(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    if "level" in event_dict:
        event_dict["level"] = event_dict["level"].upper()
    return event_dict


def setup_logging(*, file: TextIO | None = None) -> None:
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
