"""Source adapter that scrapes prometheus-node-exporter-lua on each AP."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import aiohttp
import structlog

from openwrt_presence.domain import Mac, NodeName, StationReading

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

_METRIC_PREFIX = "wifi_station_signal_dbm"
_METRIC_RE = re.compile(
    r'^wifi_station_signal_dbm\{[^}]*mac="([^"]+)"[^}]*\}\s+(-?\d+(?:\.\d+)?)\s*$',
)


class ExporterSource:
    """Scrapes /metrics on each AP for wifi_station_signal_dbm."""

    def __init__(
        self,
        node_urls: dict[NodeName, str],
        tracked_macs: AbstractSet[Mac],
        dns_cache_ttl: int = 300,
    ) -> None:
        self._node_urls = node_urls
        self._tracked_macs: frozenset[Mac] = frozenset(tracked_macs)
        self._dns_cache_ttl = dns_cache_ttl
        self._connector: aiohttp.TCPConnector | None = None
        self._session: aiohttp.ClientSession | None = None
        self._node_healthy: dict[NodeName, bool] = {}
        self._log: structlog.stdlib.BoundLogger = structlog.get_logger()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._connector is None or self._connector.closed:
            self._connector = aiohttp.TCPConnector(
                ttl_dns_cache=self._dns_cache_ttl,
            )
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                connector_owner=False,
                timeout=aiohttp.ClientTimeout(total=5),
            )
        return self._session

    async def close(self) -> None:
        """Close the persistent session and connector."""
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector and not self._connector.closed:
            await self._connector.close()

    async def query(self) -> list[StationReading]:
        """Scrape all APs in parallel and return station readings.

        Only logs on health transitions: when a node becomes unreachable
        or recovers.  Repeated failures for the same node are silent.
        """
        readings: list[StationReading] = []
        session = self._get_session()
        tasks = {
            node: asyncio.create_task(self._scrape_ap(session, node, url))
            for node, url in self._node_urls.items()
        }
        for node, task in tasks.items():
            first_seen = node not in self._node_healthy
            try:
                ap_readings = await task
                readings.extend(ap_readings)
                if first_seen:
                    self._log.info("initial_node_state", node=node, healthy=True)
                elif not self._node_healthy[node]:
                    self._log.info("node_recovered", node=node)
                self._node_healthy[node] = True
            except Exception as exc:
                was_healthy = self._node_healthy.get(node, True)
                self._node_healthy[node] = False
                if first_seen:
                    self._log.info("initial_node_state", node=node, healthy=False)
                if was_healthy:
                    self._log.warning(
                        "node_unreachable",
                        node=node,
                        error=type(exc).__name__,
                    )

        return self._filter_tracked(readings)

    async def _scrape_ap(
        self,
        session: aiohttp.ClientSession,
        node: NodeName,
        url: str,
    ) -> list[StationReading]:
        """Scrape a single AP and parse its metrics.

        Uses ``response.text()`` which reads until EOF — a prior attempt
        with ``response.content.read(1<<20)`` truncated at the first TCP
        chunk (StreamReader.read(n) is single-chunk on aiohttp, not "read
        until n bytes or EOF"), silently dropping every wifi metric past
        byte ~2000 on real APs.  The 5s ``ClientTimeout`` in the session
        already bounds the scrape against a pathological exporter — no
        additional body cap is needed on a trusted LAN.
        """
        async with session.get(url) as response:
            response.raise_for_status()
            text = await response.text()
        return self._parse_metrics(text, node)

    @staticmethod
    def _parse_metrics(text: str, ap: NodeName) -> list[StationReading]:
        """Parse Prometheus text exposition format for wifi RSSI metrics.

        Lines that look like our metric (start with the prefix) but fail to
        parse raise ValueError. That bubbles up to the per-AP try/except in
        query() and the node gets marked unhealthy — the operator sees
        node_unreachable and investigates. Silent skipping of garbage from
        an AP hides real bugs.
        """
        readings: list[StationReading] = []
        for line in text.splitlines():
            if not line.startswith(_METRIC_PREFIX):
                continue
            m = _METRIC_RE.match(line)
            if m is None:
                raise ValueError(f"{ap}: malformed metric line: {line!r}")
            mac = Mac(m.group(1).lower().replace("-", ":"))
            rssi = int(float(m.group(2)))
            readings.append(StationReading(mac=mac, ap=ap, rssi=rssi))
        return readings

    def _filter_tracked(self, readings: list[StationReading]) -> list[StationReading]:
        """Keep only readings for tracked MACs."""
        return [r for r in readings if r.mac in self._tracked_macs]

    @property
    def all_nodes_unhealthy(self) -> bool:
        """True iff every configured node failed its last scrape.

        Returns False before the first query (no evidence yet), and
        False when any node is currently healthy. Used by __main__._run
        as a circuit breaker: when this flips True, skip the engine
        cycle so a complete network outage can't manufacture false
        AWAY transitions (C3).
        """
        if not self._node_healthy:
            return False
        return not any(self._node_healthy.values())
