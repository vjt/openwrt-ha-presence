from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator

from openwrt_presence.parser import PresenceEvent, parse_hostapd_message

logger = logging.getLogger(__name__)

# RFC3164 syslog format from OpenWrt:
#   <PRI>Mmm dd HH:MM:SS hostname program[PID]: message
#   <PRI>Mmm dd HH:MM:SS hostname program: message
# The date field may have a single-digit day padded with a space (e.g. "Feb  3").
_RFC3164_RE = re.compile(
    r"^<\d+>"                          # priority
    r"\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+"  # timestamp
    r"(\S+)\s+"                        # (1) hostname
    r"(\w+)(?:\[\d+\])?:\s+"          # (2) program name, optional [PID]
    r"(.+)$"                           # (3) message body
)


def parse_rfc3164(raw: str) -> tuple[str, str, str] | None:
    """Parse an RFC3164 syslog line.

    Returns ``(hostname, program, message)`` or ``None`` if the line
    doesn't match the expected format.
    """
    match = _RFC3164_RE.match(raw)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


class _SyslogProtocol(asyncio.DatagramProtocol):
    """UDP datagram protocol that parses syslog messages and enqueues events."""

    def __init__(self, queue: asyncio.Queue[PresenceEvent]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        text = data.decode("utf-8", errors="replace")
        parsed = parse_rfc3164(text)
        if parsed is None:
            return

        hostname, program, message = parsed
        if program != "hostapd":
            return

        event = parse_hostapd_message(message, hostname)
        if event is not None:
            self._queue.put_nowait(event)


class SyslogSource:
    """Source adapter that listens for UDP syslog messages from OpenWrt APs."""

    def __init__(self, listen: str) -> None:
        host, port_str = listen.rsplit(":", 1)
        self._host = host
        self._port = int(port_str)
        self._queue: asyncio.Queue[PresenceEvent] = asyncio.Queue()
        self._transport: asyncio.DatagramTransport | None = None
        self._stopped = False

    def _make_protocol(self) -> _SyslogProtocol:
        """Create a protocol instance (exposed for testing)."""
        return _SyslogProtocol(self._queue)

    async def tail(self) -> AsyncIterator[PresenceEvent]:
        """Listen for UDP syslog datagrams and yield PresenceEvents.

        Creates a UDP transport on the configured address and yields
        events as they arrive. Call :meth:`stop` to close the transport
        and terminate the iterator.
        """
        loop = asyncio.get_event_loop()
        transport, _ = await loop.create_datagram_endpoint(
            self._make_protocol,
            local_addr=(self._host, self._port),
        )
        self._transport = transport
        self._stopped = False

        try:
            while not self._stopped:
                try:
                    event = await asyncio.wait_for(
                        self._queue.get(), timeout=0.5
                    )
                    yield event
                except asyncio.TimeoutError:
                    continue
        finally:
            if self._transport is not None:
                self._transport.close()

    async def stop(self) -> None:
        """Stop the syslog listener and close the transport."""
        self._stopped = True
        if self._transport is not None:
            self._transport.close()
            self._transport = None
