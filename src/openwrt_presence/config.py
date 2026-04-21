from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from openwrt_presence.domain import Mac, NodeName, PersonName, Room


class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclass(frozen=True)
class NodeConfig:
    room: Room
    url: str | None = None
    exit: bool = False


@dataclass(frozen=True)
class PersonConfig:
    macs: list[Mac]


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    topic_prefix: str
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    nodes: dict[NodeName, NodeConfig]
    people: dict[PersonName, PersonConfig]
    departure_timeout: int
    away_timeout: int = 64800
    poll_interval: int = 30
    exporter_port: int = 9100
    dns_cache_ttl: int = 300
    _mac_lookup: dict[Mac, PersonName] = field(
        default_factory=dict, repr=False, compare=False
    )

    @staticmethod
    def _normalize_mac(mac: str) -> Mac:
        """Lowercase and replace ``-`` with ``:``."""
        return Mac(mac.lower().replace("-", ":"))

    @property
    def node_urls(self) -> dict[NodeName, str]:
        """Return resolved metrics URLs for all nodes."""
        return {
            name: node.url or f"http://{name}:{self.exporter_port}/metrics"
            for name, node in self.nodes.items()
        }

    @property
    def has_exit_nodes(self) -> bool:
        """Return True if any node is marked as an exit node."""
        return any(n.exit for n in self.nodes.values())

    @property
    def tracked_macs(self) -> frozenset[Mac]:
        """Return the set of all MACs known across all people."""
        return frozenset(self._mac_lookup.keys())

    def timeout_for_node(self, node_name: NodeName) -> int:
        """Return the appropriate timeout for *node_name*.

        If no exit nodes are configured, all nodes use ``departure_timeout``
        (backward compatible). Otherwise exit nodes use ``departure_timeout``
        and interior nodes use ``away_timeout``.
        """
        if not self.has_exit_nodes:
            return self.departure_timeout
        node = self.nodes.get(node_name)
        if node is not None and node.exit:
            return self.departure_timeout
        return self.away_timeout

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Validate *data* and return a fully-initialised :class:`Config`."""

        # --- mqtt ---
        mqtt_raw = data.get("mqtt", {})
        mqtt = MqttConfig(
            host=mqtt_raw["host"],
            port=mqtt_raw["port"],
            topic_prefix=mqtt_raw["topic_prefix"],
            username=mqtt_raw.get("username"),
            password=mqtt_raw.get("password"),
        )

        # --- nodes ---
        nodes_raw: dict[str, Any] = data.get("nodes", {})
        if not nodes_raw:
            raise ConfigError("At least one node must be configured")
        nodes: dict[NodeName, NodeConfig] = {}
        for name, ndata in nodes_raw.items():
            nodes[NodeName(name)] = NodeConfig(
                room=Room(ndata["room"]),
                url=ndata.get("url"),
                exit=ndata.get("exit", False),
            )

        # --- people ---
        people_raw: dict[str, Any] = data.get("people", {})
        if not people_raw:
            raise ConfigError("At least one person must be configured")

        people: dict[PersonName, PersonConfig] = {}
        mac_lookup: dict[Mac, PersonName] = {}
        for person_name, pdata in people_raw.items():
            person = PersonName(person_name)
            macs = [cls._normalize_mac(m) for m in pdata["macs"]]
            for mac in macs:
                if mac in mac_lookup:
                    raise ConfigError(
                        f"MAC {mac} is a duplicate — already assigned to "
                        f"{mac_lookup[mac]!r}"
                    )
                mac_lookup[mac] = person
            people[person] = PersonConfig(macs=macs)

        # --- departure_timeout ---
        departure_timeout: int = data["departure_timeout"]

        # --- away_timeout ---
        away_timeout: int = data.get("away_timeout", 64800)

        # --- poll_interval ---
        poll_interval: int = data.get("poll_interval", 30)

        # --- exporter_port ---
        exporter_port: int = data.get("exporter_port", 9100)

        # --- dns_cache_ttl ---
        dns_cache_ttl: int = data.get("dns_cache_ttl", 300)

        return cls(
            mqtt=mqtt,
            nodes=nodes,
            people=people,
            departure_timeout=departure_timeout,
            away_timeout=away_timeout,
            poll_interval=poll_interval,
            exporter_port=exporter_port,
            dns_cache_ttl=dns_cache_ttl,
            _mac_lookup=mac_lookup,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> Config:
        """Load a YAML file and return a validated :class:`Config`."""
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return cls.from_dict(data)

    def mac_to_person(self, mac: Mac) -> PersonName | None:
        """Return the person name for *mac*, or ``None`` if unknown.

        *mac* must already be normalised (lowercase, colon-separated).
        Use :meth:`_normalize_mac` at external boundaries.
        """
        return self._mac_lookup.get(mac)
