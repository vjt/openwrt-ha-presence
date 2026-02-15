import pytest

from openwrt_presence.config import Config


@pytest.fixture
def sample_config() -> Config:
    return Config.from_dict({
        "source": {"type": "prometheus", "url": "http://localhost:9090"},
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "topic_prefix": "openwrt-presence",
        },
        "nodes": {
            "albert": {"room": "bedroom"},
            "pingu": {"room": "office"},
            "mowgli": {"room": "garden"},
        },
        "departure_timeout": 120,
        "people": {
            "alice": {"macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]},
            "bob": {"macs": ["aa:bb:cc:dd:ee:03"]},
        },
    })
