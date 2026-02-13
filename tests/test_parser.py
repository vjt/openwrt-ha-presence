import json
from openwrt_presence.parser import parse_hostapd_message, parse_victorialogs_line, PresenceEvent


class TestParseHostapdMessage:
    def test_parses_sta_connected_open(self):
        msg = "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        event = parse_hostapd_message(msg, "ap-kitchen")
        assert event is not None
        assert event.event == "connect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"
        assert event.node == "ap-kitchen"

    def test_parses_sta_connected_ft(self):
        msg = "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=ft"
        event = parse_hostapd_message(msg, "ap-garden")
        assert event is not None
        assert event.event == "connect"

    def test_parses_sta_disconnected(self):
        msg = "phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:f0"
        event = parse_hostapd_message(msg, "ap-office")
        assert event is not None
        assert event.event == "disconnect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"

    def test_parses_phy0_interface(self):
        msg = "phy0-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        event = parse_hostapd_message(msg, "ap-kitchen")
        assert event is not None
        assert event.event == "connect"

    def test_ignores_irrelevant_messages(self):
        irrelevant = [
            "phy1-ap0: STA aa:bb:cc:dd:ee:f0 WPA: pairwise key handshake completed (RSN)",
            "phy1-ap0: STA aa:bb:cc:dd:ee:f0 IEEE 802.11: authenticated",
            "nl80211: kernel reports: key addition failed",
            "phy1-ap0: STA aa:bb:cc:dd:ee:f0 IEEE 802.11: associated (aid 3)",
        ]
        for msg in irrelevant:
            assert parse_hostapd_message(msg, "ap-kitchen") is None

    def test_normalizes_mac_to_lowercase(self):
        msg = "phy1-ap0: AP-STA-CONNECTED AA:BB:CC:DD:EE:F0 auth_alg=open"
        event = parse_hostapd_message(msg, "ap-kitchen")
        assert event is not None
        assert event.mac == "aa:bb:cc:dd:ee:f0"


class TestParseVictoriaLogsLine:
    def test_parses_connected_event(self):
        line = json.dumps({
            "_time": "2026-02-12T23:23:23Z",
            "_msg": "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=ft",
            "tags.hostname": "ap-garden",
        })
        event = parse_victorialogs_line(line)
        assert event is not None
        assert event.event == "connect"
        assert event.node == "ap-garden"
        assert event.mac == "aa:bb:cc:dd:ee:f0"

    def test_parses_timestamp(self):
        line = json.dumps({
            "_time": "2026-02-12T23:23:23Z",
            "_msg": "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=ft",
            "tags.hostname": "ap-garden",
        })
        event = parse_victorialogs_line(line)
        assert event is not None
        assert event.timestamp.year == 2026
        assert event.timestamp.month == 2

    def test_returns_none_for_irrelevant_message(self):
        line = json.dumps({
            "_time": "2026-02-12T23:23:23Z",
            "_msg": "phy1-ap0: STA aa:bb:cc:dd:ee:f0 WPA: start authentication",
            "tags.hostname": "ap-garden",
        })
        assert parse_victorialogs_line(line) is None

    def test_returns_none_for_malformed_json(self):
        assert parse_victorialogs_line("not json at all") is None

    def test_returns_none_for_missing_fields(self):
        line = json.dumps({"_msg": "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=ft"})
        assert parse_victorialogs_line(line) is None
