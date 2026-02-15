"""Pretty-print openwrt-presence JSON logs with ANSI colors.

Usage::

    docker container logs eve -f 2>&1 | openwrt-monitor
    docker container logs eve -f 2>&1 | python -m openwrt_presence.monitor
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

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


def _format_state_change(data: dict) -> str:
    person = data.get("person", "?")
    event = data.get("event", "?")
    room = data.get("room") or ""
    node = data.get("node", "")
    mac = data.get("mac", "")
    rssi = data.get("rssi")
    event_ts = _parse_time(data.get("event_ts", data.get("ts", "")))

    if event == "home":
        bullet = f"{GREEN}●{RESET}"
        event_str = f"{GREEN}home{RESET}"
    else:
        bullet = f"{RED}○{RESET}"
        event_str = f"{RED}away{RESET}"

    room_str = f"  {CYAN}{room}{RESET}" if room else ""
    rssi_str = f" {rssi}dBm" if rssi is not None else ""
    detail = f"{DIM}({node}{rssi_str} / {mac}){RESET}"

    return f"{DIM}{event_ts}{RESET}  {bullet} {BOLD}{person:<10}{RESET} {event_str}{room_str}  {detail}"


def _format_log(data: dict) -> str:
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


def main() -> None:
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                print(line)
                continue

            if data.get("message") == "state_change":
                print(_format_state_change(data))
            else:
                print(_format_log(data))
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
