"""Source protocol — the pluggable interface for station readings.

Kept separate from concrete implementations (ExporterSource) so that
test doubles and future source types (e.g. mock sources, file replay)
can structurally satisfy the contract without importing aiohttp.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openwrt_presence.domain import StationReading


class Source(Protocol):
    """A pluggable producer of station readings.

    Contract:
    - ``query()`` returns every currently-visible tracked station,
      best RSSI per MAC if the same station appears on multiple APs.
      Per-node failures are isolated — a partial outage returns a
      partial list, never raises. A total outage returns an empty list.
    - ``close()`` is idempotent and releases all I/O resources.
    - ``all_nodes_unhealthy`` is ``True`` only when every configured
      node has been observed unhealthy AND the source has produced at
      least one prior successful query. Fresh-boot returns ``False``
      so the startup seed is not suppressed by a "never queried yet"
      state.
    """

    async def query(self) -> list[StationReading]: ...

    async def close(self) -> None: ...

    @property
    def all_nodes_unhealthy(self) -> bool: ...
