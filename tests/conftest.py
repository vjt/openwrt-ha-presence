import pytest

from openwrt_presence.config import Config


@pytest.fixture
def sample_config() -> Config:
    return Config.from_dict({
        "source": {"type": "victorialogs", "url": "http://localhost:9428"},
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "topic_prefix": "openwrt-presence",
        },
        "nodes": {
            "ap-garden": {"room": "garden", "type": "exit", "timeout": 120},
            "ap-office": {"room": "office", "type": "interior"},
            "ap-bedroom": {"room": "bedroom", "type": "interior"},
        },
        "away_timeout": 64800,
        "people": {
            "alice": {"macs": ["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"]},
            "bob": {"macs": ["aa:bb:cc:dd:ee:03"]},
        },
    })
