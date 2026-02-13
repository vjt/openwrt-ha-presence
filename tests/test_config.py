import pytest
from openwrt_presence.config import Config, ConfigError


class TestConfigLoading:
    def test_loads_valid_config(self, sample_config: Config):
        assert sample_config.away_timeout == 64800
        assert len(sample_config.people) == 2
        assert len(sample_config.nodes) == 3

    def test_node_properties(self, sample_config: Config):
        garden = sample_config.nodes["ap-garden"]
        assert garden.room == "garden"
        assert garden.type == "exit"
        assert garden.timeout == 120
        office = sample_config.nodes["ap-office"]
        assert office.type == "interior"
        assert office.timeout is None

    def test_person_mac_lookup(self, sample_config: Config):
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:01") == "alice"
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:02") == "alice"
        assert sample_config.mac_to_person("aa:bb:cc:dd:ee:03") == "bob"
        assert sample_config.mac_to_person("ff:ff:ff:ff:ff:ff") is None

    def test_mac_lookup_is_case_insensitive(self, sample_config: Config):
        assert sample_config.mac_to_person("AA:BB:CC:DD:EE:01") == "alice"

    def test_mac_lookup_normalizes_separators(self, sample_config: Config):
        assert sample_config.mac_to_person("AA-BB-CC-DD-EE-01") == "alice"


class TestConfigValidation:
    def test_rejects_duplicate_mac_across_people(self):
        with pytest.raises(ConfigError, match="duplicate"):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "interior"}},
                "away_timeout": 3600,
                "people": {
                    "alice": {"macs": ["aa:bb:cc:dd:ee:01"]},
                    "bob": {"macs": ["aa:bb:cc:dd:ee:01"]},
                },
            })

    def test_rejects_exit_node_without_timeout(self):
        with pytest.raises(ConfigError, match="timeout"):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "exit"}},
                "away_timeout": 3600,
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
            })

    def test_rejects_unknown_source_type(self):
        with pytest.raises(ConfigError, match="source"):
            Config.from_dict({
                "source": {"type": "nosql_blockchain"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "interior"}},
                "away_timeout": 3600,
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
            })

    def test_rejects_missing_people(self):
        with pytest.raises(ConfigError):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {"ap1": {"room": "room1", "type": "interior"}},
                "away_timeout": 3600,
                "people": {},
            })

    def test_rejects_missing_nodes(self):
        with pytest.raises(ConfigError):
            Config.from_dict({
                "source": {"type": "victorialogs", "url": "http://localhost:9428"},
                "mqtt": {"host": "localhost", "port": 1883, "topic_prefix": "test"},
                "nodes": {},
                "away_timeout": 3600,
                "people": {"alice": {"macs": ["aa:bb:cc:dd:ee:01"]}},
            })
