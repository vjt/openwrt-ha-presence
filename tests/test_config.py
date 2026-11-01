import dataclasses

import pytest

from openwrt_presence.config import (
    DEFAULT_AWAY_TIMEOUT_SEC,
    DEFAULT_DNS_CACHE_TTL_SEC,
    DEFAULT_EVENT_HOLD_SEC,
    DEFAULT_EXPORTER_PORT,
    DEFAULT_POLL_INTERVAL_SEC,
    Config,
    ConfigError,
)
from openwrt_presence.domain import Mac, NodeName


def _base_config(**overrides):
    """Return a valid config dict, with optional overrides."""
    cfg = {
        "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
        "nodes": {"ap1": {"room": "room1"}},
        "departure_timeout": 120,
        "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
    }
    cfg.update(overrides)
    return cfg


class TestConfigLoading:
    def test_loads_valid_config(self, sample_config: Config):
        assert sample_config.departure_timeout == 120
        assert len(sample_config.people) == 2
        assert len(sample_config.nodes) == 3

    def test_node_properties(self, sample_config: Config):
        bedroom = sample_config.nodes[NodeName("ap-bedroom")]
        assert bedroom.room == "bedroom"
        office = sample_config.nodes[NodeName("ap-living")]
        assert office.room == "office"

    def test_person_mac_lookup(self, sample_config: Config):
        assert sample_config.mac_to_person(Mac("aa:bb:cc:dd:ee:01")) == "alice"
        assert sample_config.mac_to_person(Mac("aa:bb:cc:dd:ee:02")) == "alice"
        assert sample_config.mac_to_person(Mac("aa:bb:cc:dd:ee:03")) == "bob"
        assert sample_config.mac_to_person(Mac("ff:ff:ff:ff:ff:ff")) is None

    def test_normalize_mac_lowercases(self):
        assert Config._normalize_mac("AA:BB:CC:DD:EE:01") == "aa:bb:cc:dd:ee:01"

    def test_normalize_mac_replaces_dashes(self):
        assert Config._normalize_mac("AA-BB-CC-DD-EE-01") == "aa:bb:cc:dd:ee:01"

    def test_exporter_port_default(self, sample_config: Config):
        assert sample_config.exporter_port == 9100

    def test_exporter_port_custom(self):
        cfg = Config.from_dict(_base_config(exporter_port=9200))
        assert cfg.exporter_port == 9200

    def test_node_url_default(self, sample_config: Config):
        assert sample_config.nodes[NodeName("ap-living")].url is None

    def test_node_url_override(self):
        cfg = Config.from_dict(
            _base_config(
                nodes={
                    "ap1": {"room": "room1", "url": "http://192.168.1.10:9100/metrics"},
                }
            )
        )
        assert cfg.nodes[NodeName("ap1")].url == "http://192.168.1.10:9100/metrics"

    def test_node_urls_property(self):
        cfg = Config.from_dict(
            _base_config(
                nodes={
                    "ap1": {"room": "room1"},
                    "ap2": {"room": "room2", "url": "http://10.0.0.5:9200/metrics"},
                },
                exporter_port=9100,
            )
        )
        urls = cfg.node_urls
        assert urls[NodeName("ap1")] == "http://ap1:9100/metrics"
        assert urls[NodeName("ap2")] == "http://10.0.0.5:9200/metrics"


