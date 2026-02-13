from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import aiohttp

from openwrt_presence.parser import PresenceEvent, parse_victorialogs_line

logger = logging.getLogger(__name__)

QUERY = '_msg:~"AP-STA-(CONNECTED|DISCONNECTED)" AND tags.appname:"hostapd"'


class VictoriaLogsSource:
    """Source adapter that reads presence events from VictoriaLogs."""

    def __init__(self, url: str) -> None:
        self._url = url.rstrip("/")

    async def backfill(self, hours: int = 4) -> AsyncIterator[PresenceEvent]:
        """Query recent logs to reconstruct state."""
        url = f"{self._url}/select/logsql/query"
        params = {"query": QUERY, "start": f"-{hours}h"}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                body = await response.text()

        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            event = parse_victorialogs_line(line)
            if event is not None:
                yield event

    async def tail(self) -> AsyncIterator[PresenceEvent]:
        """Stream live events. Auto-reconnects on failure."""
        url = f"{self._url}/select/logsql/tail"
        params = {"query": QUERY}
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)

        while True:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, params=params) as response:
                        buffer = b""
                        async for chunk in response.content:
                            buffer += chunk
                            while b"\n" in buffer:
                                raw_line, buffer = buffer.split(b"\n", 1)
                                line = raw_line.decode().strip()
                                if not line:
                                    continue
                                event = parse_victorialogs_line(line)
                                if event is not None:
                                    yield event
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.warning(
                    "VictoriaLogs tail connection error: %s. "
                    "Reconnecting in 5 seconds...",
                    exc,
                )
                await asyncio.sleep(5)
