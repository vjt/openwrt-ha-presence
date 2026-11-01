"""HTTP endpoint for AP push events (sources/events.py)."""

from __future__ import annotations

import dataclasses
import json

import aiohttp
import pytest

from openwrt_presence.config import NodeConfig
from openwrt_presence.domain import ApEvent, ApEventKind, Mac, NodeName, Room
from openwrt_presence.sources.events import make_app

TOKEN_BED = "b" * 40
TOKEN_GARDEN = "g" * 40


@pytest.fixture
def push_config(sample_config):
    nodes = dict(sample_config.nodes)
    nodes[NodeName("ap-bedroom")] = NodeConfig(room=Room("bedroom"), token=TOKEN_BED)
    nodes[NodeName("ap-garden")] = NodeConfig(
        room=Room("garden"), exit=True, token=TOKEN_GARDEN
    )
    return dataclasses.replace(sample_config, nodes=nodes, event_port=8099)


class _Client:
    """Minimal client bound to the test's own loop (see test_source_exporters)."""

    def __init__(self, base: str) -> None:
        self._base = base

    async def post(self, path, **kw):
        return await self._call("POST", path, **kw)

    async def get(self, path, **kw):
        return await self._call("GET", path, **kw)

    async def _call(self, method, path, **kw):
        async with (
            aiohttp.ClientSession() as session,
            session.request(method, self._base + path, **kw) as resp,
        ):
            await resp.read()
            return resp


async def _start(aiohttp_server, config):
    received: list[ApEvent] = []
    server = await aiohttp_server(make_app(config, received.append))
    return _Client(f"http://{server.host}:{server.port}"), received


def _body(**over):
    b = {
        "node": "ap-bedroom",
        "mac": "aa:bb:cc:dd:ee:01",
        "event": "assoc",
        "rssi": -61,
    }
    b.update(over)
    return json.dumps(b)


def _auth(token=TOKEN_BED):
    return {"Authorization": f"Bearer {token}"}


async def test_tracked_assoc_accepted(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=_body(), headers=_auth())
    assert resp.status == 202
    assert received == [
        ApEvent(
            node=NodeName("ap-bedroom"),
            mac=Mac("aa:bb:cc:dd:ee:01"),
            kind=ApEventKind.ASSOC,
            rssi=-61,
        )
    ]


async def test_untracked_mac_accepted_same_status(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post(
        "/event", data=_body(mac="ff:ff:ff:ff:ff:ff"), headers=_auth()
    )
    assert resp.status == 202
    # Forwarded: the engine, not the endpoint, decides untracked = no-op.
    assert [e.mac for e in received] == ["ff:ff:ff:ff:ff:ff"]


async def test_disassoc_null_rssi(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post(
        "/event", data=_body(event="disassoc", rssi=None), headers=_auth()
    )
    assert resp.status == 202
    assert received[0].kind is ApEventKind.DISASSOC
    assert received[0].rssi is None


async def test_uppercase_mac_normalized(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post(
        "/event", data=_body(mac="AA:BB:CC:DD:EE:01"), headers=_auth()
    )
    assert resp.status == 202
    assert received[0].mac == "aa:bb:cc:dd:ee:01"


async def test_token_of_other_node_forbidden(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=_body(), headers=_auth(TOKEN_GARDEN))
    assert resp.status == 403
    assert received == []


async def test_wrong_token_forbidden(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=_body(), headers=_auth("x" * 40))
    assert resp.status == 403
    assert received == []


async def test_missing_token_forbidden(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=_body())
    assert resp.status == 403
    assert received == []


async def test_non_bearer_scheme_forbidden(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post(
        "/event", data=_body(), headers={"Authorization": f"Basic {TOKEN_BED}"}
    )
    assert resp.status == 403
    assert received == []


async def test_unknown_node_forbidden(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=_body(node="ap-nope"), headers=_auth())
    assert resp.status == 403
    assert received == []


async def test_node_without_token_forbidden(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=_body(node="ap-living"), headers=_auth())
    assert resp.status == 403
    assert received == []


@pytest.mark.parametrize(
    "data",
    [
        "not json",
        "[]",
        json.dumps(
            {"node": "ap-bedroom", "mac": "aa:bb:cc:dd:ee:01", "event": "assoc"}
        ),
        _body(extra=1),
        _body(mac="aa:bb:cc:dd:ee"),
        _body(mac="aa-bb-cc-dd-ee-01"),
        _body(mac=12),
        _body(event="probe"),
        _body(rssi="-61"),
        _body(rssi=True),
        _body(rssi=-61.5),
        _body(rssi=0),
        _body(rssi=-128),
        _body(node=7),
    ],
)
async def test_malformed_rejected(aiohttp_server, push_config, data):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data=data, headers=_auth())
    assert resp.status == 400
    assert received == []


async def test_oversized_body_rejected(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.post("/event", data="x" * 4096, headers=_auth())
    assert resp.status == 413
    assert received == []


async def test_get_not_allowed(aiohttp_server, push_config):
    client, received = await _start(aiohttp_server, push_config)
    resp = await client.get("/event")
    assert resp.status == 405
    assert received == []
