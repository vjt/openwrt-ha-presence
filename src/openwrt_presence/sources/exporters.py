"""Source adapter that scrapes prometheus-node-exporter-lua on each AP."""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from openwrt_presence.engine import StationReading

logger = logging.getLogger(__name__)

_METRIC_RE = re.compile(
    r'^wifi_station_signal_dbm\{[^}]*mac="([^"]+)"[^}]*\}\s+(-?\d+(?:\.\d+)?)\s*$',
    re.MULTILINE,
)


class ExporterSource:
    """Scrapes /metrics on each AP for wifi_station_signal_dbm."""

    def __init__(self, node_urls: dict[str, str], tracked_macs: set[str]) -> None:
        self._node_urls = node_urls
        self._tracked_macs = {m.lower() for m in tracked_macs}

    async def query(self) -> list[StationReading]:
        """Scrape all APs in parallel and return station readings.

        APs that fail to respond are silently skipped (logged as warning).
        """
        readings: list[StationReading] = []
        timeout = aiohttp.ClientTimeout(total=5)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = {
                node: asyncio.create_task(self._scrape_ap(session, node, url))
                for node, url in self._node_urls.items()
            }
            for node, task in tasks.items():
                try:
                    ap_readings = await task
                    readings.extend(ap_readings)
                except Exception as exc:
                    logger.warning("Failed to scrape %s: %s", node, exc)

        return self._filter_tracked(readings)

    async def _scrape_ap(
        self, session: aiohttp.ClientSession, node: str, url: str
    ) -> list[StationReading]:
        """Scrape a single AP and parse its metrics."""
        async with session.get(url) as response:
            text = await response.text()
        return self._parse_metrics(text, node)

    @staticmethod
    def _parse_metrics(text: str, ap: str) -> list[StationReading]:
        """Parse Prometheus text exposition format for wifi RSSI metrics."""
        readings: list[StationReading] = []
        for match in _METRIC_RE.finditer(text):
            mac = match.group(1).lower().replace("-", ":")
            rssi = int(float(match.group(2)))
            readings.append(StationReading(mac=mac, ap=ap, rssi=rssi))
        return readings

    def _filter_tracked(self, readings: list[StationReading]) -> list[StationReading]:
        """Keep only readings for tracked MACs."""
        return [r for r in readings if r.mac in self._tracked_macs]
