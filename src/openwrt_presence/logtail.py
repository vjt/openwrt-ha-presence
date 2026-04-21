"""Pretty-print openwrt-presence JSON logs with ANSI colors.

Usage::

    docker container logs eve -f 2>&1 | openwrt-presence-logtail
    docker container logs eve -f 2>&1 | python -m openwrt_presence.logtail

The schema consumed here is produced by :mod:`openwrt_presence.audit`
(``state_computed`` / ``state_delivered``) and :mod:`openwrt_presence.logging`
(structlog envelope: ``level``, ``ts``, ``message``).  :class:`AuditRecord`
types the coupling locally — producer and consumer don't share the
definition; the CLI is schema-specific and declares its own view.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Literal, NotRequired, TypedDict, cast


class AuditRecord(TypedDict):
    """Typed view of one JSON audit line.

    Base fields (``message``, ``ts``, ``level``) are the structlog
    envelope from :mod:`openwrt_presence.logging`.  The rest are
    ``NotRequired`` and populated only for ``state_computed`` /
    ``state_delivered`` lines (see :func:`openwrt_presence.audit._state_fields`).
    For AWAY transitions on never-seen persons, ``room``/``mac``/``node``/``rssi``
    are ``None``.

    Producer shape lives in :mod:`openwrt_presence.audit`; this TypedDict
    is the CLI's local view of it (unshared by design).
    """

    message: str
    ts: str
    level: str
    person: NotRequired[str]
    presence: NotRequired[Literal["home", "away"]]
    room: NotRequired[str | None]
    mac: NotRequired[str | None]
    node: NotRequired[str | None]
    rssi: NotRequired[int | None]
    event_ts: NotRequired[str]


# ANSI escape codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def _parse_time(iso: str) -> str:
    """Extract HH:MM:SS from an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso[:8] if len(iso) >= 8 else iso


def _format_state_change(data: AuditRecord) -> str:
    person = data.get("person", "?")
    event = data.get("presence", "?")
    room = data.get("room") or ""
    node = data.get("node") or ""
    mac = data.get("mac") or ""
    rssi = data.get("rssi")
    event_ts = _parse_time(data.get("event_ts") or data.get("ts", ""))

    if event == "home":
        bullet = f"{GREEN}●{RESET}"
        event_str = f"{GREEN}home{RESET}"
    else:
        bullet = f"{RED}○{RESET}"
        event_str = f"{RED}away{RESET}"

    room_str = f"  {CYAN}{room}{RESET}" if room else ""
    rssi_str = f" {rssi}dBm" if rssi is not None else ""
    detail = f"{DIM}({node}{rssi_str} / {mac}){RESET}"

    return (
        f"{DIM}{event_ts}{RESET}  {bullet} {BOLD}{person:<10}{RESET} "
        f"{event_str}{room_str}  {detail}"
    )


def _format_state_delivered(data: AuditRecord) -> str:
    person = data.get("person", "?")
    event = data.get("presence", "?")
    ts = _parse_time(data.get("ts", ""))
    mark = f"{GREEN}✓{RESET}" if event == "home" else f"{RED}✓{RESET}"
    return f"{DIM}{ts}{RESET}  {mark} {DIM}{person} {event} delivered{RESET}"


def _format_log(data: AuditRecord) -> str:
    ts = _parse_time(data.get("ts", ""))
    level = data.get("level", "INFO")
    message = data.get("message", "")

    if level == "WARNING":
        prefix = f"{YELLOW}⚠{RESET} "
    elif level == "ERROR":
        prefix = f"{RED}✗{RESET} "
    else:
        prefix = "  "

    return f"{DIM}{ts}{RESET}  {prefix}{message}"


def _parse(line: str) -> AuditRecord | None:
    """Return a typed audit record, or ``None`` if the line isn't our JSON.

    Monitor is a dev tool: malformed or foreign lines are printed raw by
    the caller, not crashed on.  ``cast`` is a bare assertion — we shape-
    check with ``isinstance(dict)`` first, then trust the producer
    contract (audit.py + logging.py).
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return cast("AuditRecord", raw)


def main() -> None:
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            data = _parse(line)
            if data is None:
                print(line)
                continue

            msg = data.get("message")
            if msg == "state_computed":
                print(_format_state_change(data))
            elif msg == "state_delivered":
                print(_format_state_delivered(data))
            else:
                print(_format_log(data))
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