class TestConfigValidation:
    def test_rejects_duplicate_mac_across_people(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict(
                _base_config(
                    people={
                        "alice": {"macs": ["aa:bb:cc:dd:ee:01"]},
                        "bob": {"macs": ["aa:bb:cc:dd:ee:01"]},
                    }
                )
            )

    def test_rejects_missing_people(self):
        with pytest.raises(ConfigError):
            Config.from_dict(_base_config(people={}))

    def test_rejects_missing_nodes(self):
        with pytest.raises(ConfigError):
            Config.from_dict(_base_config(nodes={}))

    def test_missing_mqtt_section_raises_config_error(self):
        with pytest.raises(ConfigError, match="mqtt section is required"):
            Config.from_dict(
                {
                    "nodes": {"ap-x": {"room": "x"}},
                    "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
                    "departure_timeout": 120,
                }
            )

    def test_missing_departure_timeout_raises_config_error(self):
        with pytest.raises(ConfigError, match="departure_timeout is required"):
            Config.from_dict(
                {
                    "mqtt": {"host": "x", "port": 1883, "topic_prefix": "p"},
                    "nodes": {"ap-x": {"room": "x"}},
                    "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
                }
            )

    def test_config_defaults_reference_module_constants(self):
        cfg = Config.from_dict(
            {
                "mqtt": {"host": "x", "port": 1883, "topic_prefix": "p"},
                "nodes": {"ap-x": {"room": "x"}},
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
                "departure_timeout": 120,
            }
        )
        assert cfg.away_timeout == DEFAULT_AWAY_TIMEOUT_SEC
        assert cfg.poll_interval == DEFAULT_POLL_INTERVAL_SEC
        assert cfg.exporter_port == DEFAULT_EXPORTER_PORT
        assert cfg.dns_cache_ttl == DEFAULT_DNS_CACHE_TTL_SEC


class TestExitNodes:
    def test_node_exit_defaults_to_false(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.nodes[NodeName("ap1")].exit is False

    def test_node_exit_true(self):
        cfg = Config.from_dict(
            _base_config(
                nodes={
                    "ap1": {"room": "garden", "exit": True},
                }
            )
        )
        assert cfg.nodes[NodeName("ap1")].exit is True

    def test_away_timeout_default(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.away_timeout == 64800  # 18 hours

    def test_away_timeout_custom(self):
        cfg = Config.from_dict(_base_config(away_timeout=3600))
        assert cfg.away_timeout == 3600

    def test_has_exit_nodes_false_when_none(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.has_exit_nodes is False

    def test_has_exit_nodes_true(self):
        cfg = Config.from_dict(
            _base_config(
                nodes={
                    "ap1": {"room": "garden", "exit": True},
                    "ap2": {"room": "office"},
                }
            )
        )
        assert cfg.has_exit_nodes is True

    def test_timeout_for_node_no_exit_nodes(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.timeout_for_node(NodeName("ap1")) == 120  # departure_timeout for all

    def test_timeout_for_node_with_exit_nodes(self):
        cfg = Config.from_dict(
            _base_config(
                nodes={
                    "gate": {"room": "garden", "exit": True},
                    "office": {"room": "office"},
                }
            )
        )
        assert cfg.timeout_for_node(NodeName("gate")) == 120  # departure_timeout
        assert cfg.timeout_for_node(NodeName("office")) == 64800  # away_timeout
        assert (
            cfg.timeout_for_node(NodeName("unknown")) == 64800
        )  # unknown → interior default


class TestConfigImmutability:
    def test_config_is_frozen(self, sample_config: Config):
        with pytest.raises(dataclasses.FrozenInstanceError):
            sample_config.poll_interval = 9999  # type: ignore[misc]

    def test_config_exposes_tracked_macs(self, sample_config: Config):
        expected = frozenset(
            {
                Mac("aa:bb:cc:dd:ee:01"),
                Mac("aa:bb:cc:dd:ee:02"),
                Mac("aa:bb:cc:dd:ee:03"),
            }
        )
        assert sample_config.tracked_macs == expected


_TOKEN_A = "a" * 32
_TOKEN_B = "b" * 32


class TestEventConfig:
    def test_events_disabled_by_default(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.event_port is None
        assert cfg.nodes[NodeName("ap1")].token is None

    def test_event_port_and_token(self):
        cfg = Config.from_dict(
            _base_config(
                event_port=8099,
                nodes={"ap1": {"room": "r1", "token": _TOKEN_A}},
            )
        )
        assert cfg.event_port == 8099
        assert cfg.nodes[NodeName("ap1")].token == _TOKEN_A
        assert cfg.event_hold == DEFAULT_EVENT_HOLD_SEC

    def test_event_hold_custom(self):
        cfg = Config.from_dict(
            _base_config(
                event_port=8099,
                event_hold=5,
                nodes={"ap1": {"room": "r1", "token": _TOKEN_A}},
            )
        )
        assert cfg.event_hold == 5

    def test_token_not_in_repr(self):
        cfg = Config.from_dict(
            _base_config(
                event_port=8099,
                nodes={"ap1": {"room": "r1", "token": _TOKEN_A}},
            )
        )
        assert _TOKEN_A not in repr(cfg)

    def test_short_token_rejected(self):
        with pytest.raises(ConfigError, match="token"):
            Config.from_dict(
                _base_config(
                    event_port=8099,
                    nodes={"ap1": {"room": "r1", "token": "short"}},
                )
            )

    def test_non_string_token_rejected(self):
        with pytest.raises(ConfigError, match="token"):
            Config.from_dict(
                _base_config(
                    event_port=8099,
                    nodes={"ap1": {"room": "r1", "token": 12345}},
                )
            )

    def test_duplicate_token_rejected(self):
        with pytest.raises(ConfigError, match="token"):
            Config.from_dict(
                _base_config(
                    event_port=8099,
                    nodes={
                        "ap1": {"room": "r1", "token": _TOKEN_A},
                        "ap2": {"room": "r2", "token": _TOKEN_A},
                    },
                )
            )

    def test_tokens_without_port_rejected(self):
        with pytest.raises(ConfigError, match="event_port"):
            Config.from_dict(
                _base_config(nodes={"ap1": {"room": "r1", "token": _TOKEN_A}})
            )

    def test_port_without_tokens_rejected(self):
        with pytest.raises(ConfigError, match="token"):
            Config.from_dict(_base_config(event_port=8099))

    @pytest.mark.parametrize("port", [0, 65536, "8099", True])
    def test_bad_port_rejected(self, port):
        with pytest.raises(ConfigError, match="event_port"):
            Config.from_dict(
                _base_config(
                    event_port=port,
                    nodes={"ap1": {"room": "r1", "token": _TOKEN_A}},
                )
            )

    @pytest.mark.parametrize("hold", [0, -1, "10", True])
    def test_bad_event_hold_rejected(self, hold):
        with pytest.raises(ConfigError, match="event_hold"):
            Config.from_dict(
                _base_config(
                    event_port=8099,
                    event_hold=hold,
                    nodes={"ap1": {"room": "r1", "token": _TOKEN_A}},
                )
            )

    def test_mixed_nodes_some_without_token(self):
        cfg = Config.from_dict(
            _base_config(
                event_port=8099,
                nodes={
                    "ap1": {"room": "r1", "token": _TOKEN_A},
                    "ap2": {"room": "r2"},
                    "ap3": {"room": "r3", "token": _TOKEN_B},
                },
            )
        )
        assert cfg.nodes[NodeName("ap2")].token is None
