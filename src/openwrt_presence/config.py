from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


class ConfigError(Exception):
    """Raised when configuration is invalid."""


_VALID_SOURCE_TYPES = {"victorialogs", "syslog"}


@dataclass(frozen=True)
class NodeConfig:
    room: str
    type: Literal["exit", "interior"]
    timeout: int | None = None


@dataclass(frozen=True)
class PersonConfig:
    macs: list[str]


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    topic_prefix: str
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class SourceConfig:
    type: Literal["victorialogs", "syslog"]
    url: str | None = None
    listen: str | None = None


@dataclass
class Config:
    source: SourceConfig
    mqtt: MqttConfig
    nodes: dict[str, NodeConfig]
    people: dict[str, PersonConfig]
    away_timeout: int
    _mac_lookup: dict[str, str] = field(default_factory=dict, repr=False)

    @staticmethod
    def _normalize_mac(mac: str) -> str:
        """Lowercase and replace ``-`` with ``:``."""
        return mac.lower().replace("-", ":")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """Validate *data* and return a fully-initialised :class:`Config`."""

        # --- source ---
        src_raw = data.get("source", {})
        src_type = src_raw.get("type")
        if src_type not in _VALID_SOURCE_TYPES:
            raise ConfigError(
                f"Unknown source type {src_type!r}; "
                f"expected one of {sorted(_VALID_SOURCE_TYPES)}"
            )
        source = SourceConfig(
            type=src_type,
            url=src_raw.get("url"),
            listen=src_raw.get("listen"),
        )

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
        nodes: dict[str, NodeConfig] = {}
        for name, ndata in nodes_raw.items():
            node = NodeConfig(
                room=ndata["room"],
                type=ndata["type"],
                timeout=ndata.get("timeout"),
            )
            if node.type == "exit" and node.timeout is None:
                raise ConfigError(
                    f"Exit node {name!r} requires a timeout value"
                )
            nodes[name] = node

        # --- people ---
        people_raw: dict[str, Any] = data.get("people", {})
        if not people_raw:
            raise ConfigError("At least one person must be configured")

        people: dict[str, PersonConfig] = {}
        mac_lookup: dict[str, str] = {}
        for person_name, pdata in people_raw.items():
            macs = [cls._normalize_mac(m) for m in pdata["macs"]]
            for mac in macs:
                if mac in mac_lookup:
                    raise ConfigError(
                        f"MAC {mac} is a duplicate — already assigned to "
                        f"{mac_lookup[mac]!r}"
                    )
                mac_lookup[mac] = person_name
            people[person_name] = PersonConfig(macs=macs)

        # --- away_timeout ---
        away_timeout: int = data["away_timeout"]

        return cls(
            source=source,
            mqtt=mqtt,
            nodes=nodes,
            people=people,
            away_timeout=away_timeout,
            _mac_lookup=mac_lookup,
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> Config:
        """Load a YAML file and return a validated :class:`Config`."""
        with open(path) as fh:
            data = yaml.safe_load(fh)
        return cls.from_dict(data)

    def mac_to_person(self, mac: str) -> str | None:
        """Return the person name for *mac*, or ``None`` if unknown.

        Lookup is case-insensitive and normalises ``-`` separators to ``:``.
        """
        return self._mac_lookup.get(self._normalize_mac(mac))
