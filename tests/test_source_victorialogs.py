from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from openwrt_presence.parser import PresenceEvent
from openwrt_presence.sources.victorialogs import (
    QUERY,
    VictoriaLogsSource,
)


def _make_jsonl_line(
    *,
    msg: str = "phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:f0 auth_alg=open",
    hostname: str = "ap-kitchen",
    time: str = "2026-02-12T23:23:23Z",
) -> str:
    return json.dumps(
        {"_time": time, "_msg": msg, "tags.hostname": hostname}
    )


class TestQueryConstant:
    def test_builds_correct_query(self):
        assert 'AP-STA-(CONNECTED|DISCONNECTED)' in QUERY
        assert 'hostapd' in QUERY


class TestBackfill:
    @pytest.fixture
    def source(self) -> VictoriaLogsSource:
        return VictoriaLogsSource("http://logs.local:9428")

    async def test_backfill_url_construction(self, source: VictoriaLogsSource):
        """Verify the URL and query params sent to aiohttp."""
        line = _make_jsonl_line()
        response_text = line + "\n"

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=response_text)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            events = [e async for e in source.backfill(hours=4)]

        # Check the URL was called with the correct path and params
        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        url = call_args[0][0]
        params = call_args[1].get("params", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert "/select/logsql/query" in url
        assert params["query"] == QUERY
        assert params["start"] == "-4h"

    async def test_backfill_parses_response_lines(self, source: VictoriaLogsSource):
        """Mock aiohttp response with JSONL, verify PresenceEvents are yielded."""
        lines = [
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
                hostname="ap-office",
                time="2026-02-12T10:00:00Z",
            ),
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:02",
                hostname="ap-garden",
                time="2026-02-12T10:01:00Z",
            ),
        ]
        response_text = "\n".join(lines) + "\n"

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=response_text)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            events = [e async for e in source.backfill(hours=4)]

        assert len(events) == 2
        assert events[0].event == "connect"
        assert events[0].mac == "aa:bb:cc:dd:ee:01"
        assert events[0].node == "ap-office"
        assert events[1].event == "disconnect"
        assert events[1].mac == "aa:bb:cc:dd:ee:02"
        assert events[1].node == "ap-garden"

    async def test_backfill_skips_malformed_lines(self, source: VictoriaLogsSource):
        """Include a bad line in the response, verify it's skipped."""
        lines = [
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
                hostname="ap-office",
            ),
            "this is not valid json",
            '{"_msg": "irrelevant message", "_time": "2026-02-12T10:00:00Z", "tags.hostname": "ap-x"}',
            "",
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:02",
                hostname="ap-garden",
            ),
        ]
        response_text = "\n".join(lines)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=response_text)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            events = [e async for e in source.backfill(hours=2)]

        assert len(events) == 2
        assert events[0].event == "connect"
        assert events[1].event == "disconnect"

    async def test_backfill_sorts_by_timestamp(self, source: VictoriaLogsSource):
        """VictoriaLogs returns events grouped by stream (per-AP), not
        in global chronological order. Backfill must sort them."""
        lines = [
            # AP-1 events (later)
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
                hostname="ap-office",
                time="2026-02-12T10:05:00Z",
            ),
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01",
                hostname="ap-office",
                time="2026-02-12T10:10:00Z",
            ),
            # AP-2 events (earlier, but returned second by VictoriaLogs)
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
                hostname="ap-garden",
                time="2026-02-12T10:00:00Z",
            ),
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:01",
                hostname="ap-garden",
                time="2026-02-12T10:03:00Z",
            ),
        ]
        response_text = "\n".join(lines) + "\n"

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=response_text)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            events = [e async for e in source.backfill(hours=4)]

        assert len(events) == 4
        # Should be sorted chronologically, not in VictoriaLogs stream order
        assert events[0].node == "ap-garden"   # 10:00
        assert events[1].node == "ap-garden"   # 10:03
        assert events[2].node == "ap-office"   # 10:05
        assert events[3].node == "ap-office"   # 10:10
    @pytest.fixture
    def source(self) -> VictoriaLogsSource:
        return VictoriaLogsSource("http://logs.local:9428")

    async def test_tail_parses_streamed_lines(self, source: VictoriaLogsSource):
        """Mock a streaming response, verify events are yielded."""
        lines = [
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
                hostname="ap-office",
                time="2026-02-12T10:00:00Z",
            ),
            _make_jsonl_line(
                msg="phy1-ap0: AP-STA-DISCONNECTED aa:bb:cc:dd:ee:02",
                hostname="ap-garden",
                time="2026-02-12T10:01:00Z",
            ),
        ]

        # Simulate streaming: each line comes as a separate chunk with newline
        async def mock_content_iter():
            for line in lines:
                yield (line + "\n").encode()
            # Simulate stream ending (which triggers reconnect)
            return

        mock_content = MagicMock()
        mock_content.__aiter__ = lambda self: mock_content_iter()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.content = mock_content
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        events: list[PresenceEvent] = []
        with patch("aiohttp.ClientSession", return_value=mock_session):
            async for event in source.tail():
                events.append(event)
                if len(events) >= 2:
                    break

        assert len(events) == 2
        assert events[0].event == "connect"
        assert events[0].mac == "aa:bb:cc:dd:ee:01"
        assert events[1].event == "disconnect"
        assert events[1].mac == "aa:bb:cc:dd:ee:02"

    async def test_tail_url_construction(self, source: VictoriaLogsSource):
        """Verify the tail endpoint URL and query params."""
        line = _make_jsonl_line()

        async def mock_content_iter():
            yield (line + "\n").encode()

        mock_content = MagicMock()
        mock_content.__aiter__ = lambda self: mock_content_iter()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.content = mock_content
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            async for event in source.tail():
                break  # just need one event to verify the URL

        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        url = call_args[0][0]
        params = call_args[1].get("params", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert "/select/logsql/tail" in url
        assert params["query"] == QUERY

    async def test_tail_reconnects_on_error(self, source: VictoriaLogsSource):
        """Mock a connection error then success, verify reconnection."""
        line = _make_jsonl_line(
            msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
            hostname="ap-office",
        )

        async def mock_content_iter():
            yield (line + "\n").encode()

        mock_content = MagicMock()
        mock_content.__aiter__ = lambda self: mock_content_iter()

        mock_response_ok = AsyncMock()
        mock_response_ok.status = 200
        mock_response_ok.content = mock_content
        mock_response_ok.__aenter__ = AsyncMock(return_value=mock_response_ok)
        mock_response_ok.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise aiohttp.ClientError("connection refused")
            return mock_response_ok

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(side_effect=side_effect)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        events: list[PresenceEvent] = []
        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            async for event in source.tail():
                events.append(event)
                if len(events) >= 1:
                    break

        assert len(events) == 1
        assert events[0].event == "connect"
        # Verify sleep was called for retry delay
        mock_sleep.assert_called_once()
        # Verify two get calls were made (first failed, second succeeded)
        assert call_count == 2

    async def test_tail_reconnects_on_timeout(self, source: VictoriaLogsSource):
        """Mock a TimeoutError then success, verify reconnection."""
        line = _make_jsonl_line(
            msg="phy1-ap0: AP-STA-CONNECTED aa:bb:cc:dd:ee:01 auth_alg=open",
            hostname="ap-office",
        )

        async def mock_content_iter():
            yield (line + "\n").encode()

        mock_content = MagicMock()
        mock_content.__aiter__ = lambda self: mock_content_iter()

        mock_response_ok = AsyncMock()
        mock_response_ok.status = 200
        mock_response_ok.content = mock_content
        mock_response_ok.__aenter__ = AsyncMock(return_value=mock_response_ok)
        mock_response_ok.__aexit__ = AsyncMock(return_value=False)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError()
            return mock_response_ok

        mock_session = AsyncMock(spec=aiohttp.ClientSession)
        mock_session.get = MagicMock(side_effect=side_effect)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        events: list[PresenceEvent] = []
        with patch("aiohttp.ClientSession", return_value=mock_session), \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            async for event in source.tail():
                events.append(event)
                if len(events) >= 1:
                    break

        assert len(events) == 1
        mock_sleep.assert_called_once()
        assert call_count == 2
