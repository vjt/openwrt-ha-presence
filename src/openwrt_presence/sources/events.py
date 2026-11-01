"""HTTP endpoint for station events pushed by the APs.

``POST /event`` with ``Authorization: Bearer <node token>`` and body
``{"node": str, "mac": str, "event": "assoc"|"disassoc", "rssi": int|null}``.
See docs/plans/2026-11-01-ap-push-events.md.

Status codes: 202 accepted (tracked or not — no oracle on which MACs
eve tracks), 400 malformed, 403 unknown node / bad token, 413 oversize.
"""

from __future__ import annotations

import hmac
import json
import re
from typing import TYPE_CHECKING, Final, cast

import structlog
from aiohttp import web

from openwrt_presence.domain import ApEvent, ApEventKind, Mac, NodeName

if TYPE_CHECKING:
    from collections.abc import Callable

    from openwrt_presence.config import Config

MAX_BODY_BYTES: Final[int] = 1024
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_KEYS: Final[frozenset[str]] = frozenset({"node", "mac", "event", "rssi"})
_RSSI_MIN: Final[int] = -127
_RSSI_MAX: Final[int] = -1

_log: structlog.stdlib.BoundLogger = structlog.get_logger()


class EventRejectedError(Exception):
    """A pushed event was refused; ``status`` is the HTTP status to send."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_event(body: bytes, authorization: str | None, config: Config) -> ApEvent:
    """Validate and authenticate a pushed event.

    Returns the :class:`ApEvent`. Raises :class:`EventRejectedError` with
    status 400 for a malformed body, 403 when the node is unknown, has no
    token, or the bearer token does not match that node's token.
    """
    try:
        data: object = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EventRejectedError(400, "body is not JSON") from exc
    if not isinstance(data, dict):
        raise EventRejectedError(400, "body is not a JSON object")
    fields = cast("dict[str, object]", data)  # JSON object keys are str
    if frozenset(fields) != _KEYS:
        raise EventRejectedError(400, f"keys must be exactly {sorted(_KEYS)}")

    node, mac, kind, rssi = (
        fields["node"],
        fields["mac"],
        fields["event"],
        fields["rssi"],
    )
    if not isinstance(node, str):
        raise EventRejectedError(400, "node must be a string")
    if not isinstance(mac, str) or not _MAC_RE.match(mac.lower()):
        raise EventRejectedError(400, "mac must be aa:bb:cc:dd:ee:ff")
    try:
        event_kind = ApEventKind(kind)
    except ValueError as exc:
        raise EventRejectedError(400, "event must be assoc or disassoc") from exc
    if rssi is not None and (
        type(rssi) is not int or not _RSSI_MIN <= rssi <= _RSSI_MAX
    ):
        raise EventRejectedError(
            400, f"rssi must be null or an int in [{_RSSI_MIN}, {_RSSI_MAX}]"
        )

    node_cfg = config.nodes.get(NodeName(node))
    if node_cfg is None or node_cfg.token is None:
        raise EventRejectedError(403, "unknown node or node without token")
    expected = f"Bearer {node_cfg.token}".encode()
    presented = (authorization or "").encode()
    if not hmac.compare_digest(presented, expected):
        raise EventRejectedError(403, "bad token for node")

    return ApEvent(
        node=NodeName(node), mac=Mac(mac.lower()), kind=event_kind, rssi=rssi
    )


def make_app(config: Config, on_event: Callable[[ApEvent], None]) -> web.Application:
    """Build the aiohttp app serving ``POST /event``.

    *on_event* runs on the event loop for every accepted event; whatever
    it raises propagates to aiohttp (logged, 500 to the AP).
    """

    async def _handle(request: web.Request) -> web.Response:
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            _log.warning(
                "ap_event_rejected",
                status=413,
                reason="body too large",
                peer=request.remote,
            )
            raise
        try:
            event = parse_event(body, request.headers.get("Authorization"), config)
        except EventRejectedError as exc:
            _log.warning(
                "ap_event_rejected",
                status=exc.status,
                reason=exc.reason,
                peer=request.remote,
            )
            return web.Response(status=exc.status, text=exc.reason)

        tracked = config.mac_to_person(event.mac) is not None
        # Untracked stations (guests, IoT) are logged without their MAC.
        _log.info(
            "ap_event",
            node=event.node,
            kind=event.kind.value,
            tracked=tracked,
            mac=event.mac if tracked else None,
            rssi=event.rssi,
        )
        on_event(event)
        return web.Response(status=202)

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post("/event", _handle)
    return app
