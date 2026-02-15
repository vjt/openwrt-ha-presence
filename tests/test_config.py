import pytest
from openwrt_presence.config import Config, ConfigError


def _base_config(**overrides):
    """Return a valid config dict, with optional overrides."""
    cfg = {
        "source": {"type": "prometheus", "url": "http://localhost:9090"},
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

    def test_tracked_macs_returns_uppercase(self, sample_config: Config):
        macs = sample_config.tracked_macs
        assert isinstance(macs, set)
        assert "AA:BB:CC:DD:EE:01" in macs
        assert "AA:BB:CC:DD:EE:02" in macs
        assert "AA:BB:CC:DD:EE:03" in macs
        assert len(macs) == 3
        # All uppercase
        for mac in macs:
            assert mac == mac.upper()


class TestConfigValidation:
    def test_rejects_duplicate_mac_across_people(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict(_base_config(people={
                "alice": {"macs": ["aa:bb:cc:dd:ee:01"]},
                "bob": {"macs": ["aa:bb:cc:dd:ee:01"]},
            }))

    def test_rejects_unknown_source_type(self):
        with pytest.raises(ConfigError, match="source"):
            Config.from_dict(_base_config(source={"type": "nosql_blockchain"}))

    def test_rejects_missing_people(self):
        with pytest.raises(ConfigError):
            Config.from_dict(_base_config(people={}))

    def test_rejects_missing_nodes(self):
        with pytest.raises(ConfigError):
            Config.from_dict(_base_config(nodes={}))
