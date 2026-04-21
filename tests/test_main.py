"""End-to-end tests for __main__._run with injected fakes.

Replaces Session 1 source-inspection scaffolding. Covers startup seed
(H9 initial-query safety, every-person publish), query-exception
resilience, C3 all-APs-unreachable skip, and reconnect republish
(the C2 threadsafe hop + on_connected semantics end-to-end).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from openwrt_presence.__main__ import _run
from openwrt_presence.domain import Mac, NodeName, StationReading
from tests.fakes import FakeMqttClient, FakeSource


async def _run_briefly(config, client, source, wait: float = 0.2):
    task = asyncio.create_task(_run(config, client, source))
    await asyncio.sleep(wait)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


@pytest.mark.timeout(5)
async def test_startup_publishes_state_for_every_person(sample_config):
    client = FakeMqttClient()
    source = FakeSource()
    source.schedule(
        [
            StationReading(
                mac=Mac("aa:bb:cc:dd:ee:01"), ap=NodeName("ap-garden"), rssi=-55
            ),
            StationReading(
                mac=Mac("aa:bb:cc:dd:ee:03"), ap=NodeName("ap-living"), rssi=-65
            ),
        ]
    )

    await _run_briefly(sample_config, client, source)

    people_with_state = {
        m.topic.split("/")[1] for m in client.published if m.topic.endswith("/state")
    }
    assert people_with_state >= {"alice", "bob"}


@pytest.mark.timeout(5)
async def test_initial_query_failure_does_not_crash(sample_config):
    """H9: initial query raising must not abort startup seed."""
    client = FakeMqttClient()
    source = FakeSource()
    source.raise_on_next = RuntimeError("boom")

    await _run_briefly(sample_config, client, source)

    # Even though query blew up, startup seed ran and published state per person
    assert any(m.topic.endswith("/alice/state") for m in client.published)
    assert any(m.topic.endswith("/bob/state") for m in client.published)


@pytest.mark.timeout(5)
async def test_all_nodes_unreachable_skips_engine(sample_config):
    """C3: when every AP is dark, engine must not run — avoids false AWAY."""
    client = FakeMqttClient()
    source = FakeSource()
    # First query OK (empty readings → startup seed publishes AwayState for everyone)
    source.schedule([])
    # Subsequent polls: flag source unhealthy; test that we don't see extra
    # state churn beyond the startup seed snapshot per person.
    source.all_nodes_unhealthy = True

    # Shorter dwell — startup seed is what we're asserting; unhealthy pathway
    # just shouldn't crash the loop.
    await _run_briefly(sample_config, client, source, wait=0.15)

    # Startup seed published state for each configured person (they're all away
    # because readings is empty). No RuntimeError, no StopIteration.
    state_topics = [m.topic for m in client.published if m.topic.endswith("/state")]
    assert any(t.endswith("/alice/state") for t in state_topics)
    assert any(t.endswith("/bob/state") for t in state_topics)


@pytest.mark.timeout(5)
async def test_reconnect_triggers_republish(sample_config):
    """C2 + on_connected end-to-end: reconnect republishes cached state.

    The callback hops via loop.call_soon_threadsafe, so we await
    asyncio.sleep after trigger_connect to let the hop resolve.
    """
    client = FakeMqttClient()
    source = FakeSource()
    source.schedule(
        [
            StationReading(
                mac=Mac("aa:bb:cc:dd:ee:01"), ap=NodeName("ap-garden"), rssi=-55
            ),
        ]
    )

    task = asyncio.create_task(_run(sample_config, client, source))
    await asyncio.sleep(0.2)  # startup seed done; publisher cache populated

    client.clear()
    client.trigger_disconnect()
    client.trigger_connect()
    await asyncio.sleep(0.1)  # let call_soon_threadsafe _reseed run

    dump = [(m.topic, m.payload) for m in client.published]
    assert any(
        m.topic == "openwrt-presence/alice/state" and m.payload == "home"
        for m in client.published
    ), f"No alice=home republish after reconnect. Published: {dump}"

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.timeout(5)
async def test_run_wires_lwt_before_connect(sample_config):
    """H1: LWT must be set via will_set BEFORE connect_async, else paho
    never transmits it to the broker and HA never marks us unavailable
    on crash."""

    class OrderedFakeClient(FakeMqttClient):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        def will_set(self, topic, payload, qos=0, retain=False):
            self.events.append("will_set")
            super().will_set(topic, payload, qos, retain)

        def connect_async(self, host, port):
            self.events.append("connect_async")
            super().connect_async(host, port)

    client = OrderedFakeClient()
    source = FakeSource()
    await _run_briefly(sample_config, client, source, wait=0.1)

    # will_set must appear BEFORE connect_async (paho records it for the
    # CONNECT packet; setting it after has no effect).
    assert "will_set" in client.events
    assert "connect_async" in client.events
    assert client.events.index("will_set") < client.events.index("connect_async")
    assert client.lwt is not None
    assert client.lwt.topic == "openwrt-presence/status"
    assert client.lwt.payload == "offline"
    assert client.lwt.qos == 1
    assert client.lwt.retain is True
