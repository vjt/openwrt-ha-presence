from __future__ import annotations

import logging

import aiohttp

from openwrt_presence.engine import StationReading

logger = logging.getLogger(__name__)


class PrometheusSource:
    """Source adapter that queries a Prometheus-compatible TSDB for RSSI metrics."""

    def __init__(self, url: str, macs: set[str]) -> None:
        self._url = url.rstrip("/")
        self._macs = macs

    def _build_query(self) -> str:
        """Build a PromQL instant query for tracked MACs."""
        mac_re = "|".join(sorted(self._macs))
        return f'wifi_station_signal_dbm{{mac=~"{mac_re}"}}'

    async def query(self) -> list[StationReading]:
        """Query the TSDB and return current station readings.

        Returns an empty list on connection errors (caller retries next cycle).
        """
        url = f"{self._url}/api/v1/query"
        params = {"query": self._build_query()}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    data = await response.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning("Prometheus query failed: %s", exc)
            return []

        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict) -> list[StationReading]:
        """Parse a Prometheus instant query response into StationReadings.

        Expected format::

            {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"mac": "AA:BB:CC:DD:EE:01", "instance": "ap1", ...},
                            "value": [1234567890, "-45"]
                        },
                        ...
                    ]
                }
            }
        """
        readings: list[StationReading] = []

        try:
            results = data["data"]["result"]
        except (KeyError, TypeError):
            logger.warning("Unexpected Prometheus response format: %s", data)
            return readings

        for entry in results:
            try:
                metric = entry["metric"]
                mac = metric["mac"].lower().replace("-", ":")
                ap = metric["instance"]
                rssi = int(float(entry["value"][1]))
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                logger.debug("Skipping malformed result entry: %s (%s)", entry, exc)
                continue

            readings.append(StationReading(mac=mac, ap=ap, rssi=rssi))

        return readings
