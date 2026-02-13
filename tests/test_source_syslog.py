from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from openwrt_presence.parser import PresenceEvent
from openwrt_presence.sources.syslog import SyslogSource, parse_rfc3164


# --- Unit tests for RFC3164 parsing ---

class TestParseRfc3164:
    def test_parses_hostapd_connect_message(self):
        raw = (
            "<134>Feb 12 23:23:23 mowgli hostapd: "
            "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        )
        result = parse_rfc3164(raw)
        assert result is not None
        hostname, program, message = result
        assert hostname == "mowgli"
        assert program == "hostapd"
        assert message == "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"

    def test_parses_hostapd_disconnect_message(self):
        raw = (
            "<134>Feb 12 23:23:23 mowgli hostapd: "
            "phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:f0"
        )
        result = parse_rfc3164(raw)
        assert result is not None
        hostname, program, message = result
        assert hostname == "mowgli"
        assert program == "hostapd"
        assert message == "phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:f0"

    def test_parses_different_hostname(self):
        raw = (
            "<134>Jan  5 01:02:03 bagheera hostapd: "
            "phy0-ap0: AP-STA-CONNECTED 11:22:33:44:55:66 auth_alg=ft"
        )
        result = parse_rfc3164(raw)
        assert result is not None
        hostname, program, message = result
        assert hostname == "bagheera"
        assert program == "hostapd"

    def test_parses_dropbear_message(self):
        raw = "<86>Feb 12 10:00:00 mowgli dropbear[1234]: Exit (root): Disconnect"
        result = parse_rfc3164(raw)
        assert result is not None
        hostname, program, message = result
        assert hostname == "mowgli"
        assert program == "dropbear"

    def test_parses_kernel_message(self):
        raw = "<6>Feb 12 10:00:00 mowgli kernel: [12345.678] some kernel log"
        result = parse_rfc3164(raw)
        assert result is not None
        _, program, _ = result
        assert program == "kernel"

    def test_returns_none_for_garbage(self):
        assert parse_rfc3164("this is not syslog") is None

    def test_returns_none_for_empty(self):
        assert parse_rfc3164("") is None

    def test_handles_single_digit_day_with_leading_space(self):
        raw = "<134>Feb  3 23:23:23 mowgli hostapd: phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        result = parse_rfc3164(raw)
        assert result is not None
        hostname, program, message = result
        assert hostname == "mowgli"
        assert program == "hostapd"

    def test_program_with_pid(self):
        raw = "<134>Feb 12 23:23:23 mowgli hostapd[456]: phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        result = parse_rfc3164(raw)
        assert result is not None
        hostname, program, message = result
        assert hostname == "mowgli"
        assert program == "hostapd"
        assert message == "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"


# --- Tests for the protocol / datagram handling ---

class TestSyslogProtocol:
    @pytest.fixture
    def source(self) -> SyslogSource:
        return SyslogSource("0.0.0.0:5140")

    def test_hostapd_datagram_enqueues_event(self, source: SyslogSource):
        """A valid hostapd connect datagram should be parsed and enqueued."""
        data = (
            b"<134>Feb 12 23:23:23 mowgli hostapd: "
            b"phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
        )
        addr = ("192.168.1.1", 12345)
        protocol = source._make_protocol()
        protocol.datagram_received(data, addr)

        assert not source._queue.empty()
        event = source._queue.get_nowait()
        assert isinstance(event, PresenceEvent)
        assert event.event == "connect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"
        assert event.node == "mowgli"

    def test_disconnect_datagram_enqueues_event(self, source: SyslogSource):
        data = (
            b"<134>Feb 12 23:23:23 mowgli hostapd: "
            b"phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:f0"
        )
        addr = ("192.168.1.1", 12345)
        protocol = source._make_protocol()
        protocol.datagram_received(data, addr)

        assert not source._queue.empty()
        event = source._queue.get_nowait()
        assert event.event == "disconnect"
        assert event.mac == "aa:bb:cc:dd:ee:f0"
        assert event.node == "mowgli"

    def test_non_hostapd_datagram_is_ignored(self, source: SyslogSource):
        """Messages from other programs should not produce events."""
        data = b"<86>Feb 12 10:00:00 mowgli dropbear[1234]: Exit (root): Disconnect"
        addr = ("192.168.1.1", 12345)
        protocol = source._make_protocol()
        protocol.datagram_received(data, addr)

        assert source._queue.empty()

    def test_kernel_datagram_is_ignored(self, source: SyslogSource):
        data = b"<6>Feb 12 10:00:00 mowgli kernel: [12345.678] some kernel log"
        addr = ("192.168.1.1", 12345)
        protocol = source._make_protocol()
        protocol.datagram_received(data, addr)

        assert source._queue.empty()

    def test_irrelevant_hostapd_message_is_ignored(self, source: SyslogSource):
        """hostapd messages that are not connect/disconnect should be ignored."""
        data = (
            b"<134>Feb 12 23:23:23 mowgli hostapd: "
            b"phy1-ap0: STA aa:bb:cc:dd:ee:f0 WPA: pairwise key handshake completed (RSN)"
        )
        addr = ("192.168.1.1", 12345)
        protocol = source._make_protocol()
        protocol.datagram_received(data, addr)

        assert source._queue.empty()

    def test_garbage_datagram_is_ignored(self, source: SyslogSource):
        data = b"this is not syslog at all"
        addr = ("192.168.1.1", 12345)
        protocol = source._make_protocol()
        protocol.datagram_received(data, addr)

        assert source._queue.empty()

    def test_multiple_datagrams_in_sequence(self, source: SyslogSource):
        """Multiple valid datagrams should all be enqueued in order."""
        protocol = source._make_protocol()
        addr = ("192.168.1.1", 12345)

        messages = [
            (
                b"<134>Feb 12 23:23:23 mowgli hostapd: "
                b"phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open"
            ),
            (
                b"<134>Feb 12 23:23:24 bagheera hostapd: "
                b"phy0-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:02"
            ),
            (
                b"<134>Feb 12 23:23:25 mowgli hostapd: "
                b"phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:03 auth_alg=ft"
            ),
        ]

        for msg in messages:
            protocol.datagram_received(msg, addr)

        events = []
        while not source._queue.empty():
            events.append(source._queue.get_nowait())

        assert len(events) == 3
        assert events[0].event == "connect"
        assert events[0].mac == "aa:bb:cc:dd:ee:01"
        assert events[0].node == "mowgli"
        assert events[1].event == "disconnect"
        assert events[1].mac == "aa:bb:cc:dd:ee:02"
        assert events[1].node == "bagheera"
        assert events[2].event == "connect"
        assert events[2].mac == "aa:bb:cc:dd:ee:03"
        assert events[2].node == "mowgli"


# --- Tests for tail() async iterator ---

class TestTail:
    async def test_tail_yields_events_from_queue(self):
        """tail() should yield PresenceEvent objects placed in the queue."""
        source = SyslogSource("0.0.0.0:5140")

        # We'll mock the event loop to avoid actually binding a UDP socket.
        mock_transport = MagicMock()

        async def fake_create_datagram_endpoint(protocol_factory, local_addr):
            protocol = protocol_factory()
            protocol.connection_made(mock_transport)
            return mock_transport, protocol

        loop = asyncio.get_event_loop()
        original = loop.create_datagram_endpoint
        loop.create_datagram_endpoint = fake_create_datagram_endpoint

        try:
            # Feed some datagrams via the protocol after tail() starts
            async def feed_datagrams():
                # Give tail() a moment to start
                await asyncio.sleep(0.01)
                data1 = (
                    b"<134>Feb 12 23:23:23 mowgli hostapd: "
                    b"phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open"
                )
                data2 = (
                    b"<134>Feb 12 23:23:24 bagheera hostapd: "
                    b"phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:f1"
                )
                # Push events directly into the queue since the protocol
                # was created by tail()
                from openwrt_presence.sources.syslog import parse_rfc3164
                from openwrt_presence.parser import parse_hostapd_message

                for raw in [data1, data2]:
                    text = raw.decode("utf-8", errors="replace")
                    parsed = parse_rfc3164(text)
                    if parsed:
                        hostname, program, message = parsed
                        if program == "hostapd":
                            ev = parse_hostapd_message(message, hostname)
                            if ev:
                                await source._queue.put(ev)

            feeder = asyncio.create_task(feed_datagrams())

            events: list[PresenceEvent] = []
            async for event in source.tail():
                events.append(event)
                if len(events) >= 2:
                    await source.stop()
                    break

            await feeder

            assert len(events) == 2
            assert events[0].event == "connect"
            assert events[0].mac == "aa:bb:cc:dd:ee:f0"
            assert events[0].node == "mowgli"
            assert events[1].event == "disconnect"
            assert events[1].mac == "aa:bb:cc:dd:ee:f1"
            assert events[1].node == "bagheera"
        finally:
            loop.create_datagram_endpoint = original

    async def test_tail_stops_when_transport_closed(self):
        """After stop() is called, tail() should terminate."""
        source = SyslogSource("0.0.0.0:5140")

        mock_transport = MagicMock()

        async def fake_create_datagram_endpoint(protocol_factory, local_addr):
            protocol = protocol_factory()
            protocol.connection_made(mock_transport)
            return mock_transport, protocol

        loop = asyncio.get_event_loop()
        original = loop.create_datagram_endpoint
        loop.create_datagram_endpoint = fake_create_datagram_endpoint

        try:
            async def stop_soon():
                await asyncio.sleep(0.05)
                await source.stop()

            stopper = asyncio.create_task(stop_soon())

            events: list[PresenceEvent] = []
            async for event in source.tail():
                events.append(event)

            await stopper

            # No events were fed, so we should get none
            assert len(events) == 0
            mock_transport.close.assert_called_once()
        finally:
            loop.create_datagram_endpoint = original


# --- Tests for address parsing ---

class TestAddressParsing:
    def test_parses_address_and_port(self):
        source = SyslogSource("0.0.0.0:514")
        assert source._host == "0.0.0.0"
        assert source._port == 514

    def test_parses_localhost(self):
        source = SyslogSource("127.0.0.1:5140")
        assert source._host == "127.0.0.1"
        assert source._port == 5140
