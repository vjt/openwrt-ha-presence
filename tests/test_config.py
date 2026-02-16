import pytest
from openwrt_presence.config import Config, ConfigError


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
        bedroom = sample_config.nodes["albert"]
        assert bedroom.room == "bedroom"
        office = sample_config.nodes["pingu"]
        assert office.room == "office"

    def test_person_mac_lookup(self, sample_config: Config):
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:01") == "alice"
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:02") == "alice"
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:03") == "bob"
        assert sample_config.mac_to_person("ff:ff:ff:ff:ff:ff") is None

    def test_mac_lookup_is_case_insensitive(self, sample_config: Config):
        assert sample_config.mac_to_person("AA:BB:CC:DD:EE:01") == "alice"

    def test_mac_lookup_normalizes_separators(self, sample_config: Config):
        assert sample_config.mac_to_person("AA-BB-CC-DD-EE-01") == "alice"

    def test_exporter_port_default(self, sample_config: Config):
        assert sample_config.exporter_port == 9100

    def test_exporter_port_custom(self):
        cfg = Config.from_dict(_base_config(exporter_port=9200))
        assert cfg.exporter_port == 9200

    def test_node_url_default(self, sample_config: Config):
        assert sample_config.nodes["pingu"].url is None

    def test_node_url_override(self):
        cfg = Config.from_dict(_base_config(nodes={
            "ap1": {"room": "room1", "url": "http://192.168.1.10:9100/metrics"},
        }))
        assert cfg.nodes["ap1"].url == "http://192.168.1.10:9100/metrics"

    def test_node_urls_property(self):
        cfg = Config.from_dict(_base_config(
            nodes={
                "ap1": {"room": "room1"},
                "ap2": {"room": "room2", "url": "http://10.0.0.5:9200/metrics"},
            },
            exporter_port=9100,
        ))
        urls = cfg.node_urls
        assert urls["ap1"] == "http://ap1:9100/metrics"
        assert urls["ap2"] == "http://10.0.0.5:9200/metrics"


class TestConfigValidation:
    def test_rejects_duplicate_mac_across_people(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict(_base_config(people={
                "alice": {"macs": ["aa:bb:cc:dd:ee:01"]},
                "bob": {"macs": ["aa:bb:cc:dd:ee:01"]},
            }))

    def test_rejects_missing_people(self):
        with pytest.raises(ConfigError):
            Config.from_dict(_base_config(people={}))

    def test_rejects_missing_nodes(self):
        with pytest.raises(ConfigError):
            Config.from_dict(_base_config(nodes={}))


class TestExitNodes:
    def test_node_exit_defaults_to_false(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.nodes["ap1"].exit is False

    def test_node_exit_true(self):
        cfg = Config.from_dict(_base_config(nodes={
            "ap1": {"room": "garden", "exit": True},
        }))
        assert cfg.nodes["ap1"].exit is True

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
        cfg = Config.from_dict(_base_config(nodes={
            "ap1": {"room": "garden", "exit": True},
            "ap2": {"room": "office"},
        }))
        assert cfg.has_exit_nodes is True

    def test_timeout_for_node_no_exit_nodes(self):
        cfg = Config.from_dict(_base_config())
        assert cfg.timeout_for_node("ap1") == 120  # departure_timeout for all

    def test_timeout_for_node_with_exit_nodes(self):
        cfg = Config.from_dict(_base_config(nodes={
            "gate": {"room": "garden", "exit": True},
            "office": {"room": "office"},
        }))
        assert cfg.timeout_for_node("gate") == 120       # departure_timeout
        assert cfg.timeout_for_node("office") == 64800    # away_timeout
        assert cfg.timeout_for_node("unknown") == 64800   # unknown → interior default