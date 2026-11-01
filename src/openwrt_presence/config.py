from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import yaml

from openwrt_presence.domain import Mac, NodeName, PersonName, Room

if TYPE_CHECKING:
    from pathlib import Path

# Default values — single source of truth for dataclass defaults AND
# from_dict fallbacks. Operators override via config.yaml; omitting the
# key keeps the default.
DEFAULT_AWAY_TIMEOUT_SEC: Final[int] = 64800  # 18h — phone Wi-Fi doze safety net
DEFAULT_POLL_INTERVAL_SEC: Final[int] = 30  # /metrics scrape cadence
DEFAULT_EXPORTER_PORT: Final[int] = 9100  # prometheus-node-exporter
DEFAULT_DNS_CACHE_TTL_SEC: Final[int] = 300  # 5m — resolver reuse
DEFAULT_EVENT_HOLD_SEC: Final[int] = 10  # scrape can't undo an AP event this soon
MIN_TOKEN_LEN: Final[int] = 32


class ConfigError(Exception):
    """Raised when configuration is invalid."""


@dataclass(frozen=True)
class NodeConfig:
    room: Room
    url: str | None = None
    exit: bool = False
    # Bearer token the AP presents when pushing events. None = this AP
    # is scrape-only.  Kept out of repr so a logged config never leaks it.
    token: str | None = field(default=None, repr=False)


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
    away_timeout: int = DEFAULT_AWAY_TIMEOUT_SEC
    poll_interval: int = DEFAULT_POLL_INTERVAL_SEC
    exporter_port: int = DEFAULT_EXPORTER_PORT
    dns_cache_ttl: int = DEFAULT_DNS_CACHE_TTL_SEC
    event_port: int | None = None
    event_hold: int = DEFAULT_EVENT_HOLD_SEC
    _mac_lookup: dict[Mac, PersonName] = field(
        default_factory=lambda: {}, repr=False, compare=False
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
        if "mqtt" not in data:
            raise ConfigError("mqtt section is required")
        mqtt_raw = data["mqtt"]
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
                token=cls._parse_token(name, ndata.get("token")),
            )
        tokens = [n.token for n in nodes.values() if n.token is not None]
        if len(set(tokens)) != len(tokens):
            raise ConfigError("node tokens must be unique — one per AP")

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
        if "departure_timeout" not in data:
            raise ConfigError("departure_timeout is required (typical: 120 seconds)")
        departure_timeout: int = data["departure_timeout"]

        # --- away_timeout ---
        away_timeout: int = data.get("away_timeout", DEFAULT_AWAY_TIMEOUT_SEC)

        # --- poll_interval ---
        poll_interval: int = data.get("poll_interval", DEFAULT_POLL_INTERVAL_SEC)

        # --- exporter_port ---
        exporter_port: int = data.get("exporter_port", DEFAULT_EXPORTER_PORT)

        # --- dns_cache_ttl ---
        dns_cache_ttl: int = data.get("dns_cache_ttl", DEFAULT_DNS_CACHE_TTL_SEC)

        # --- AP push events ---
        event_port, event_hold = cls._parse_events(data, has_tokens=bool(tokens))

        return cls(
            mqtt=mqtt,
            nodes=nodes,
            people=people,
            departure_timeout=departure_timeout,
            away_timeout=away_timeout,
            poll_interval=poll_interval,
            exporter_port=exporter_port,
            dns_cache_ttl=dns_cache_ttl,
            event_port=event_port,
            event_hold=event_hold,
            _mac_lookup=mac_lookup,
        )

    @staticmethod
    def _parse_events(
        data: dict[str, Any], *, has_tokens: bool
    ) -> tuple[int | None, int]:
        """Validate ``event_port`` / ``event_hold``; return them.

        Raises :class:`ConfigError` on a bad port or hold, a port with no
        tokens, or tokens with no port (either way the config is dead).
        """
        event_port: int | None = data.get("event_port")
        if event_port is not None and (
            type(event_port) is not int or not 1 <= event_port <= 65535
        ):
            raise ConfigError(
                f"event_port must be an int in 1..65535, got {event_port!r}"
            )
        if event_port is not None and not has_tokens:
            raise ConfigError(
                "event_port is set but no node has a token — no AP could push"
            )
        if event_port is None and has_tokens:
            raise ConfigError(
                "nodes have a token but event_port is not set — tokens would be dead"
            )
        event_hold: int = data.get("event_hold", DEFAULT_EVENT_HOLD_SEC)
        if type(event_hold) is not int or event_hold <= 0:
            raise ConfigError(f"event_hold must be a positive int, got {event_hold!r}")
        return event_port, event_hold

    @staticmethod
    def _parse_token(node: str, raw: object) -> str | None:
        """Validate a node's push token; ``None`` means scrape-only.

        Raises :class:`ConfigError` if present but not a string of at
        least :data:`MIN_TOKEN_LEN` characters.
        """
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) < MIN_TOKEN_LEN:
            raise ConfigError(
                f"nodes.{node}.token must be a string of at least "
                f"{MIN_TOKEN_LEN} chars (e.g. `openssl rand -hex 32`)"
            )
        return raw

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
