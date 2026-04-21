"""Structural ``MqttClient`` protocol — the minimum paho surface this
codebase touches.

Defined here (not in ``mqtt.py``) so tests' ``FakeMqttClient`` satisfies
it without importing production MQTT code.

Rationale: although ``paho-mqtt`` v2 ships a ``py.typed`` marker, strict
pyright still flags many ``Client`` method accesses as
``reportUnknownMemberType`` (paho's own internals leak ``Unknown`` through
the public surface).  We enumerate exactly the surface this project
touches and type the boundary — a typing concession that also becomes a
budget: adding new paho methods requires adding them here first.  See
finding H5 in ``docs/reviews/architecture/2026-04-21-architecture-review.md``.

Return types are deliberately widened to ``object`` for methods whose
result we discard.  This lets both paho's real client (which returns
``MQTTErrorCode`` / ``None`` / ``Client`` depending on the method) and the
test ``FakeMqttClient`` (which returns ``None`` uniformly) satisfy the
protocol without either having to lie about reality.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

# paho-mqtt v2 CallbackAPIVersion.VERSION2 callback shapes.  The opaque
# ``object`` params absorb paho's own types (``ReasonCode``, ``Properties``,
# ``ConnectFlags``, ``DisconnectFlags``) — we only read them via ``str()``
# in the logger, never dispatch on them.
OnConnect = Callable[[object, object, object, object, object | None], None]
OnDisconnect = Callable[[object, object, object, object, object | None], None]


class PublishResult(Protocol):
    """Minimal ``MQTTMessageInfo`` shape — we only read ``rc``."""

    rc: int


class MqttClient(Protocol):
    """Paho-mqtt-shaped client.  Tests use ``FakeMqttClient``.

    Return types for methods whose result we ignore are widened to
    ``object`` so both paho (``MQTTErrorCode`` / ``Client`` / ``None``)
    and the fake (``None``) satisfy the protocol.
    """

    # Both paho and ``FakeMqttClient`` expose ``on_connect`` / ``on_disconnect``
    # as ``@property`` + setter descriptors.  Declaring them as property here
    # (rather than bare attributes) keeps the protocol structural — invariant
    # attribute matching would otherwise reject both implementations.
    @property
    def on_connect(self) -> OnConnect | None: ...

    @on_connect.setter
    def on_connect(self, cb: OnConnect | None) -> None: ...

    @property
    def on_disconnect(self) -> OnDisconnect | None: ...

    @on_disconnect.setter
    def on_disconnect(self, cb: OnDisconnect | None) -> None: ...

    def will_set(
        self, topic: str, payload: str, qos: int = 0, retain: bool = False
    ) -> None: ...

    def connect_async(self, host: str, port: int) -> object: ...

    def loop_start(self) -> object: ...

    def loop_stop(self) -> object: ...

    def disconnect(self) -> object: ...

    def publish(
        self,
        topic: str,
        payload: str = "",
        qos: int = 0,
        retain: bool = False,
    ) -> PublishResult: ...

    def enable_logger(self, logger: object = None) -> None: ...

    def username_pw_set(
        self, username: str | None, password: str | None = None
    ) -> None: ...

    def reconnect_delay_set(self, min_delay: int, max_delay: int) -> None: ...

    def max_queued_messages_set(self, queue_size: int) -> object: ...
